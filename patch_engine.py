#!/usr/bin/env python3
"""Patch a fresh clone of theautoman for the Colab GPU build.

Run from the repo root (or pass repo root as argv[1]):
    python3 patch_engine.py [/path/to/theautoman]

Does three things:
 1. NVENC: adds a `_video_flags(export)` helper to app/ffmpeg/wrapper.py and
    app/transitions/engine.py so h264_nvenc gets correct flags when
    AUTOMAN_CODEC=h264_nvenc. Makes config codec read $AUTOMAN_CODEC.
 2. Kokoro: writes app/tts/kokoro_provider.py (OpenAI-compatible local server).
 3. Registers the Kokoro provider in voice_manager.py and adds the enum.
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()

HELPER = '''
def _video_flags(export):
    """Return codec flags for export. Uses NVENC flags when codec is an nvenc
    codec (h264_nvenc / hevc_nvenc), otherwise the original x264 flags."""
    codec = (export.codec or "libx264").lower()
    if "nvenc" in codec:
        q = getattr(export, "quality", None)
        qv = getattr(q, "value", None) if q is not None else None
        nv_preset = {"low": "p1", "medium": "p3", "high": "p4", "ultra": "p6"}.get(qv, "p4")
        return ["-c:v", export.codec, "-rc", "vbr", "-cq", str(export.crf),
                "-b:v", "0", "-preset", nv_preset]
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
    if "_video_flags(export)" in src:
        return False
    src = src.replace("def _run_ffmpeg(", HELPER + "\ndef _run_ffmpeg(", 1)
    pat = re.compile(r'"-c:v",\s*export\.codec,\s*"-crf",\s*str\(export\.crf\),\s*"-preset",\s*export\.preset_speed,', re.S)
    src, n = pat.subn("*_video_flags(export),", src)
    path.write_text(src)
    print(f"[wrapper] patched {n} encode blocks")
    return True


def patch_engine_transitions(path: Path) -> bool:
    if not path.exists():
        return False
    src = path.read_text()
    if "_video_flags(export)" in src:
        return False
    pat = re.compile(r'"-c:v",\s*export\.codec,\s*"-crf",\s*str\(export\.crf\),\s*"-preset",\s*export\.preset_speed,', re.S)
    src, n = pat.subn("*_video_flags(export),", src)
    if n and "_video_flags" not in src:
        src = src.replace("def _build_xfade", HELPER + "\ndef _build_xfade", 1) if "def _build_xfade" in src else src
    path.write_text(src)
    print(f"[transitions] patched {n} encode blocks")
    return True


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


def main():
    (ROOT / "app" / "tts" / "kokoro_provider.py").write_text(KOKORO_PROVIDER)
    print("[kokoro_provider.py] written")
    patch_wrapper(ROOT / "app" / "ffmpeg" / "wrapper.py")
    patch_engine_transitions(ROOT / "app" / "transitions" / "engine.py")
    patch_config(ROOT / "app" / "core" / "config.py")
    patch_voice_manager(ROOT / "app" / "tts" / "voice_manager.py")


if __name__ == "__main__":
    main()
