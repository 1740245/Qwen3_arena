@echo off
setlocal
cd /d %~dp0

if not exist .venv\Scripts\activate.bat (
  echo ⚠️  .venv not found. Create it first (python -m venv .venv).
  exit /b 1
)

call .venv\Scripts\activate.bat
python -m uvicorn backend.app.main:app --reload --port 8000
