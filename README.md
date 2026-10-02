# Day-Trades-Options-and-Futures-Markets-
## Claude Code agents

| Agent | What it does | File |
|---|---|---|
| `tradingview-levels` | On demand, marks **prior-day high/low (PDH/PDL, red trend lines)** and, on request, **prior-week high/low (PWH/PWL, blue trend lines)**, full session with extended hours, plus **New Week Opening Gaps** (green boxes on NQ/ES every Sunday after the 6pm ET open, last 5 kept), on your TradingView charts in Chrome. Replaces its own old lines and boxes. Never trades and never gives advice. | [`agents/tradingview-levels.md`](agents/tradingview-levels.md) |

**Install:** copy `agents/*.md` to `~/.claude/agents/`. Then in Claude Code, say *"mark PDH/PDL on my watchlist"* or *"mark PDH/PDL on NQ and ES"*.
The watchlist is read from `TRADING_PLAN_CHECKLIST.md`, section 1.6.
