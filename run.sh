#!/usr/bin/env bash
# Start the tracker locally. First run installs deps into a venv.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
echo "JG Job Tracker -> http://127.0.0.1:8765"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
