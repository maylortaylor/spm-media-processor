@echo off
cd /d "%~dp0"
if not exist "venv" (
  echo Creating virtual environment...
  python -m venv venv
  venv\Scripts\pip install -r requirements.txt --quiet
)
call venv\Scripts\activate.bat
python gui_server.py
pause
