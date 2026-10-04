@echo off
REM ============================================================================
REM  AutoScene Studio - LOCAL dashboard  (image + video build v9)
REM  Runs the same web app the Colab notebook runs, but on this PC.
REM  Uploads, renders and finished videos stay on this machine.
REM ============================================================================
setlocal
set "ROOT=C:\Users\Tlite\automan-web-colab"
set "ENGINE=C:\Users\Tlite\automan-engine"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "FF=C:\Users\Tlite\AppData\Local\hermes\tools\ffmpeg-9.0.1-win32-x64\bin"

REM --- engine + ffmpeg wiring ---
set "THEAUTOMAN_DIR=%ENGINE%"
set "THEAUTOMAN_PYTHON=%PY%"
set "FFMPEG_PATH=%FF%\ffmpeg.exe"
set "FFPROBE_PATH=%FF%\ffprobe.exe"

REM --- this PC has no NVIDIA GPU: CPU encode (libx264) ---
set "AUTOMAN_CODEC=libx264"
set "AUTOMAN_PADDING=0.12"
set "AUTOMAN_BEAT_SECONDS=8"

REM --- finished videos are also copied here (this PC's "Drive") ---
set "DRIVE_OUT_DIR=C:\Users\Tlite\Desktop\AutoScene_Outputs"

REM --- no login on the dashboard ---
set "ADMIN_PASSWORD="
set "LOG_PATH=%ROOT%\logs\app.log"

if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
if not exist "%DRIVE_OUT_DIR%" mkdir "%DRIVE_OUT_DIR%"
cd /d "%ROOT%"

powershell -NoProfile -Command "exit (Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count" >nul 2>&1
if %errorlevel%==1 (
  echo AutoScene is already running - opening the dashboard...
  start "" "http://127.0.0.1:7860"
  goto :eof
)

echo Starting AutoScene Studio on http://127.0.0.1:7860
echo The server runs in a minimised window. Stop it with Stop-AutoScene.bat
start "AutoScene dashboard" /min cmd /c ""%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 7860 1>> "%LOG_PATH%" 2>&1"

for /l %%i in (1,1,25) do (
  ping -n 2 127.0.0.1 >nul
  curl -s -o NUL http://127.0.0.1:7860/health && goto :open
)
:open
start "" "http://127.0.0.1:7860"
