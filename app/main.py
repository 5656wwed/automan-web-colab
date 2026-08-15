"""AutoScene Studio — Web front end.
Upload a script (one narration line per beat) + video clips, render a
narrated documentary, download the finished MP4.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi import Request

# ---------------------------------------------------------------------------
# Paths & engine wiring
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent          # /home/ubuntu/automan-web
UPLOAD_DIR = BASE_DIR / "uploads"
STAGE_DIR = BASE_DIR / "stage"
JOBS_DIR = BASE_DIR / "jobs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
STAGE_DIR.mkdir(parents=True, exist_ok=True)

# The theautoman engine lives in a sibling checkout.
THEAUTOMAN_DIR = Path(os.environ.get("THEAUTOMAN_DIR", "/home/ubuntu/theautoman"))
PYTHON = Path(os.environ.get("THEAUTOMAN_PYTHON", str(THEAUTOMAN_DIR / "venv" / "bin" / "python")))
MAIN_PY = THEAUTOMAN_DIR / "app" / "main.py"
# Shared pronunciation dictionary (read by the engine's TTS cleaner).
PRONUNCIATION_MAP_PATH = THEAUTOMAN_DIR / "pronunciation_map.json"
# CapCut-style 3D LUT color filters (applied via ffmpeg lut3d).
LUTS_DIR = THEAUTOMAN_DIR / "luts"
LUTS_DIR.mkdir(parents=True, exist_ok=True)
MUSIC_DIR = THEAUTOMAN_DIR / "music"
MUSIC_DIR.mkdir(parents=True, exist_ok=True)
# ffmpeg binary: prefer $FFMPEG_PATH env, else the engine's bundled one, else system
_BUNDLED_FF = THEAUTOMAN_DIR / ".tools" / "ffmpeg" / "bin" / "ffmpeg"
_FFMPEG = os.environ.get("FFMPEG_PATH") or (str(_BUNDLED_FF) if _BUNDLED_FF.exists() else "ffmpeg")
_BUNDLED_FP = THEAUTOMAN_DIR / ".tools" / "ffmpeg" / "bin" / "ffprobe"
_FFPROBE = os.environ.get("FFPROBE_PATH") or (str(_BUNDLED_FP) if _BUNDLED_FP.exists() else "ffprobe")

ALLOWED_MEDIA = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".jpg", ".jpeg", ".png", ".webp"}
# Directory where Pocket TTS voice clones are stored (env-overridable for Colab).
POCKET_VOICES_DIR = Path(os.environ.get("POCKET_VOICES_DIR", "/home/ubuntu/pocket_tts_voices"))
# Finished videos are copied here when set (e.g. Google Drive on Colab).
DRIVE_OUT_DIR = Path(os.environ.get("DRIVE_OUT_DIR", "")) if os.environ.get("DRIVE_OUT_DIR") else None


def _save_to_drive(src: Path) -> str:
    """Copy a finished video into DRIVE_OUT_DIR (e.g. Google Drive). Returns a
    display path or an error message; never raises."""
    if not DRIVE_OUT_DIR or not src.exists():
        return ""
    try:
        DRIVE_OUT_DIR.mkdir(parents=True, exist_ok=True)
        dest = DRIVE_OUT_DIR / src.name
        shutil.copyfile(src, dest)
        return str(dest)
    except Exception as e:
        return f"Drive save failed: {e}"


def re_search_num(s: str) -> int:
    """Extract the first integer from a string (for natural file ordering)."""
    import re
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else 0


# Strip story-beat labels ("Beat 1:", "1.", "3)", "#7", "2 -") from the start of a
# narration line so the TTS never reads the internal script marker out loud.
# Only matches a leading label; never touches mid-sentence numbers like "3 legions".
_BEAT_LABEL_RE = re.compile(r"^\s*(?:beat\s*)?(?:\d+\s*[:.)-]|#\d+)\s*", re.IGNORECASE)

def clean_beat(line: str) -> str:
    return _BEAT_LABEL_RE.sub("", line, count=1).strip()


# ---------------------------------------------------------------------------
# Settings (persisted to settings.json)
# ---------------------------------------------------------------------------
SETTINGS_PATH = BASE_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "retention_days": 7,                 # auto-delete server projects after N days
    "cleanup_enabled": True,             # run the auto-cleanup task
    "cleanup_every_hours": 1,
    "default_provider": "edge",          # edge | pocket
    "default_voice": "en-US-GuyNeural",  # edge voice or pocket name/hf:// URL
    "admin_email": "",                   # login email (if set, login requires email+password)
    "admin_password": "",                # if empty, login is disabled
    "session_hours": 12,
    "create_defaults": {},               # last-used Create-tab settings (auto-saved)
    "auto_apply_default_preset": True,   # apply the default (starred) preset on every load
}

PRESETS_PATH = BASE_DIR / "presets.json"

def _load_presets() -> dict:
    try:
        return json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in data.items() if v is not None})
    # Env overrides (used on Colab so the public tunnel stays password-protected
    # without committing a password to the repo).
    if os.environ.get("ADMIN_PASSWORD"):
        merged["admin_password"] = os.environ["ADMIN_PASSWORD"]
        if os.environ.get("ADMIN_EMAIL"):
            merged["admin_email"] = os.environ["ADMIN_EMAIL"]
    return merged

def _save_settings(settings: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")

def get_settings() -> dict:
    return _load_settings()


# ---------------------------------------------------------------------------
# Auth (simple session cookie — single-worker deployment)
# ---------------------------------------------------------------------------
SESSIONS: dict[str, float] = {}  # token -> expiry (epoch)
SESSIONS_PATH = BASE_DIR / "sessions.json"

def _load_sessions():
    """Restore sessions from disk so a service restart doesn't log everyone out."""
    global SESSIONS
    try:
        if SESSIONS_PATH.exists():
            data = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
            now = time.time()
            SESSIONS = {k: v for k, v in data.items() if v > now}  # drop expired
    except Exception:
        SESSIONS = {}

def _save_sessions():
    try:
        SESSIONS_PATH.write_text(json.dumps(SESSIONS, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def _hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}::{password}".encode()).hexdigest()

def make_session() -> str:
    token = secrets.token_hex(24)
    hours = get_settings().get("session_hours", 12)
    SESSIONS[token] = time.time() + hours * 3600
    _save_sessions()
    return token

def require_auth(request: Request) -> None:
    if not get_settings().get("admin_password"):
        return  # login disabled
    token = request.cookies.get("automan_session")
    exp = SESSIONS.get(token or "")
    if not token or not exp or exp < time.time():
        raise HTTPException(401, "Not logged in")
    return


_load_sessions()  # restore persisted sessions so restarts don't log users out


app = FastAPI(title="AutoScene Studio Web")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# In-memory job registry (single-worker deployments only)
# ---------------------------------------------------------------------------
JOBS: dict[str, dict] = {}


def _job(job_id: str) -> dict:
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, f"Job {job_id} not found.")
    return j


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    resp = FileResponse(BASE_DIR / "app" / "static" / "index.html")
    # Never cache the page — guarantees the browser always loads the latest JS,
    # so stale code can't cause phantom "failed" states.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "engine": str(THEAUTOMAN_DIR), "jobs": len(JOBS)}


# ---------------------------------------------------------------------------
# Pronunciation dictionary (edit once, applied to every render automatically)
# ---------------------------------------------------------------------------
@app.get("/api/pronunciations")
def get_pronunciations(request: Request):
    """Return the current pronunciation dictionary as {Name: phonetic}."""
    require_auth(request)
    try:
        return JSONResponse(json.loads(PRONUNCIATION_MAP_PATH.read_text(encoding="utf-8")))
    except Exception:
        return JSONResponse({})


@app.put("/api/pronunciations")
async def save_pronunciations(body: dict, request: Request):
    """Replace the dictionary. body = { map: {Name: phonetic, ...} }"""
    require_auth(request)
    mapping = (body or {}).get("map") or {}
    # keep only string key/value pairs, strip empties
    clean = {str(k).strip(): str(v).strip() for k, v in mapping.items() if str(v).strip()}
    try:
        PRONUNCIATION_MAP_PATH.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        raise HTTPException(500, f"Could not save pronunciation map: {e}")
    return {"saved": len(clean)}


# ---------------------------------------------------------------------------
# Auto-detect proper nouns in a script (spaCy NER) to flag for pronunciation
# ---------------------------------------------------------------------------
_nlp = None

def _get_nlp():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
    return _nlp


@app.post("/api/names")
def api_names(body: dict, request: Request):
    require_auth(request)
    text = (body or {}).get("script") or ""
    if not text.strip():
        return {"names": [], "note": ""}
    try:
        nlp = _get_nlp()
        doc = nlp(text)
        names, seen = [], set()
        for ent in doc.ents:
            if ent.label_ in ("PERSON", "NORP", "GPE", "ORG", "LOC", "PRODUCT", "EVENT"):
                k = ent.text.lower()
                if k not in seen:
                    seen.add(k)
                    names.append(ent.text)
        return {"names": sorted(names), "note": ""}
    except Exception as e:
        return {"names": [], "note": f"spaCy not available: {e}"}


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@app.post("/api/login")
def login(body: dict, response: Response):
    settings = get_settings()
    pw = settings.get("admin_password")
    if not pw:
        return {"ok": True, "note": "login disabled"}
    email = (body or {}).get("email", "").strip().lower()
    guess = (body or {}).get("password", "")
    # if an admin email is configured, it must match too
    if settings.get("admin_email") and email != str(settings.get("admin_email")).strip().lower():
        raise HTTPException(401, "Wrong email or password")
    salt = settings.get("password_salt", "automan")
    if _hash_pw(guess, salt) != _hash_pw(pw, salt):
        raise HTTPException(401, "Wrong email or password")
    token = make_session()
    response.set_cookie("automan_session", token, httponly=True, samesite="lax", max_age=settings.get("session_hours", 12) * 3600)
    return {"ok": True, "email": email or str(settings.get("admin_email", "")).lower()}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("automan_session")
    if token and token in SESSIONS:
        del SESSIONS[token]
        _save_sessions()
    response.delete_cookie("automan_session")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    require_auth(request)
    settings = get_settings()
    return {
        "authed": bool(settings.get("admin_password")),
        "login_required": bool(settings.get("admin_password")),
        "logged_in": True,
    }


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------
@app.get("/api/settings")
def api_get_settings(request: Request):
    require_auth(request)
    s = get_settings()
    # never expose the raw password
    out = {k: v for k, v in s.items() if k not in ("admin_password", "password_salt")}
    out["login_enabled"] = bool(s.get("admin_password"))
    out["admin_email"] = s.get("admin_email", "") or ""
    out["create_defaults"] = s.get("create_defaults", {}) or {}
    return out


@app.get("/api/clones")
def api_clones(request: Request):
    """List the user's Pocket TTS voice clones (from the voices dir)."""
    require_auth(request)
    d = POCKET_VOICES_DIR
    clones = []
    if d.exists():
        clones = sorted(f.stem for f in d.glob("*.safetensors"))
    return {"clones": clones}


@app.post("/api/clones/{name}/rename")
def api_clone_rename(name: str, new_name: str, request: Request):
    """Rename a saved Pocket TTS voice clone (.safetensors)."""
    require_auth(request)
    voices_dir = POCKET_VOICES_DIR
    clean = lambda s: re.sub(r"[^A-Za-z0-9 _\-]+", "", (s or "").strip()).strip()
    src_name = clean(name)
    dst_name = clean(new_name)
    if not dst_name:
        raise HTTPException(400, "New name is empty or invalid")
    if dst_name == src_name:
        return {"ok": True}
    src = voices_dir / f"{src_name}.safetensors"
    dst = voices_dir / f"{dst_name}.safetensors"
    if not src.exists():
        raise HTTPException(404, f"Clone '{src_name}' not found")
    if dst.exists():
        raise HTTPException(400, f"A clone named '{dst_name}' already exists")
    src.rename(dst)
    clones = sorted(f.stem for f in voices_dir.glob("*.safetensors"))
    return {"ok": True, "renamed": [src_name, dst_name], "clones": clones}


# ---------------------------------------------------------------------------
# Edge voices (all male en-US / en-GB) + Pocket voice cloning
# ---------------------------------------------------------------------------
_EDGE_VOICES_CACHE: dict = {"at": 0, "voices": []}

def _edge_male_us_uk() -> list[dict]:
    global _EDGE_VOICES_CACHE
    if time.time() - _EDGE_VOICES_CACHE["at"] < 21600 and _EDGE_VOICES_CACHE["voices"]:
        return _EDGE_VOICES_CACHE["voices"]
    import edge_tts, asyncio
    try:
        raw = asyncio.run(edge_tts.list_voices())
        out = []
        for v in raw:
            loc = v.get("Locale", "")
            gender = (v.get("Gender") or "").lower()
            if loc in ("en-US", "en-GB") and gender == "male":
                out.append({"id": v["ShortName"], "name": v.get("FriendlyName", v["ShortName"]), "locale": loc})
        out.sort(key=lambda x: (x["locale"], x["name"]))
        _EDGE_VOICES_CACHE = {"at": time.time(), "voices": out}
        return out
    except Exception as e:
        return _EDGE_VOICES_CACHE["voices"]


@app.get("/api/voices")
def api_voices(request: Request):
    require_auth(request)
    return {"voices": _edge_male_us_uk()}


@app.get("/api/voice/preview")
def api_voice_preview(request: Request, provider: str = "edge", voice_id: str = ""):
    """Synthesize a short sample with the given provider+voice and return the audio."""
    require_auth(request)
    text = request.query_params.get("text", "Hello! This is a sample of the voice you selected.")
    r = subprocess.run(
        [str(PYTHON), str(THEAUTOMAN_DIR / "voice_preview.py"), provider, voice_id, text],
        capture_output=True, text=True, timeout=180, cwd=str(THEAUTOMAN_DIR),
    )
    out_line = ((r.stdout or "").strip().splitlines() or [""])[-1]
    if r.returncode != 0 or not out_line or not Path(out_line).exists():
        err = (r.stderr or "").strip()[-300:]
        raise HTTPException(400, f"Voice preview failed: {err or 'unknown error'}")
    ext = Path(out_line).suffix.lstrip(".").lower()
    media = "audio/wav" if ext == "wav" else "audio/mpeg"
    return FileResponse(out_line, media_type=media)


ENGINE_CONFIG_PATH = Path.home() / ".config" / "AutoSceneStudio" / "settings.json"


@app.get("/api/fish/config")
def api_fish_config(request: Request):
    """Report whether a Fish Audio key is set and list available Fish voices."""
    require_auth(request)
    r = subprocess.run([str(PYTHON), str(THEAUTOMAN_DIR / "fish_info.py")],
                       capture_output=True, text=True, timeout=60, cwd=str(THEAUTOMAN_DIR))
    try:
        data = json.loads(r.stdout)
    except Exception:
        data = {"key_set": False, "voices": [], "error": (r.stderr or "")[-200:]}
    data["has_key"] = bool(data.get("key_set", False))
    return data


@app.put("/api/fish/config")
def api_fish_set_key(body: dict, request: Request):
    """Save the Fish Audio API key into the engine config (persists to disk)."""
    require_auth(request)
    key = (body or {}).get("api_key", "").strip()
    path = ENGINE_CONFIG_PATH
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["fish_audio_api_key"] = key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"saved": bool(key)}


@app.post("/api/clone")
async def api_clone_voice(request: Request, name: str = Form(...), file: UploadFile = File(...)):
    """Clone a voice from an uploaded sample and save it for reuse."""
    require_auth(request)
    voice_name = re.sub(r"[^A-Za-z0-9 _\-]+", "", (name or "").strip()).strip() or "clone"
    voices_dir = POCKET_VOICES_DIR
    voices_dir.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.gettempdir()) / f"clone_src_{uuid.uuid4().hex[:8]}.wav"
    with tmp.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
    # ensure it's a mono 16k wav
    wav16k = Path(tempfile.gettempdir()) / f"clone_wav_{uuid.uuid4().hex[:8]}.wav"
    conv = subprocess.run([_FFMPEG, "-v", "error", "-y", "-i", str(tmp),
                           "-ac", "1", "-ar", "16000", str(wav16k)],
                          capture_output=True, text=True, timeout=120)
    out_path = voices_dir / f"{voice_name}.safetensors"
    cli = os.environ.get("POCKET_TTS_CLI", "/home/ubuntu/.local/bin/pocket-tts")
    exp = subprocess.run([cli, "export-voice", str(wav16k), str(out_path), "--quiet"],
                         capture_output=True, text=True, timeout=300)
    try:
        tmp.unlink(missing_ok=True); wav16k.unlink(missing_ok=True)
    except Exception:
        pass
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise HTTPException(500, f"Clone failed: {(exp.stderr or '')[-300:]}")
    return {"name": voice_name, "filename": out_path.name,
            "clones": sorted(f.stem for f in voices_dir.glob("*.safetensors"))}


# ---------------------------------------------------------------------------
# Color filters (3D LUTs)
# ---------------------------------------------------------------------------
@app.get("/api/luts")
def api_luts(request: Request):
    """List available color filters (.cube LUTs)."""
    require_auth(request)
    luts = sorted(f.stem for f in LUTS_DIR.glob("*.cube"))
    return {"luts": luts}


@app.post("/api/luts/upload")
async def api_lut_upload(file: UploadFile = File(...), request: Request = None):
    require_auth(request)
    name = Path(file.filename or "filter").stem
    # sanitize name
    name = re.sub(r"[^A-Za-z0-9 _\-]+", "", name).strip() or "filter"
    dest = LUTS_DIR / f"{name}.cube"
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
    return {"saved": dest.name, "luts": sorted(f.stem for f in LUTS_DIR.glob("*.cube"))}


# ---------------------------------------------------------------------------
# Background music
# ---------------------------------------------------------------------------
@app.get("/api/music")
def api_music(request: Request):
    """List uploaded background-music tracks."""
    require_auth(request)
    tracks = []
    for f in sorted(MUSIC_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in (".mp3", ".wav", ".m4a", ".aac", ".ogg"):
            tracks.append({"name": f.stem, "filename": f.name, "size": f.stat().st_size})
    return {"music": tracks}


@app.post("/api/music/upload")
async def api_music_upload(file: UploadFile = File(...), request: Request = None):
    require_auth(request)
    suffix = Path(file.filename or ".mp3").suffix.lower()
    if suffix not in (".mp3", ".wav", ".m4a", ".aac", ".ogg"):
        raise HTTPException(400, "Use an audio file: mp3/wav/m4a/aac/ogg")
    stem = re.sub(r"[^A-Za-z0-9 _\-]+", "", Path(file.filename or "music").stem).strip() or "music"
    dest = MUSIC_DIR / f"{stem}{suffix}"
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
    return {"saved": dest.name, "name": dest.stem, "music": api_music_list()}


def api_music_list() -> list:
    return [f.stem for f in MUSIC_DIR.iterdir()
            if f.is_file() and f.suffix.lower() in (".mp3", ".wav", ".m4a", ".aac", ".ogg")]


# ---------------------------------------------------------------------------
# Presets + create defaults
# ---------------------------------------------------------------------------
def _create_settings_dict(voice_provider, voice_id, color_filter, intensity,
                          brightness, contrast, saturation, warmth,
                          music_name, music_volume, mute_original, music_loop,
                          resolution, quality, transition) -> dict:
    return {
        "voice_provider": voice_provider, "voice_id": voice_id,
        "color_filter": color_filter or "",
        "intensity": intensity, "brightness": brightness,
        "contrast": contrast, "saturation": saturation, "warmth": warmth,
        "music": music_name or "", "music_volume": music_volume,
        "mute_original": bool(mute_original), "music_loop": bool(music_loop),
        "resolution": resolution, "quality": quality, "transition": transition,
    }


def _save_create_defaults(s: dict) -> None:
    cur = get_settings()
    cur["create_defaults"] = s
    _save_settings(cur)


@app.get("/api/presets")
def api_get_presets(request: Request):
    require_auth(request)
    presets = _load_presets()
    default = presets.pop("_default", "") or ""
    return {
        "default": default,
        "presets": [{"name": k, "is_default": (k == default), **v} for k, v in presets.items()],
    }


@app.post("/api/presets")
def api_save_preset(body: dict, request: Request):
    require_auth(request)
    name = str((body or {}).get("name", "")).strip() or "preset"
    settings = (body or {}).get("settings") or {}
    presets = _load_presets()
    presets[name] = settings
    # keep _default intact (it's just a name)
    PRESETS_PATH.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "name": name}


@app.put("/api/presets/{name}/default")
def api_set_default_preset(name: str, request: Request):
    require_auth(request)
    presets = _load_presets()
    if name not in presets:
        raise HTTPException(404, "Preset not found")
    presets["_default"] = name
    PRESETS_PATH.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "default": name}


@app.delete("/api/presets/{name}")
def api_delete_preset(name: str, request: Request):
    require_auth(request)
    presets = _load_presets()
    if name in presets:
        del presets[name]
        if presets.get("_default") == name:
            presets["_default"] = ""
        PRESETS_PATH.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Quick color preview (render a single frame with current filter + adjustments)
# ---------------------------------------------------------------------------
def _load_color_filter():
    """Load the engine's color_filter module without the `app` package clash."""
    import importlib.util
    path = THEAUTOMAN_DIR / "app" / "ffmpeg" / "color_filter.py"
    spec = importlib.util.spec_from_file_location("_color_filter_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@app.post("/api/preview")
async def preview(
    request: Request,
    color_filter: str = Form(""),
    intensity: float = Form(1.0),
    brightness: float = Form(0.0),
    contrast: float = Form(1.0),
    saturation: float = Form(1.0),
    warmth: float = Form(0.0),
    music_name: str = Form(""),
    music_volume: float = Form(0.3),
    mute_original: int = Form(0),
    music_loop: int = Form(1),
    clip: int = Form(0),          # 1 = short mp4 clip (with audio), 0 = still JPEG
    file: UploadFile = File(default=None),
):
    require_auth(request)
    cf = _load_color_filter()
    chain = cf.build_color_chain(
        color_filter or None,
        intensity=intensity, brightness=brightness,
        contrast=contrast, saturation=saturation, warmth=warmth,
    )

    # source: uploaded file, else first staged file
    src: Path | None = None
    if file and file.filename:
        tmp = Path(tempfile.gettempdir()) / "automan_preview_src"
        tmp.mkdir(parents=True, exist_ok=True)
        src = tmp / f"src_{uuid.uuid4().hex[:8]}{Path(file.filename).suffix or '.png'}"
        with src.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
    else:
        staged = sorted([f for f in STAGE_DIR.iterdir() if f.is_file()],
                        key=lambda p: int(re_search_num(p.name)))
        if staged:
            src = staged[0]
    if not src:
        if clip:
            raise HTTPException(400, "To preview a clip (with music), pick your clips first — the clip preview needs real footage.")
        src = BASE_DIR / "static" / "demo_preview.jpg"
        if not src.exists():
            raise HTTPException(400, "No source clip/image to preview. Upload one or pick your clips.")

    music_path = None
    if music_name:
        for cand in (MUSIC_DIR / music_name, MUSIC_DIR / f"{music_name}.mp3",
                     MUSIC_DIR / f"{music_name}.wav"):
            if cand.exists():
                music_path = cand
                break
        if music_path is None:
            for f in MUSIC_DIR.iterdir():
                if f.is_file() and f.stem.lower() == music_name.lower():
                    music_path = f
                    break

    if clip:
        out_mp4 = Path(tempfile.gettempdir()) / f"preview_{uuid.uuid4().hex[:8]}.mp4"
        _render_clip_preview(src, music_path, music_volume, bool(mute_original), bool(music_loop), chain, out_mp4)
        return Response(content=out_mp4.read_bytes(), media_type="video/mp4")

    out_jpg = Path(tempfile.gettempdir()) / f"preview_{uuid.uuid4().hex[:8]}.jpg"
    vf = chain if chain else "null"
    probe = subprocess.run([_FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
                           capture_output=True, text=True, timeout=20)
    seek_args = []
    is_img = src.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif")
    if probe.returncode == 0 and probe.stdout.strip() and not is_img:
        try:
            dur = float(probe.stdout.strip())
            if dur > 1.0:  # only seek real footage; seeking a still image skips its only frame
                seek_args = ["-ss", str(max(0.0, dur / 2.0))]
        except ValueError:
            pass
    cmd = [_FFMPEG, "-v", "error", "-y", *seek_args, "-i", str(src),
           "-vf", f"{vf},scale='min(960,iw)':-2", "-frames:v", "1", "-q:v", "3", str(out_jpg)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if not out_jpg.exists() or out_jpg.stat().st_size == 0:
        raise HTTPException(500, f"Preview failed: {r.stderr[-300:]}")
    return Response(content=out_jpg.read_bytes(), media_type="image/jpeg")


def _render_clip_preview(src: Path, music_path, music_volume, mute_bg, music_loop, chain, out: Path):
    """Render a ~8s preview clip with color chain + music mix + mute."""
    is_image = src.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
    # does the source have an audio stream?
    has_orig_audio = False
    if not is_image:
        probe = subprocess.run([_FFPROBE, "-v", "error", "-select_streams", "a",
                                "-show_entries", "stream=index",
                                "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
                               capture_output=True, text=True, timeout=20)
        has_orig_audio = bool(probe.stdout.strip())
    has_music = bool(music_path)

    inputs = []
    if is_image:
        inputs = ["-loop", "1", "-i", str(src)]
    else:
        inputs = ["-i", str(src)]
    if has_music:
        if music_loop:
            inputs += ["-stream_loop", "-1"]
        inputs += ["-i", str(music_path)]

    vf = (chain + "," if chain else "") + "scale=1280:-2,format=yuv420p"
    # audio chain
    labels, mix = [], []
    if has_orig_audio and not mute_bg:
        labels.append(f"[0:a]volume=0.15[orig]"); mix.append("[orig]")
    if has_music:
        labels.append(f"[1:a]volume={music_volume:.3f}[music]"); mix.append("[music]")

    if mix:
        if len(mix) == 1:
            af = labels[0].replace("[music]", "[a]").replace("[orig]", "[a]")
        else:
            af = ";".join(labels) + f";{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0[a]"
        cmd = [_FFMPEG] + inputs + ["-filter_complex", f"{vf}[v];{af}",
                        "-map", "[v]", "-map", "[a]", "-t", "8",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                        "-c:a", "aac", "-b:a", "128k", "-shortest", str(out)]
    else:
        # no audio at all
        cmd = [_FFMPEG] + inputs + ["-vf", vf, "-an", "-t", "8",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", str(out)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if not out.exists() or out.stat().st_size == 0:
        raise HTTPException(500, f"Clip preview failed: {(res.stderr or '')[-300:]}")


@app.put("/api/settings")
def api_save_settings(body: dict, request: Request):
    require_auth(request)
    cur = get_settings()
    for k in ("retention_days", "cleanup_enabled", "cleanup_every_hours",
              "default_provider", "default_voice", "session_hours", "admin_email",
              "auto_apply_default_preset"):
        if k in body and body[k] is not None:
            cur[k] = body[k]
    if "create_defaults" in body and isinstance(body.get("create_defaults"), dict):
        cur["create_defaults"] = body["create_defaults"]
    # changing/clearing the password
    if "new_password" in body and body.get("new_password"):
        cur["admin_password"] = body["new_password"]
        cur["password_salt"] = secrets.token_hex(8)
    elif "admin_password" in body and not body.get("admin_password"):
        cur["admin_password"] = ""
    _save_settings(cur)
    return {k: v for k, v in cur.items() if k not in ("admin_password", "password_salt")}


# ---------------------------------------------------------------------------
# Cleanup / retention
# ---------------------------------------------------------------------------
def cleanup_old_projects(settings: dict | None = None):
    settings = settings or get_settings()
    if not settings.get("cleanup_enabled"):
        return {"removed": 0, "skipped": "cleanup disabled"}
    retention = max(1, int(settings.get("retention_days", 7)))
    cutoff = time.time() - retention * 86400
    removed = 0
    for p in UPLOAD_DIR.iterdir():
        if p.is_dir() and p.name != "images" and p.stat().st_mtime < cutoff:
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    # stale staged files older than retention
    for f in STAGE_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
    return {"removed": removed, "retention_days": retention}


@app.post("/api/cleanup")
def api_cleanup(request: Request):
    require_auth(request)
    return cleanup_old_projects()


@app.get("/api/storage")
def api_storage(request: Request):
    require_auth(request)
    total, count = 0, 0
    for p in UPLOAD_DIR.iterdir():
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
            count += 1
    return {"projects": count, "bytes": total, "retention_days": get_settings().get("retention_days", 7)}


def _run_cleanup_loop(settings_path: Path = SETTINGS_PATH):
    while True:
        try:
            cleanup_old_projects()
        except Exception as e:
            print(f"cleanup error: {e}", flush=True)
        try:
            every = max(0.25, float(get_settings().get("cleanup_every_hours", 1)))
        except Exception:
            every = 1.0
        time.sleep(every * 3600)

_cleanup_thread = threading.Thread(target=_run_cleanup_loop, daemon=True)
_cleanup_thread.start()


# ---------------------------------------------------------------------------
# Stage upload: one clip at a time, so the browser can show per-file progress
# ---------------------------------------------------------------------------
@app.post("/api/stage")
async def stage_upload(file: UploadFile = File(...), request: Request = None):
    """Upload a single clip into the staging area. Returns its index + filename."""
    require_auth(request)
    ext = Path(file.filename or "clip").suffix.lower()
    if ext not in ALLOWED_MEDIA:
        raise HTTPException(400, f"Unsupported file type: {ext}. Use mp4/mov/mkv/avi/webm/jpg/png.")
    # Derive the slot from the clip's own embedded number (001, 2, 10) so a
    # retry overwrites the same slot instead of creating a duplicate.
    idx = re_search_num(file.filename or "")
    if idx <= 0:
        idx = len([p for p in STAGE_DIR.iterdir() if p.is_file()]) + 1
    dest = STAGE_DIR / f"{idx:03d}{ext}"
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
    return {"index": idx, "name": dest.name, "size": dest.stat().st_size}


@app.get("/api/stage")
def stage_list(request: Request):
    """List currently staged clips, sorted numerically."""
    require_auth(request)
    import re
    files = sorted(STAGE_DIR.iterdir(), key=lambda p: int(re.search(r"\d+", p.name).group() or 0) if re.search(r"\d+", p.name) else 0)
    return {"files": [{"name": f.name, "size": f.stat().st_size} for f in files if f.is_file()]}


@app.delete("/api/stage")
def stage_clear(request: Request):
    """Clear the staging area (e.g. after a render, or to start over)."""
    require_auth(request)
    for f in STAGE_DIR.iterdir():
        if f.is_file():
            f.unlink(missing_ok=True)
    return {"cleared": True}


@app.get("/api/clips/download")
def clips_download(request: Request):
    """Zip all currently staged clips and download them as one .zip."""
    require_auth(request)
    clips = sorted([p for p in STAGE_DIR.iterdir() if p.is_file()],
                   key=lambda p: int(re_search_num(p.name)))
    if not clips:
        raise HTTPException(400, "No staged clips to download.")
    import zipfile
    zpath = Path(tempfile.gettempdir()) / f"automan_clips_{uuid.uuid4().hex[:8]}.zip"
    # ZIP_STORED: video is already compressed, don't waste CPU re-compressing it.
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
        for p in clips:
            zf.write(p, arcname=f"clips/{p.name}")
    return FileResponse(zpath, media_type="application/zip",
                        filename=f"automan_clips_{len(clips)}.zip")


# ---------------------------------------------------------------------------
# Create a project: script beats + uploaded media
# ---------------------------------------------------------------------------
@app.post("/api/projects")
async def create_project(
    request: Request,
    title: str = Form(...),
    script: str = Form(...),
    voice_provider: str = Form("edge"),
    voice_id: str = Form("en-US-GuyNeural"),
    aspect_ratio: str = Form("16:9"),
    transition: str = Form("fade"),
    resolution: str = Form("1920x1080"),
    quality: str = Form("high"),
    color_filter: str = Form(""),
    intensity: float = Form(1.0),
    brightness: float = Form(0.0),
    contrast: float = Form(1.0),
    saturation: float = Form(1.0),
    warmth: float = Form(0.0),
    music_name: str = Form(""),
    music_volume: float = Form(0.3),
    mute_original: int = Form(0),
    music_loop: int = Form(1),
    files: list[UploadFile] = File(default=[]),
):
    """Create a project from pasted beats and uploaded clips/images.

    `script` is free text with one narration line per line. Line i pairs
    with uploaded file i (in order).
    """
    require_auth(request)
    project_id = uuid.uuid4().hex[:12]
    pdir = UPLOAD_DIR / project_id / "images"
    pdir.mkdir(parents=True, exist_ok=True)

    # Parse beats (each non-empty line = one scene's narration).
    # Strip any leading "Beat N:" / numbered label so it's never spoken by TTS,
    # and drop lines that had nothing but a label.
    beats = [b for b in (clean_beat(ln) for ln in script.splitlines()) if b]
    if not beats:
        raise HTTPException(400, "Script is empty — paste at least one narration line.")

    media_saved = []
    # Prefer staged files (uploaded via /api/stage for progress). If none,
    # fall back to direct multipart upload (legacy path).
    staged = sorted([f for f in STAGE_DIR.iterdir() if f.is_file()],
                    key=lambda p: int(re_search_num(p.name)))
    if staged:
        for i, src in enumerate(staged[: len(beats)], start=1):
            ext = src.suffix.lower()
            dest = pdir / f"{i}{ext}"
            dest.write_bytes(src.read_bytes())
            media_saved.append(dest.name)
        # Clear staging after consuming
        for f in STAGE_DIR.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
    else:
        def _numkey(f: UploadFile) -> int:
            m = re_search_num(f.filename or "")
            return m
        for i, f in enumerate(sorted(files, key=_numkey), start=1):
            ext = Path(f.filename or f"file{i}").suffix.lower()
            if ext not in ALLOWED_MEDIA:
                continue
            dest = pdir / f"{i}{ext}"
            with dest.open("wb") as out:
                while chunk := await f.read(1024 * 1024):
                    out.write(chunk)
            media_saved.append(dest.name)
            if len(media_saved) == len(beats):
                break  # only need as many media as beats

    if not media_saved:
        # No media uploaded — build project with empty image refs and let
        # validation catch it; but better to error now with a clear message.
        raise HTTPException(400, "No video/image files received. Upload at least one clip.")

    if len(media_saved) < len(beats):
        raise HTTPException(
            400,
            f"You have {len(beats)} narration lines but only {len(media_saved)} media files. "
            "Each beat needs its own clip/image.",
        )

    scenes = [
        {"image": media_saved[i], "script": beats[i]}
        for i in range(len(beats))
    ]

    project = {
        "title": title or "Untitled",
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "fps": 30,
        "transition": transition,
        "transition_duration": 0.4,
        "quality": quality,
        "color_filter": color_filter or None,
        "filter_intensity": intensity,
        "brightness": brightness,
        "contrast": contrast,
        "saturation": saturation,
        "warmth": warmth,
        "music_name": music_name or None,
        "music_volume": music_volume,
        "mute_original": bool(mute_original),
        "music_loop": bool(music_loop),
        "voice": {"provider": voice_provider, "voice_id": voice_id},
        "scenes": scenes,
    }

    (UPLOAD_DIR / project_id / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # remember these settings as the default for next time
    _save_create_defaults(_create_settings_dict(
        voice_provider, voice_id, color_filter, intensity, brightness,
        contrast, saturation, warmth, music_name, music_volume,
        mute_original, music_loop, resolution, quality, transition))

    return {"project_id": project_id, "beats": len(beats), "media": media_saved}


# ---------------------------------------------------------------------------
# Batch: paste multiple documentaries at once, queue them all
# ---------------------------------------------------------------------------
def _build_project(project_id: str, title: str, beats: list[str],
                   voice_provider: str, voice_id: str,
                   resolution: str, quality: str, transition: str,
                   media_paths: list[Path], color_filter: str = "",
                   intensity: float = 1.0, brightness: float = 0.0,
                   contrast: float = 1.0, saturation: float = 1.0,
                   warmth: float = 0.0,
                   music_name: str = "", music_volume: float = 0.3,
                   mute_original: int = 0, music_loop: int = 1) -> int:
    """Create a project.json + copy its clips. Returns media file count."""
    pdir = UPLOAD_DIR / project_id / "images"
    pdir.mkdir(parents=True, exist_ok=True)
    media_saved = []
    for i, src in enumerate(media_paths, start=1):
        ext = src.suffix.lower()
        dest = pdir / f"{i}{ext}"
        dest.write_bytes(src.read_bytes())
        media_saved.append(dest.name)
    scenes = [{"image": media_saved[i], "script": beats[i]} for i in range(len(beats))]
    project = {
        "title": title or "Untitled",
        "aspect_ratio": "16:9",
        "resolution": resolution,
        "fps": 30,
        "transition": transition,
        "transition_duration": 0.4,
        "quality": quality,
        "color_filter": color_filter or None,
        "filter_intensity": intensity,
        "brightness": brightness,
        "contrast": contrast,
        "saturation": saturation,
        "warmth": warmth,
        "music_name": music_name or None,
        "music_volume": music_volume,
        "mute_original": bool(mute_original),
        "music_loop": bool(music_loop),
        "voice": {"provider": voice_provider, "voice_id": voice_id},
        "scenes": scenes,
    }
    (UPLOAD_DIR / project_id / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(media_saved)


_DOC_HEADER_RE = re.compile(r"^\s*---+|\s*---+\s*$")

def _parse_documents(text: str) -> list[dict]:
    """Split pasted text into documents.

    Each document is introduced by a line like:
        --- Documentary Title ---
    (or just '---' for an untitled doc). Lines after it up to the next header
    are that documentary's beats. A blank line inside is ignored.
    """
    docs = []
    cur_title, cur_lines = None, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("---"):
            if cur_title is not None or cur_lines:
                docs.append({"title": (cur_title or f"Documentary {len(docs)+1}"),
                             "lines": cur_lines})
            m = re.match(r"^---+\s*(.*?)\s*---+\s*$", line)
            cur_title = (m.group(1).strip() if m and m.group(1).strip() else None)
            cur_lines = []
        else:
            cur_lines.append(line)
    if cur_title is not None or cur_lines:
        docs.append({"title": (cur_title or f"Documentary {len(docs)+1}"), "lines": cur_lines})
    return docs


@app.post("/api/batch")
async def create_batch(
    request: Request,
    script: str = Form(...),
    voice_provider: str = Form("edge"),
    voice_id: str = Form("en-US-GuyNeural"),
    resolution: str = Form("1920x1080"),
    quality: str = Form("high"),
    transition: str = Form("fade"),
    color_filter: str = Form(""),
    intensity: float = Form(1.0),
    brightness: float = Form(0.0),
    contrast: float = Form(1.0),
    saturation: float = Form(1.0),
    warmth: float = Form(0.0),
    music_name: str = Form(""),
    music_volume: float = Form(0.3),
    mute_original: int = Form(0),
    music_loop: int = Form(1),
    source: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    """Create + queue MULTIPLE documentaries from one paste.

    Script format: each documentary starts with a header line
    `--- Title ---`, followed by one narration line per beat.
    Clips are uploaded in order across all docs; they're split by each
    doc's beat count. Each doc is queued and renders sequentially.
    """
    require_auth(request)
    docs = _parse_documents(script)
    if not docs:
        raise HTTPException(400, "No documentaries found. Use a `--- Title ---` header per documentary.")

    # Gather media: prefer multipart files when source=files (grouped per-doc),
    # otherwise staged files, else multipart.
    use_files = source == "files"
    staged = [] if use_files else sorted(
        [f for f in STAGE_DIR.iterdir() if f.is_file()],
        key=lambda p: int(re_search_num(p.name)))
    media_files: list[Path] = []
    if staged:
        for f in staged:
            media_files.append(f)
    else:
        for f in sorted(files, key=lambda f: re_search_num(f.filename or "")):
            if Path(f.filename or "").suffix.lower() in ALLOWED_MEDIA:
                media_files.append(Path(f.filename))  # placeholder, see below

    total_beats = sum(len(d["lines"]) for d in docs)
    if len(media_files) < total_beats:
        raise HTTPException(400, f"You have {total_beats} beats across {len(docs)} documentaries but only {len(media_files)} clips. Each beat needs a clip.")

    # Build media path list (staged files are real paths; multipart need writing)
    real_paths: list[Path] = []
    if staged:
        real_paths = media_files
    else:
        # write multipart uploads to a temp staging dir in order
        tmp = STAGE_DIR
        real_paths = []
        for i, f in enumerate(files, start=1):
            if Path(f.filename or "").suffix.lower() not in ALLOWED_MEDIA:
                continue
            ext = Path(f.filename or f"clip{i}").suffix.lower()
            dest = tmp / f"batch_{i}{ext}"
            with dest.open("wb") as out:
                while chunk := await f.read(1024 * 1024):
                    out.write(chunk)
            real_paths.append(dest)

    # Partition clips per document, create + queue each
    results = []
    ptr = 0
    for doc in docs:
        beats = [b for b in (clean_beat(ln) for ln in doc["lines"]) if b]
        if not beats:
            raise HTTPException(400, f"Documentary '{doc['title']}' has no narration lines.")
        n = len(beats)
        clip_slice = real_paths[ptr:ptr + n]
        ptr += n
        pid = uuid.uuid4().hex[:12]
        _build_project(pid, doc["title"], beats, voice_provider, voice_id,
                       resolution, quality, transition, clip_slice, color_filter,
                       intensity, brightness, contrast, saturation, warmth,
                       music_name, music_volume, mute_original, music_loop)
        # queue a render
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {
            "id": job_id, "project_id": pid, "status": "queued", "progress": 0,
            "scenes_total": n, "scenes_done": 0, "stage": "queued",
            "output": None, "error": None, "log": [], "started": time.time(),
            "queue_pos": None,
        }
        _enqueue(job_id)
        results.append({"title": doc["title"], "project_id": pid, "job_id": job_id, "beats": n})

    # clean up batch staging temps (non-staged path)
    if not staged:
        for f in STAGE_DIR.iterdir():
            if f.name.startswith("batch_"):
                f.unlink(missing_ok=True)

    # remember these settings as the default for next time
    _save_create_defaults(_create_settings_dict(
        voice_provider, voice_id, color_filter, intensity, brightness,
        contrast, saturation, warmth, music_name, music_volume,
        mute_original, music_loop, resolution, quality, transition))

    return {"documents": len(results), "total_beats": total_beats, "jobs": results}


# ---------------------------------------------------------------------------
# Render a project (background)
# ---------------------------------------------------------------------------
@app.post("/api/projects/{project_id}/render")
async def start_render(project_id: str, request: Request):
    require_auth(request)
    pdir = UPLOAD_DIR / project_id
    proj_json = pdir / "project.json"
    if not proj_json.exists():
        raise HTTPException(404, "Project not found.")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "status": "queued",
        "progress": 0,
        "scenes_total": None,
        "scenes_done": 0,
        "stage": "queued",
        "output": None,
        "error": None,
        "log": [],
        "started": time.time(),
        "queue_pos": None,
    }
    _enqueue(job_id)
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# Render queue (batch): jobs render one after another, FIFO
# ---------------------------------------------------------------------------
QUEUE: list[str] = []
_queue_lock = threading.Lock()
_queue_ready = threading.Event()

def _enqueue(job_id: str) -> None:
    with _queue_lock:
        QUEUE.append(job_id)
        for i, jid in enumerate(QUEUE):
            if jid in JOBS:
                JOBS[jid]["queue_pos"] = i + 1
    _queue_ready.set()

def _next_job() -> str | None:
    with _queue_lock:
        if not QUEUE:
            return None
        jid = QUEUE.pop(0)
        if jid in JOBS:
            JOBS[jid]["queue_pos"] = None
        for i, q in enumerate(QUEUE):
            if q in JOBS:
                JOBS[q]["queue_pos"] = i + 1
        return jid

def _batch_worker() -> None:
    while True:
        _queue_ready.wait()
        _queue_ready.clear()
        time.sleep(0.2)
        jid = _next_job()
        if jid and jid in JOBS:
            try:
                _run_render(jid, JOBS[jid]["project_id"])
            except Exception as e:
                JOBS[jid]["status"] = "failed"
                JOBS[jid]["stage"] = "failed"
                JOBS[jid]["error"] = str(e)

_batch_thread = threading.Thread(target=_batch_worker, daemon=True)
_batch_thread.start()


def _parse_progress(j):
    """Update per-scene progress from the live log file."""
    lp = j.get("log_file")
    if not lp or not Path(lp).exists():
        return
    total, done = None, 0
    try:
        txt = Path(lp).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return
    total_m = re.search(r"Rendering Scene (\d+)/(\d+)", txt)
    if total_m:
        done, total = int(total_m.group(1)), int(total_m.group(2))
    # count finished scene clips
    m_done = len(re.findall(r"Rendering Scene \d+/\d+", txt))
    if total and done:
        j["scenes_total"] = total
        j["scenes_done"] = done
        j["progress"] = round(100 * done / total, 1)
        j["stage"] = f"rendering scene {done}/{total}"
    # export/merge phase — track the chunked merge so the bar keeps moving
    # (previously it sat at 100% the whole time, looking frozen).
    if "Merging" in txt or "Export" in txt or "Chunk" in txt or "xfade merged" in txt:
        cs_m = re.search(r"chunk_size=(\d+)", txt)
        chunk_size = int(cs_m.group(1)) if cs_m else 15
        chunks_done = len(re.findall(r"xfade merged \d+ clips → _chunk_", txt))
        total_scenes = total or j.get("scenes_total") or 0
        if total_scenes and chunk_size:
            total_chunks = -(-total_scenes // chunk_size)  # ceil division
            j["stage"] = f"merging chunk {chunks_done}/{total_chunks}"
            if chunks_done >= total_chunks:
                j["progress"] = 95  # all chunks merged, final assembly left
            else:
                j["progress"] = round(100 * chunks_done / total_chunks, 1)
        elif chunks_done:
            j["progress"] = 90
            j["stage"] = "merging / exporting"
    if "final_video.mp4" in txt and j.get("status") == "rendering":
        j["stage"] = "finalizing"
        j["progress"] = 99


def _run_render(job_id: str, project_id: str):
    j = _job(job_id)
    j["status"] = "rendering"
    j["stage"] = "starting"
    cmd = [
        str(PYTHON),
        str(MAIN_PY),
        "render",
        str(UPLOAD_DIR / project_id / "project.json"),
    ]
    log_path = JOBS_DIR / f"{job_id}.log"
    j["log_file"] = str(log_path)
    j["log"].append("Starting render...")
    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd, stdout=lf, stderr=subprocess.STDOUT,
                text=True, cwd=str(THEAUTOMAN_DIR),
            )
            proc.wait(timeout=7200)
        _parse_progress(j)
        j["log"].extend(_tail_log(log_path, 80))
        if proc.returncode != 0:
            j["status"] = "failed"
            j["error"] = _tail_log(log_path, 25) or f"render exit {proc.returncode}"
            j["stage"] = "failed"
            return
        out = _find_output(UPLOAD_DIR / project_id)
        if not out:
            j["status"] = "failed"
            j["error"] = "Render finished but no final_video.mp4 was produced."
            j["stage"] = "failed"
            return
        j["status"] = "done"
        j["progress"] = 100
        j["stage"] = "done"
        j["output"] = str(out)
        dv = _save_to_drive(out)
        if dv:
            j["drive"] = dv
            j["log"].append(f"✅ Saved to Drive: {dv}")
    except Exception as e:
        j["status"] = "failed"
        j["error"] = str(e)
        j["stage"] = "failed"


def _tail_log(path: Path, n: int) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def _find_output(pdir: Path) -> Path | None:
    for cand in (pdir / "output").rglob("*.mp4"):
        return cand
    return None


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    require_auth(request)
    j = _job(job_id)
    if j.get("status") == "rendering":
        _parse_progress(j)
    return j


@app.get("/api/jobs")
def list_jobs(request: Request):
    """List all jobs (most recent first) for the My Renders panel."""
    require_auth(request)
    items = sorted(JOBS.values(), key=lambda j: j.get("started", 0), reverse=True)
    out = []
    for j in items:
        if j.get("status") == "rendering":
            _parse_progress(j)
        out.append({
            "id": j["id"],
            "project_id": j.get("project_id"),
            "status": j.get("status"),
            "progress": j.get("progress", 0),
            "stage": j.get("stage"),
            "queue_pos": j.get("queue_pos"),
            "started": j.get("started"),
            "error": j.get("error"),
        })
    return out


@app.post("/api/queue/clear")
def clear_queue(request: Request):
    """Cancel all queued (not yet started) renders."""
    require_auth(request)
    removed = 0
    with _queue_lock:
        for jid in list(QUEUE):
            if jid in JOBS and JOBS[jid]["status"] == "queued":
                JOBS[jid]["status"] = "cancelled"
                JOBS[jid]["stage"] = "cancelled"
                removed += 1
        QUEUE.clear()
    return {"cancelled": removed}


# ---------------------------------------------------------------------------
# Completed-renders library (persists on disk, survives restart)
# ---------------------------------------------------------------------------
@app.get("/api/library")
def library(request: Request):
    """List every finished render sitting on the server, newest first."""
    require_auth(request)
    items = []
    for pdir in UPLOAD_DIR.iterdir():
        if not pdir.is_dir() or pdir.name == "images":
            continue
        out = _find_output(pdir)
        if not out:
            continue
        title = "Untitled"
        try:
            pj = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
            title = pj.get("title", "Untitled")
        except Exception:
            pass
        items.append({
            "project_id": pdir.name,
            "title": title,
            "filename": out.name,
            "size": out.stat().st_size,
            "mtime": out.stat().st_mtime,
            "download": f"/api/projects/{pdir.name}/download",
        })
    items.sort(key=lambda i: i.get("mtime", 0), reverse=True)
    return items


@app.get("/api/jobs/{job_id}/log")
def get_job_log(job_id: str, request: Request):
    require_auth(request)
    j = _job(job_id)
    lf = j.get("log_file")
    if lf and Path(lf).exists():
        return JSONResponse({"job_id": job_id, "log": Path(lf).read_text(encoding="utf-8", errors="ignore")})
    return JSONResponse({"job_id": job_id, "log": "\n".join(j.get("log", []))})


@app.get("/api/logs")
def api_logs(request: Request):
    """Server log tail (if LOG_PATH set) + the most recent render job's log."""
    require_auth(request)
    out = {"server": "", "latest_job": None, "latest_log": ""}
    lp = os.environ.get("LOG_PATH")
    if lp and Path(lp).exists():
        out["server"] = _tail_log(Path(lp), 300)
    items = sorted(JOBS.values(), key=lambda j: j.get("started", 0), reverse=True)
    if items:
        j = items[0]
        out["latest_job"] = {"id": j.get("id"), "status": j.get("status")}
        lf = j.get("log_file")
        if lf and Path(lf).exists():
            out["latest_log"] = Path(lf).read_text(encoding="utf-8", errors="ignore")[-8000:]
        else:
            out["latest_log"] = "\n".join(j.get("log", []))[-8000:]
    return out


@app.get("/api/projects/{project_id}/download")
def download(project_id: str, request: Request):
    require_auth(request)
    pdir = UPLOAD_DIR / project_id
    out = _find_output(pdir)
    if not out:
        raise HTTPException(404, "No output video yet.")
    return FileResponse(out, media_type="video/mp4", filename=out.name)
