#!/bin/bash
# After each earnings report for a stock on the option tracker's earnings watchlist, mark that
# reaction candle's high and low on TradingView (in Chrome). Only future reports are tracked.
# Scheduled weekdays after the close by ~/Library/LaunchAgents/com.soundinvestmentsolutions.earningslevels.plist;
# run it by hand any time with:  ./earnings-levels/mark-earnings-levels.sh
# Claude only runs when a report is due, so most days this costs nothing.
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/../options-tracker/.venv/bin/python"
LOG="$HOME/Library/Logs/earnings-levels.log"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

notify() { osascript -e "display notification \"$1\" with title \"Earnings levels\"" 2>/dev/null; }

echo "===== $(date '+%Y-%m-%d %H:%M %Z') =====" >> "$LOG"
DUE=$("$PY" -W ignore "$HERE/due.py" 2>> "$LOG")
if [ -z "$DUE" ] || [ "$DUE" = "[]" ]; then
  echo "nothing due" >> "$LOG"
  exit 0
fi
echo "due: $DUE" >> "$LOG"

PROMPT=$("$PY" -c 'import sys; print(open(sys.argv[1]).read().replace("{{DUE}}", sys.argv[2]))' "$HERE/PROMPT.md" "$DUE")
cd "$HOME" || exit 1
OUT=$(claude -p --chrome "$PROMPT" \
  --allowedTools "mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__tabs_close_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__javascript_tool,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__find" \
  --disallowedTools "Bash,Edit,Write,WebFetch,mcp__mcp-tradingview__mcp-tv-create-alert,mcp__mcp-tradingview__mcp-tv-delete-alert" 2>&1)
echo "$OUT" >> "$LOG"

# Record what got drawn so it isn't drawn again; SKIPPED and FAILED ones are retried next run.
echo "$OUT" | grep -E '^DONE: ' | while read -r _ TICKER DATE _; do
  "$PY" -W ignore "$HERE/due.py" --done "$TICKER" "$DATE" >> "$LOG" 2>&1
done

SUMMARY=$(echo "$OUT" | grep '^SUMMARY:' | tail -1 | cut -c10- | tr -d '"\\')
if echo "$OUT" | grep -q '^FAILED: '; then
  notify "${SUMMARY:-Couldn't mark some earnings levels.} See ~/Library/Logs/earnings-levels.log"
else
  notify "${SUMMARY:-Finished. See ~/Library/Logs/earnings-levels.log}"
fi
