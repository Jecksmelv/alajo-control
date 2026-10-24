#!/usr/bin/env bash
# Alajo Control - one-command start (macOS / Linux). Needs only Python 3.11+.
# Reads PORTKEY_API_KEY from .env in this folder or the folder above it.
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv
echo "Installing Python packages (first run takes a minute)..."
.venv/bin/python -m pip install -q -r requirements.txt
echo
echo "  Alajo Control is starting. Open  http://localhost:8000  in your browser."
echo "  Press Ctrl+C to stop."
echo
exec .venv/bin/python -m uvicorn backend.main:app --port 8000
