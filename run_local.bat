@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv — run setup from README first:
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -e .
  echo   .venv\Scripts\pip install python-libtiepie
  pause
  exit /b 1
)

echo.
echo Tiestim — local only, works offline (no internet needed).
echo Open in your browser:  http://127.0.0.1:8000/
echo Keep this window open while you use the app. Press Ctrl+C to stop.
echo.

".venv\Scripts\python.exe" -m uvicorn tiestim.api.app:app --host 127.0.0.1 --port 8000
echo.
pause
