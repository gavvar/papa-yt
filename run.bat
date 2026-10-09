@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [Audio Augment] Python not found. Install Python 3.10+ and add it to PATH.
  pause
  exit /b 1
)

if not exist .venv\Scripts\python.exe (
  echo [Audio Augment] Creating the virtual environment and installing libraries...
  python -m venv .venv
  if errorlevel 1 (
    echo [Audio Augment] Could not create .venv
    pause
    exit /b 1
  )
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [Audio Augment] Library install failed.
    pause
    exit /b 1
  )
)

echo [Audio Augment] Open http://127.0.0.1:8000
.venv\Scripts\python.exe api.py
if errorlevel 1 pause
