@echo off
REM Creates a virtualenv on first run, then starts the app.
setlocal
cd /d "%~dp0"

where python >nul 2>&1 || (echo Python 3.9+ is required but was not found on PATH. & exit /b 1)

if not exist ".venv" (
  echo Creating .venv ...
  python -m venv .venv || exit /b 1
)

call .venv\Scripts\activate.bat

if not exist ".venv\.deps-installed" (
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt || exit /b 1
  echo. > ".venv\.deps-installed"
)

where ffmpeg >nul 2>&1 || echo note: ffmpeg was not found - conversion, tags and cover art will be unavailable.

python ytmd.py %*
