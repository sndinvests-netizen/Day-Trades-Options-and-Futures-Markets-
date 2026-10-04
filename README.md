# Day-Trades-Options-and-Futures-Markets-
## Claude Code agents

| Agent | What it does | File |
|---|---|---|
| `tradingview-levels` | On demand, marks **prior-day high/low (PDH/PDL, red trend lines)** and, on request, **prior-week high/low (PWH/PWL, blue trend lines)**, full session with extended hours, plus **New Week Opening Gaps** (green boxes on NQ/ES every Sunday after the 6pm ET open, last 5 kept), on your TradingView charts in Chrome. Replaces its own old lines and boxes. Never trades and never gives advice. | [`agents/tradingview-levels.md`](agents/tradingview-levels.md) |

**Install:** copy `agents/*.md` to `~/.claude/agents/`. Then in Claude Code, say *"mark PDH/PDL on my watchlist"* or *"mark PDH/PDL on NQ and ES"*.
The watchlist is read from `TRADING_PLAN_CHECKLIST.md`, section 1.6.

## Option tracker

[`options-tracker/`](options-tracker/) opens in your browser with `./options-tracker/run.sh`. Type any ticker to see its option chain with Greeks, put/call ratios, max pain and unusual activity. Build and test strategies in an OptionStrat-style Builder (profit/loss chart and grid, chance of profit, breakevens). It also logs puts, calls and multi-leg positions, grouped as **Hop** (90 days or less), **Skip** (91-360 days) or **Leap** (over 360 days), with live P&L. Prices come from Yahoo Finance and may be delayed about 15 minutes. See [`options-tracker/README.md`](options-tracker/README.md).
