#!/bin/bash
# Sunday 4:01 PM Pacific, after the first hourly candle of the Globex open (3:00 PM) closes:
# mark that candle's range and the weekend gap on MES and MNQ in TradingView, then text the owner.
# Scheduled by ~/Library/LaunchAgents/com.soundinvestmentsolutions.futuresopen.plist, which sets TEXT_TO.
# Run it by hand with:  TEXT_TO=+1... ./futures-open-range/run.sh [--date YYYY-MM-DD]
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/../options-tracker/.venv/bin/python"
LOG="$HOME/Library/Logs/futures-open-range.log"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

text() {  # iMessage the owner; also a Mac notification
  osascript -e "display notification \"$1\" with title \"Sunday futures open\"" 2>/dev/null
  [ -n "$TEXT_TO" ] && osascript -e "tell application \"Messages\" to send \"$1\" to participant \"$TEXT_TO\" of (1st account whose service type = iMessage)" >> "$LOG" 2>&1
}

echo "===== $(date '+%Y-%m-%d %H:%M %Z') =====" >> "$LOG"
# The 3:00 PM candle can take a few minutes to show up on Yahoo; try for up to 15 minutes.
for i in $(seq 1 15); do
  LEVELS=$("$PY" -W ignore "$HERE/levels.py" "$@" 2>> "$LOG") && echo "$LEVELS" | grep -q '"candle_closed": true' && break
  sleep 60
done
echo "$LEVELS" >> "$LOG"
if ! echo "$LEVELS" | grep -q '"or_high"'; then
  text "Sunday futures open: couldn't get MES/MNQ data. See ~/Library/Logs/futures-open-range.log"
  exit 1
fi

PROMPT=$("$PY" -c 'import sys; print(open(sys.argv[1]).read().replace("{{LEVELS}}", sys.argv[2]))' "$HERE/PROMPT.md" "$LEVELS")
cd "$HOME" || exit 1
OUT=$(claude -p --chrome "$PROMPT" \
  --allowedTools "mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__tabs_close_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__javascript_tool,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__find" \
  --disallowedTools "Bash,Edit,Write,WebFetch,mcp__mcp-tradingview__mcp-tv-create-alert,mcp__mcp-tradingview__mcp-tv-delete-alert" 2>&1)
echo "$OUT" >> "$LOG"

SUMMARY=$(echo "$OUT" | grep '^SUMMARY:' | tail -1 | cut -c10- | tr -d '"\\')
if [ -z "$SUMMARY" ]; then
  # Claude or Chrome failed: still send the numbers from Yahoo.
  SUMMARY="$("$PY" -W ignore "$HERE/levels.py" --text "$@" 2>> "$LOG") Lines NOT drawn on TradingView, see log."
fi
text "$SUMMARY"
