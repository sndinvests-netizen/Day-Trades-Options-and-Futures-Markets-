#!/bin/bash
# Start the option tracker in your browser. First run sets up a private Python
# environment in .venv (kept out of git) and installs yfinance.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: installing yfinance into options-tracker/.venv ..."
  python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt || exit 1
fi
exec .venv/bin/python -W ignore app.py "$@"
