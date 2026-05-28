@echo off
cd /d "%~dp0"
if not exist "venv" (
  echo Creating virtual environment...
  python -m venv venv
  venv\Scripts\pip install -r requirements.txt --quiet
)
call venv\Scripts\activate.bat
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
python gui_server.py
pause
