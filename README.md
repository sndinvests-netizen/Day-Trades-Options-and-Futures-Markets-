# Day Trades — Options and Futures Markets

A rule-based trading reference system for intraday futures and options trading. Covers the full session workflow: pre-market preparation, trade setup evaluation, risk management, and post-session review — all constrained to a 90-minute trading window (9:30–11:00 AM ET).

---

## Repository Contents

### [TRADING_PLAN_CHECKLIST.md](./TRADING_PLAN_CHECKLIST.md)
The primary reference document. A comprehensive, section-by-section checklist covering the complete trading day from pre-market preparation through post-session debrief. Includes:
- Pre-market bias assessment (daily 50 MA, MA stack, key levels)
- Market open routine and bias confirmation
- Non-negotiable session rules and emotional state protocol
- Pre-entry gate with confluence scoring (minimum 3 of 5)
- Risk management rules and position sizing formula
- Post-session debrief with trade-by-trade review
- Extended session notes for journaling and pattern tracking

### [TRADING_QUICK_REFERENCE.md](./TRADING_QUICK_REFERENCE.md)
A condensed single-page version of the full checklist, designed to be used during the live session. Contains all the key gates, the trade ticket, and a brief post-session debrief. Print or keep open on a second screen during the 90-minute window.

### [Ripstercloud.md](./Ripstercloud.md)
Reference notes on the Ripster Clouds TradingView indicator — an EMA ribbon system used to visually assess trend direction and momentum across timeframes. Includes cloud pair definitions, how to read multi-cloud alignment, timeframe-specific usage, and integration notes linking cloud signals to the main trading plan checklist.

---

## Trading Framework Summary

| Parameter | Rule |
|-----------|------|
| Trading window | 9:30 AM – 11:00 AM ET only |
| Bias method | Daily 50 MA position (SPY, QQQ, IWM) |
| Minimum confluence | 3 of 5 factors required per trade |
| Minimum R/R | 1:2 (risk one, target two) |
| Stop loss | Anchored to structure — entered before or at entry |
| Daily loss limit | Pre-defined; if hit, session ends immediately |

---

## How to Use These Documents

1. **Before the session:** Work through Section 1 and 2 of `TRADING_PLAN_CHECKLIST.md` to establish bias and mark levels.
2. **During the session:** Use `TRADING_QUICK_REFERENCE.md` as your live session sheet. Run the pre-entry gate before every trade.
3. **After the session:** Complete the post-session debrief in `TRADING_PLAN_CHECKLIST.md` Section 6 and Section 7.
4. **Ongoing:** Review `Ripstercloud.md` when using the Ripster Clouds indicator to cross-reference cloud alignment with your bias and entry timing.
