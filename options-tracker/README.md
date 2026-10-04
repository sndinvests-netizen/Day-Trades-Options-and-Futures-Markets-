# Put Tracker: Hop / Skip / Leap

Logs puts you **buy** (long) and **sell** (short), sorts each into a group by days to expiration on the day you open it, and pulls live prices from Yahoo Finance (free, may be delayed about 15 minutes).

| Group | Days to expiration at open |
|---|---|
| **Hop** | 90 or less |
| **Skip** | 91 to 360 (about six months) |
| **Leap** | over 360 |

## Setup

```bash
cd options-tracker
pip install -r requirements.txt
```

Trades are saved to `options-tracker/trades.json`, which is kept out of git.

## Commands

```bash
# Sell (short) a put: price is per share, as on your broker screen
python put_tracker.py add SPY --side sell --strike 550 --exp 2026-12-18 --premium 6.40 --fees 1.30

# Buy (long) 2 LEAP puts
python put_tracker.py add AAPL --side buy --strike 200 --exp 2027-12-17 --premium 14.10 --contracts 2

# Open trades, grouped Hop / Skip / Leap, with live price, P&L, delta and assignment warnings
python put_tracker.py list --live
python put_tracker.py list --live --category leap
python put_tracker.py list --status closed

# Close a trade
python put_tracker.py close 3 --premium 1.05          # bought/sold back at 1.05
python put_tracker.py close 4 --expired               # expired worthless
python put_tracker.py close 5 --assigned --premium 8  # assigned; 8 = strike minus stock price

# Win rate and realized P&L by group, by bought vs sold, and by ticker
python put_tracker.py report
```

## What it shows

- **Every trade:** breakeven, max profit, max loss, days left, a warning inside the last week.
- **Sold puts:** cash needed to secure the put, annualized return on the premium, and a warning when the stock is within 2% of the strike or below it (assignment risk).
- **With `--live`:** stock price, bid/ask, mark (midpoint), implied volatility, delta (calculated with Black-Scholes from Yahoo's IV), and unrealized P&L.

To switch to a broker feed later (Schwab, Tastytrade, Tradier), replace `yahoo_quote` in `put_tracker.py`; it only needs to return the stock price, bid, ask, last, mark and IV.
