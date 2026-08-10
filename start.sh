#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
echo "Open http://127.0.0.1:8000"
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000
