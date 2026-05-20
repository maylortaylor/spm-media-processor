#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
  venv/bin/pip install -r requirements.txt --quiet
fi
source venv/bin/activate
python gui_server.py
