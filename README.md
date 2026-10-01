# Day-Trades-Options-and-Futures-Markets-
## Claude Code agents

| Agent | What it does | File |
|---|---|---|
| `tradingview-levels` | On demand, marks **prior-day high (PDH) and low (PDL)**, full session with extended hours, as labeled lines on your TradingView charts in Chrome, and removes the previous day's lines. Never trades and never gives advice. | [`agents/tradingview-levels.md`](agents/tradingview-levels.md) |

**Install:** copy `agents/*.md` to `~/.claude/agents/`. Then in Claude Code, say *"mark PDH/PDL on my watchlist"* or *"mark PDH/PDL on NQ and ES"*.
The watchlist is read from `TRADING_PLAN_CHECKLIST.md`, section 1.6.
