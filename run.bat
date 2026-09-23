@echo off
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\pip install -q -r requirements.txt
)
echo JG Job Tracker -^> http://127.0.0.1:8765
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
