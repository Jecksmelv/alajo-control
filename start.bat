@echo off
rem Alajo Control - one-command start (Windows). Needs only Python 3.11+.
rem Reads PORTKEY_API_KEY from .env in this folder or the folder above it.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
)
echo Installing Python packages (first run takes a minute)...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
echo.
echo  Alajo Control is starting. Open  http://localhost:8000  in your browser.
echo  Press Ctrl+C in this window to stop.
echo.
".venv\Scripts\python.exe" -m uvicorn backend.main:app --port 8000
