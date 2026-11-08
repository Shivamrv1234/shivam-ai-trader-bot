#!/usr/bin/env bash
# Start SHIVAM AI TRADER on Linux/macOS or a cloud VM (instant replies + alerts). Ctrl+C to stop.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run setup: creating the Python environment..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Fill in your values in .env (nano .env), then run this again."
  exit 1
fi
exec .venv/bin/python -m bot.main local "$@"
