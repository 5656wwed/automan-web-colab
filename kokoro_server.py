"""Minimal Kokoro TTS server (OpenAI-compatible /v1/audio/speech) for Colab GPU.

Runs in the base Colab Python (which has torch+CUDA preinstalled). Exposes
POST /v1/audio/speech and GET /v1/models so the engine's KokoroProvider
(an OpenAI-compatible client) can call it.

Usage:
    python kokoro_server.py [--port 8002]
"""
import argparse
import io
import os
import subprocess
import tempfile

import torch

# Load the model once at startup (GPU if available).
_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
_pipelines = {}  # lang_code -> KPipeline


def _get_pipeline(lang: str):
    if lang not in _pipelines:
        from kokoro import KPipeline
        _pipelines[lang] = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M",
                                     device=_DEVICE)
    return _pipelines[lang]


def _lang_for_voice(voice: str) -> str:
    v = (voice or "af_heart").lower()
    if v.startswith("bf_") or v.startswith("bm_"):
        return "b"
    if v.startswith("ff_") or v.startswith("fm_"):
        return "f"
    return "a"


def _wav_bytes(text, voice, speed) -> bytes:
    pipe = _get_pipeline(_lang_for_voice(voice))
    chunks = []
    sr = 24000
    for result in pipe(text, voice=voice, speed=float(speed)):
        chunks.append(result.audio)
    if not chunks:
        raise RuntimeError("Kokoro produced no audio")
    audio = torch.cat(chunks).detach().cpu().numpy()
    fd, wav = tempfile.mkstemp(suffix=".wav")
    try:
        import soundfile as sf
        sf.write(wav, audio, sr)
        mp3 = wav[:-4] + ".mp3"
        import shutil as _sh
        ff = os.environ.get("KOKORO_FFMPEG") or _sh.which("ffmpeg") or "/usr/bin/ffmpeg"
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", wav,
                        "-b:a", "192k", mp3], check=True)
        return open(mp3, "rb").read()
    finally:
        try:
            os.close(fd)
            os.remove(wav)
            if os.path.exists(wav[:-4] + ".mp3"):
                os.remove(wav[:-4] + ".mp3")
        except Exception:
            pass


from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="Kokoro TTS (Colab)")


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "kokoro", "object": "model"}]}


@app.post("/v1/audio/speech")
async def speech(req: Request):
    body = await req.json()
    text = body.get("input", "")
    voice = body.get("voice", "af_heart")
    speed = body.get("speed", 1.0)
    if not text:
        return JSONResponse({"error": "empty input"}, status_code=400)
    audio = _wav_bytes(text, voice, speed)
    return Response(content=audio, media_type="audio/mpeg")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("KOKORO_PORT", "8002")))
    args = ap.parse_args()
    print(f"Kokoro server on {_DEVICE} :{args.port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
