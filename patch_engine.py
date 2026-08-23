#!/usr/bin/env python3
"""Patch a fresh clone of theautoman for the Colab GPU build.

Run from the repo root (or pass repo root as argv[1]):
    python3 patch_engine.py [/path/to/theautoman]

Does the following:
 1. NVENC: adds a `_video_flags(export)` helper to app/ffmpeg/wrapper.py and
    app/transitions/engine.py so h264_nvenc gets correct flags when
    AUTOMAN_CODEC=h264_nvenc. Makes config codec read $AUTOMAN_CODEC.
 2. Kokoro: writes app/tts/kokoro_provider.py (OpenAI-compatible local server).
 3. Registers the Kokoro provider in voice_manager.py and adds the enum.
 4. AUDIO: mix_background_music() honors `mute_original`; pipeline Stage 5 uses
    per-project music_name / music_volume / mute_original from project.json
    (written by automan-web) instead of a random bg_music track + global volume.
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()

HELPER = '''
def _video_flags(export):
    """Return codec flags for export. Uses NVENC flags when codec is an nvenc
    codec (h264_nvenc / hevc_nvenc), QSV flags for Intel Quick Sync
    (h264_qsv / hevc_qsv), otherwise the original x264 flags."""
    codec = (export.codec or "libx264").lower()
    if "nvenc" in codec:
        q = getattr(export, "quality", None)
        qv = getattr(q, "value", None) if q is not None else None
        nv_preset = {"low": "p1", "medium": "p3", "high": "p4", "ultra": "p6"}.get(qv, "p4")
        return ["-c:v", export.codec, "-rc", "vbr", "-cq", str(export.crf),
                "-b:v", "0", "-preset", nv_preset]
    if "qsv" in codec:
        q = getattr(export, "quality", None)
        qv = getattr(q, "value", None) if q is not None else None
        qsv_preset = {"low": "veryfast", "medium": "medium", "high": "slow", "ultra": "veryslow"}.get(qv, "medium")
        return ["-c:v", export.codec, "-global_quality", str(export.crf),
                "-preset", qsv_preset, "-look_ahead", "0"]
    return ["-c:v", export.codec, "-crf", str(export.crf), "-preset", export.preset_speed]
'''

KOKORO_PROVIDER = '''"""Kokoro TTS provider (OpenAI-compatible local/GPU server)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from app.tts.base import TTSProvider, TTSResult, Voice
from app.utils.logger import get_logger

log = get_logger("tts.kokoro")

KOKORO_VOICES = [
    ("af_heart", "Heart (US female)", "female"),
    ("af_bella", "Bella (US female)", "female"),
    ("af_nicole", "Nicole (US female)", "female"),
    ("af_sarah", "Sarah (US female)", "female"),
    ("af_sky", "Sky (US female)", "female"),
    ("am_adam", "Adam (US male)", "male"),
    ("am_michael", "Michael (US male)", "male"),
    ("am_fenrir", "Fenrir (US male)", "male"),
    ("bf_emma", "Emma (UK female)", "female"),
    ("bf_isabella", "Isabella (UK female)", "female"),
    ("bm_george", "George (UK male)", "male"),
    ("bm_lewis", "Lewis (UK male)", "male"),
    ("ff_siwis", "Siwis (FR female)", "female"),
]


class KokoroProvider(TTSProvider):
    provider_name = "kokoro"

    def __init__(self, base_url: Optional[str] = None):
        self._base_url = base_url or os.environ.get("KOKORO_URL", "http://localhost:8002/v1")
        self._model = os.environ.get("KOKORO_MODEL", "kokoro")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(base_url=self._base_url,
                                  api_key=os.environ.get("KOKORO_API_KEY", "kokoro"))
        return self._client

    def is_available(self) -> bool:
        return True

    async def generate(self, text, output_path, voice_id="af_heart", speed=1.0,
                       pitch=1.0, stability=0.5, emotion=None, **kwargs):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        def _gen():
            self._get_client().audio.speech.create(
                model=self._model, voice=voice_id, input=text,
                speed=speed, response_format="mp3").stream_to_file(str(output_path))

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _gen)
        from app.utils.audio import get_audio_duration
        d = get_audio_duration(output_path)
        log.info(f"Generated Kokoro audio: {output_path.name} ({d:.1f}s)")
        return TTSResult(audio_path=output_path, duration=d, voice_id=voice_id, text=text)

    async def list_voices(self) -> list[Voice]:
        return [Voice(voice_id=v, name=n, provider=self.provider_name, language="en", gender=g)
                for v, n, g in KOKORO_VOICES]
'''


def patch_wrapper(path: Path) -> bool:
    src = path.read_text()
    changed = False
    pat = re.compile(r'"-c:v",\s*export\.codec,\s*"-crf",\s*str\(export\.crf\),\s*"-preset",\s*export\.preset_speed,', re.S)
    if "_video_flags(export)" not in src:
        src, n = pat.subn("*_video_flags(export),", src)
        if n:
            changed = True
    if "def _video_flags" not in src:
        src = src.rstrip() + "\n" + HELPER + "\n"
        changed = True
    if changed:
        path.write_text(src)
        print(f"[wrapper] _video_flags ensured")
    return changed


def patch_engine_transitions(path: Path) -> bool:
    if not path.exists():
        return False
    src = path.read_text()
    changed = False
    pat = re.compile(r'"-c:v",\s*export\.codec,\s*"-crf",\s*str\(export\.crf\),\s*"-preset",\s*export\.preset_speed,', re.S)
    if "_video_flags(export)" not in src:
        src, n = pat.subn("*_video_flags(export),", src)
        if n:
            changed = True
    if "def _video_flags" not in src:
        src = src.rstrip() + "\n" + HELPER + "\n"
        changed = True
    if changed:
        path.write_text(src)
        print(f"[transitions] _video_flags ensured")
    return changed


def patch_config(path: Path) -> bool:
    src = path.read_text()
    changed = False
    if "AUTOMAN_CODEC" not in src:
        src = src.replace('codec: str = "libx264"',
                          'codec: str = os.environ.get("AUTOMAN_CODEC", "libx264")', 1)
        if "import os" not in src.split("class ExportSettings")[0]:
            src = src.replace("from __future__ import annotations",
                              "from __future__ import annotations\nimport os", 1)
        changed = True
    if 'POCKET = "pocket"' in src and 'KOKORO = "kokoro"' not in src:
        src = src.replace('    POCKET = "pocket"', '    POCKET = "pocket"\n    KOKORO = "kokoro"', 1)
        changed = True
    if changed:
        path.write_text(src)
        print("[config] codec->env + KOKORO enum added")
    return changed


def patch_voice_manager(path: Path) -> bool:
    src = path.read_text()
    if "kokoro_provider" in src:
        return False
    src = src.replace(
        "from app.tts.fish_audio_provider import FishAudioProvider",
        "from app.tts.fish_audio_provider import FishAudioProvider\nfrom app.tts.kokoro_provider import KokoroProvider", 1)
    src = src.replace(
        'self._providers["inworld"] = inworld',
        'self._providers["inworld"] = inworld\n        self._providers["kokoro"] = KokoroProvider()', 1)
    path.write_text(src)
    print("[voice_manager] Kokoro registered")
    return True


def patch_wrapper_audio(path: Path) -> bool:
    """mix_background_music: honor mute_original instead of hardcoded voice=1.0."""
    src = path.read_text(encoding="utf-8")
    if "mute_original" in src:
        return False
    changed = False
    sig_old = ("    music_volume: float = 0.20,\n"
               "    export: ExportSettings | None = None,\n"
               ") -> None:")
    sig_new = ("    music_volume: float = 0.20,\n"
               "    export: ExportSettings | None = None,\n"
               "    mute_original: bool = False,\n"
               ") -> None:")
    if sig_old in src:
        src = src.replace(sig_old, sig_new, 1)
        changed = True
    af_old = ('    # voice stays full volume; music is attenuated; normalize=0 preserves levels\n'
              '    af = (\n'
              '        f"[0:a]volume=1.0[voice];"\n')
    af_new = ('    # voice stays full volume unless muted; music attenuated; normalize=0 preserves levels\n'
              '    orig_vol = 0.0 if mute_original else 1.0\n'
              '    af = (\n'
              '        f"[0:a]volume={orig_vol:.1f}[voice];"\n')
    if af_old in src:
        src = src.replace(af_old, af_new, 1)
        changed = True
    if changed:
        path.write_text(src, encoding="utf-8")
        print("[wrapper] mix_background_music mute_original supported")
    else:
        print("[wrapper] WARNING: mix_background_music patterns not matched")
    return changed


# --- Stage 5 replacement -----------------------------------------------------
# Matches from `cfg = get_config()` through the bg_music warning line.
STAGE5_RE = re.compile(
    r"        cfg = get_config\(\)\n"
    r"        if cfg\.bg_music_enabled:\n"
    r".*?"
    r'log\.warning\(f"  \u26a0 Background music enabled but no files found in \{bg_dir\}"\)\n',
    re.S,
)

STAGE5_NEW = (
    "        chosen, proj_vol, proj_mute = self._resolve_bg_music()\n"
    "        if chosen:\n"
    '            log.info(f"\u25b8 Stage 5/5: Mixing background music: {chosen.name} "\n'
    '                     f"(vol={proj_vol:.0%}{\', original muted\' if proj_mute else \'\'})")\n'
    '            self._emit_progress("Mixing BG Music", self.project.scene_count, self.project.scene_count, 0.95)\n'
    '            bgm_tmp = final.parent / (final.stem + "_bgm_tmp.mp4")\n'
    "            mix_background_music(final, chosen, bgm_tmp, music_volume=proj_vol,\n"
    "                                 export=self.export, mute_original=proj_mute)\n"
    "            import os as _os\n"
    "            _os.replace(str(bgm_tmp), str(final))\n"
    '            log.info("  \u2713 Background music applied.")\n'
)

RESOLVE_HELPER = '''
    def _resolve_bg_music(self):
        """Resolve background music from project settings.

        Returns (music_path | None, music_volume, mute_original).
        Priority: project.music_name looked up in <project>/music/,
        <engine root>/music/, then <engine root>/bg_music/; otherwise a
        random track from those dirs. Volume/mute come from project.json
        when present, falling back to global config."""
        cfg = get_config()
        proj = getattr(self.project, "config", None)
        pv = getattr(proj, "music_volume", None)
        vol = float(pv) if pv is not None else float(cfg.bg_music_volume)
        mute = bool(getattr(proj, "mute_original", False))
        name = getattr(proj, "music_name", None)
        exts = (".mp3", ".m4a", ".wav", ".aac", ".ogg")
        roots = [self.project.project_dir / "music",
                 Path(__file__).resolve().parents[2] / "music",
                 Path(__file__).resolve().parents[2] / "bg_music"]
        candidates = []
        for root in roots:
            if not root.is_dir():
                continue
            tracks = [f for f in sorted(root.iterdir())
                      if f.is_file() and f.suffix.lower() in exts]
            if name:
                for f in tracks:
                    if f.stem == name:
                        return f, vol, mute
            candidates += tracks
        if candidates:
            return random.choice(candidates), vol, mute
        return None, vol, mute

'''

def patch_pipeline_audio(path: Path) -> bool:
    """Stage 5: use per-project music settings from project.json."""
    src = path.read_text(encoding="utf-8")
    if "_resolve_bg_music" in src:
        print("[pipeline] already patched")
        return False
    changed = False
    src2, n = STAGE5_RE.subn(lambda m: STAGE5_NEW, src, count=1)
    if n:
        src = src2
        changed = True
        print("[pipeline] Stage 5 block replaced")
    else:
        print("[pipeline] WARNING: Stage 5 block not matched!")
    anchor = "    async def run(self)"
    if anchor in src:
        src = src.replace(anchor, RESOLVE_HELPER + anchor, 1)
        changed = True
    path.write_text(src, encoding="utf-8")
    print("[pipeline] Stage 5 uses project music settings")
    return changed


def patch_project_model(path: Path) -> bool:
    """ProjectConfig: accept music_name / music_volume / mute_original / music_loop."""
    src = path.read_text(encoding="utf-8")
    if "music_volume" in src:
        return False
    marker = "    scenes: list[SceneConfig] = Field(min_length=1)"
    if marker in src:
        src = src.replace(marker, marker + "\n"
                          "    music_name: Optional[str] = None\n"
                          "    music_volume: Optional[float] = Field(default=None, ge=0.0, le=1.0)\n"
                          "    mute_original: bool = False\n"
                          "    music_loop: bool = True", 1)
    path.write_text(src, encoding="utf-8")
    print("[project] music fields added to ProjectConfig")
    return True


def main():
    (ROOT / "app" / "tts" / "kokoro_provider.py").write_text(KOKORO_PROVIDER)
    print("[kokoro_provider.py] written")
    patch_wrapper(ROOT / "app" / "ffmpeg" / "wrapper.py")
    patch_engine_transitions(ROOT / "app" / "transitions" / "engine.py")
    patch_config(ROOT / "app" / "core" / "config.py")
    patch_voice_manager(ROOT / "app" / "tts" / "voice_manager.py")
    patch_wrapper_audio(ROOT / "app" / "ffmpeg" / "wrapper.py")
    patch_project_model(ROOT / "app" / "core" / "project.py")
    patch_pipeline_audio(ROOT / "app" / "core" / "pipeline.py")


if __name__ == "__main__":
    main()
