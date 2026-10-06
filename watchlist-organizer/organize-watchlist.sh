#!/bin/bash
# Sort the main TradingView watchlist into its sections and colour flags.
# Runs Claude Code headless with only the TradingView watchlist and symbol-search tools.
# Scheduled weekly by ~/Library/LaunchAgents/com.soundinvestmentsolutions.watchlist.plist;
# run it by hand any time with:  ./watchlist-organizer/organize-watchlist.sh
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/Library/Logs/watchlist-organizer.log"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

# The mcp-tradingview server is configured for the home folder, so run from there.
cd "$HOME" || exit 1
{
  echo "===== $(date '+%Y-%m-%d %H:%M %Z') ====="
  claude -p "$(cat "$HERE/PROMPT.md")" \
    --allowedTools "mcp__mcp-tradingview__mcp-watchlist-list-watchlists,mcp__mcp-tradingview__mcp-watchlist-get-watchlist,mcp__mcp-tradingview__mcp-watchlist-add-to-watchlist,mcp__mcp-tradingview__mcp-watchlist-remove-from-watchlist,mcp__mcp-tradingview__mcp-tv-search-symbols" \
    --disallowedTools "Bash,Edit,Write,mcp__mcp-tradingview__mcp-watchlist-delete-watchlist,mcp__mcp-tradingview__mcp-tv-create-alert,mcp__mcp-tradingview__mcp-tv-delete-alert"
  echo "exit $?"
} >> "$LOG" 2>&1

# Pop up a Mac notification with this run's one-line summary.
SUMMARY=$(tail -40 "$LOG" | grep '^SUMMARY:' | tail -1 | cut -c10- | tr -d '"\\')
osascript -e "display notification \"${SUMMARY:-Finished. See ~/Library/Logs/watchlist-organizer.log}\" with title \"TradingView watchlist organized\"" 2>/dev/null
