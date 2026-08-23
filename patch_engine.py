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

NOTE on audio (mute original / music volume): the engine natively handles this
per-scene in app/renderer/scene_renderer.py (add_audio_to_video with mute_bg,
bg_audio_volume, music_volume, music_path). Do NOT patch Stage 5 or
mix_background_music — the web app's settings flow through project.json ->
scene_renderer already. Earlier patches that re-mixed music at Stage 5 and
zeroed [0:a] muted the TTS voice too — that was a bug, removed.
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()

HELPER = '''
_AUTO_CODEC_CACHE: list[str | None] = [None]

def _resolve_codec(export):
    """Return the video codec to use.

    Explicit AUTOMAN_CODEC (or project) value wins. When codec is "auto",
    probe the ffmpeg binary ONCE with real 1-frame encodes and pick the best
    hardware encoder that actually works: h264_nvenc (NVIDIA) >
    h264_qsv (Intel QSV) > h264_amf (AMD) > libx264 (CPU). Checking the
    encoder list is NOT enough - full ffmpeg builds list nvenc/qsv even when
    no such GPU exists, so each candidate is verified with a real encode."""
    codec = (export.codec or "libx264").lower()
    if codec != "auto":
        return codec
    if _AUTO_CODEC_CACHE[0] is not None:
        return _AUTO_CODEC_CACHE[0]
    import shutil as _sh, subprocess as _sp, tempfile as _tf
    ff = getattr(export, "ffmpeg_path", None) or _sh.which("ffmpeg") or "ffmpeg"
    base = ["-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "testsrc=size=320x180:rate=15", "-frames:v", "1", "-y"]
    candidates = [("h264_nvenc", ["-rc", "vbr", "-cq", "28", "-preset", "p4"]),
                  ("h264_qsv", ["-global_quality", "28", "-preset", "medium", "-look_ahead", "0"]),
                  ("h264_amf", ["-rc", "cqp", "-qp_i", "28", "-qp_p", "28", "-quality", "balanced"])]
    for cand, extra in candidates:
        tmp = _tf.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.close()
        try:
            r = _sp.run([ff] + base + ["-c:v", cand] + extra + [tmp.name],
                        capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                _AUTO_CODEC_CACHE[0] = cand
                print(f"[automan] codec auto-detect -> {cand}")
                return cand
        except Exception:
            pass
        finally:
            try:
                import os as _os
                _os.unlink(tmp.name)
            except Exception:
                pass
    _AUTO_CODEC_CACHE[0] = "libx264"
    print("[automan] codec auto-detect -> libx264 (no working hardware encoder)")
    return _AUTO_CODEC_CACHE[0]


def _video_flags(export):
    """Return codec flags for export. Uses NVENC flags when codec is an nvenc
    codec (h264_nvenc / hevc_nvenc), QSV flags for Intel Quick Sync
    (h264_qsv / hevc_qsv), AMF flags for AMD (h264_amf), otherwise the
    original x264 flags. Honors codec="auto" via _resolve_codec."""
    codec = _resolve_codec(export)
    if "nvenc" in codec:
        q = getattr(export, "quality", None)
        qv = getattr(q, "value", None) if q is not None else None
        nv_preset = {"low": "p1", "medium": "p3", "high": "p4", "ultra": "p6"}.get(qv, "p4")
        return ["-c:v", codec, "-rc", "vbr", "-cq", str(export.crf),
                "-b:v", "0", "-preset", nv_preset]
    if "qsv" in codec:
        q = getattr(export, "quality", None)
        qv = getattr(q, "value", None) if q is not None else None
        qsv_preset = {"low": "veryfast", "medium": "medium", "high": "slow", "ultra": "veryslow"}.get(qv, "medium")
        return ["-c:v", codec, "-global_quality", str(export.crf),
                "-preset", qsv_preset, "-look_ahead", "0"]
    if "amf" in codec:
        q = getattr(export, "quality", None)
        qv = getattr(q, "value", None) if q is not None else None
        amf_preset = {"low": "speed", "medium": "balanced", "high": "quality", "ultra": "quality"}.get(qv, "balanced")
        return ["-c:v", codec, "-rc", "cqp", "-qp_i", str(export.crf),
                "-qp_p", str(export.crf), "-quality", amf_preset]
    return ["-c:v", codec, "-crf", str(export.crf), "-preset", export.preset_speed]
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
                          'codec: str = os.environ.get("AUTOMAN_CODEC", "auto")', 1)
        if "import os" not in src.split("class ExportSettings")[0]:
            src = src.replace("from __future__ import annotations",
                              "from __future__ import annotations\nimport os", 1)
        changed = True
    elif 'os.environ.get("AUTOMAN_CODEC", "libx264")' in src:
        src = src.replace('os.environ.get("AUTOMAN_CODEC", "libx264")',
                          'os.environ.get("AUTOMAN_CODEC", "auto")', 1)
        changed = True
    if 'POCKET = "pocket"' in src and 'KOKORO = "kokoro"' not in src:
        src = src.replace('    POCKET = "pocket"', '    POCKET = "pocket"\n    KOKORO = "kokoro"', 1)
        changed = True
    if changed:
        path.write_text(src)
        print("[config] codec->auto env + KOKORO enum added")
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


def main():
    (ROOT / "app" / "tts" / "kokoro_provider.py").write_text(KOKORO_PROVIDER)
    print("[kokoro_provider.py] written")
    patch_wrapper(ROOT / "app" / "ffmpeg" / "wrapper.py")
    patch_engine_transitions(ROOT / "app" / "transitions" / "engine.py")
    patch_config(ROOT / "app" / "core" / "config.py")
    patch_voice_manager(ROOT / "app" / "tts" / "voice_manager.py")


if __name__ == "__main__":
    main()
