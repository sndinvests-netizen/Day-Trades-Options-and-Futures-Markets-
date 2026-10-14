#!/bin/bash
# Weekly health check for the option tracker (owner request 2026-10-14).
# Runs Sundays at 6:00 PM via ~/Library/LaunchAgents/com.soundinvestmentsolutions.optiontracker-healthcheck.plist,
# ahead of the trading week. Texts the owner only when a check fails.
cd "$(dirname "$0")"
URL="http://127.0.0.1:8765"
LOG="$HOME/Library/Logs/option-tracker-health.log"
TEXT_TO="$(cat "$HOME/.config/sis/owner_phone" 2>/dev/null)"  # kept outside the repo (public)
FAILS=()
echo "===== $(date '+%Y-%m-%d %H:%M') =====" >> "$LOG"
# Give the 6:00 launch time to finish; start the tracker if it still isn't up.
for _ in $(seq 1 60); do curl -s -o /dev/null -m 3 "$URL/" && break; sleep 2; done
if ! curl -s -o /dev/null -m 3 "$URL/"; then
  ./launch-morning.sh >> "$LOG" 2>&1 || true
  curl -s -o /dev/null -m 3 "$URL/" || FAILS+=("tracker is not running and would not start")
fi
python3 -c "import json;json.load(open('trades.json'))" 2>>"$LOG" || FAILS+=("trades.json is unreadable")
if curl -s -o /dev/null -m 3 "$URL/"; then
  for p in "/api/positions" "/api/chain?ticker=SPY" "/api/vix" "/api/feargreed" "/api/econ" "/api/earnings" "/api/screener"; do
    out=$(curl -s -m 90 -w "\n%{http_code}" "$URL$p")
    code=$(echo "$out" | tail -1)
    body=$(echo "$out" | sed '$d')
    echo "$p $code" >> "$LOG"
    if [ "$code" != "200" ]; then FAILS+=("$p returned HTTP $code")
    elif [ "$p" = "/api/positions" ] && echo "$body" | grep -q '"error"'; then FAILS+=("/api/positions has errors in it")
    fi
  done
fi
if [ ${#FAILS[@]} -eq 0 ]; then
  echo "OK" >> "$LOG"
else
  MSG="Option tracker check failed: $(IFS='; '; echo "${FAILS[*]}"). Log: ~/Library/Logs/option-tracker-health.log"
  echo "$MSG" >> "$LOG"
  osascript -e "tell application \"Messages\" to send \"$MSG\" to participant \"$TEXT_TO\" of (1st account whose service type = iMessage)" >> "$LOG" 2>&1
fi
