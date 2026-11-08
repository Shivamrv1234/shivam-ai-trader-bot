@echo off
REM Double-click to start SHIVAM AI TRADER on this Windows PC (instant replies + alerts + paper-trade checks).
REM Optional: MetaTrader 5 (e.g. an Exness account) for LIVE forex/gold prices - install the MT5 terminal,
REM log in once, keep it open, and this script installs the MetaTrader5 Python package automatically.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run setup: creating the Python environment...
  py -3.12 -m venv .venv || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
REM Keep packages up to date after you replace the files with a new version (fast when nothing changed)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
".venv\Scripts\python.exe" -m pip install -q -r requirements-windows.txt || echo MetaTrader5 package not installed (optional; forex then uses Twelve Data or Yahoo)
if not exist ".env" (
  copy .env.example .env >nul
  echo Fill in your values in the .env file that just opened, save it, then run this again.
  notepad .env
  exit /b
)
".venv\Scripts\python.exe" -m bot.main local %*
pause
