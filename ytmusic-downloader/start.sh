#!/usr/bin/env bash
# Creates a virtualenv on first run, keeps yt-dlp current, then starts the app.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "Python 3.9+ is required but '$PY' was not found."; exit 1; }

if [ ! -d .venv ]; then
  echo "Creating .venv ..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

if [ ! -f .venv/.deps-installed ] || [ requirements.txt -nt .venv/.deps-installed ]; then
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
  touch .venv/.deps-installed
fi

command -v ffmpeg >/dev/null 2>&1 || \
  echo "note: ffmpeg was not found - conversion, tags and cover art will be unavailable."

exec python ytmd.py "$@"
