#!/bin/bash
# Weekdays at the 6:30 AM Pacific open: text the owner whether the major indices are accumulating
# above their 50-day moving average or distributing below it.
# Scheduled by ~/Library/LaunchAgents/com.soundinvestmentsolutions.daily50dma.plist, which sets TEXT_TO.
# Run it by hand with:  TEXT_TO=+1... ./daily-50dma/run.sh
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/../options-tracker/.venv/bin/python"
LOG="$HOME/Library/Logs/daily-50dma.log"

echo "===== $(date '+%Y-%m-%d %H:%M %Z') =====" >> "$LOG"
MSG=$("$PY" -W ignore "$HERE/check.py" 2>> "$LOG"); RC=$?
[ $RC -eq 3 ] && { echo "market closed" >> "$LOG"; exit 0; }
[ $RC -ne 0 ] || [ -z "$MSG" ] && MSG="50-day open check: couldn't get index data. See ~/Library/Logs/daily-50dma.log"
echo "$MSG" >> "$LOG"
MSG=$(printf '%s' "$MSG" | tr -d '"\\')
[ -n "$TEXT_TO" ] && osascript -e "tell application \"Messages\" to send \"$MSG\" to participant \"$TEXT_TO\" of (1st account whose service type = iMessage)" >> "$LOG" 2>&1
