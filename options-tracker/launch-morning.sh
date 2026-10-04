#!/bin/bash
# Morning launcher for the option tracker, run by a launchd schedule (weekdays 6:00 AM).
# Starts the tracker in the background if it isn't already running, then opens it in
# the browser and exits, so the schedule can fire again the next morning.
cd "$(dirname "$0")"
URL="http://127.0.0.1:8765"
up() { curl -s -o /dev/null -m 3 "$URL/"; }
echo "$(date '+%Y-%m-%d %H:%M:%S') morning launch"
if ! up; then
  echo "starting tracker"
  nohup ./run.sh --no-browser >/dev/null 2>&1 &
  for _ in $(seq 1 90); do  # first run may install yfinance, so allow time
    up && break
    sleep 1
  done
fi
if up; then
  echo "opening $URL"
  open "$URL"
else
  echo "tracker did not start; try running ./run.sh by hand to see the error"
  exit 1
fi
