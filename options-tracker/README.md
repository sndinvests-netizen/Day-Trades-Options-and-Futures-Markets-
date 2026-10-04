# Sound Investment Solutions Option Tracker

Look up **any ticker's option chain**, **build and test strategies** (in the style of OptionStrat), and **track the positions you open**, grouped as Hop, Skip or Leap. Prices come from Yahoo Finance (free, may be delayed about 15 minutes).

| Group | Days to expiration at open |
|---|---|
| **Hop** | 90 or less |
| **Skip** | 91 to 360 (about six months) |
| **Leap** | over 360 |

## Start it

```bash
cd options-tracker
./run.sh
```

The first run installs `yfinance` into `options-tracker/.venv`. It then opens **http://127.0.0.1:8765** in your browser. Press Ctrl+C in the terminal to stop it. It runs only on your computer.

Your trades are saved to `options-tracker/trades.json`, which is kept out of git. This repo is public, so never commit that file.

## In the browser

**Chain tab.** Type any ticker and pick an expiration.
- Calls and puts appear side by side around the stock price. In-the-money strikes are shaded, with a line at the current price.
- Each strike shows bid/ask, last, volume, open interest, IV and delta. Turn on Gamma / Theta / Vega for more Greeks.
- Summary tiles: price, ATM implied volatility, put/call ratio by volume and by open interest, and max pain.
- **Unusual activity:** contracts trading at least 2× their open interest (500+ contracts), marked ● and listed below the chain, biggest ratio first. Expect more flags on expirations a few days out, where heavy same-week trading is normal.
- Auto-refresh every 1, 2 or 5 minutes. Recent tickers and your watchlist appear as one-click chips.
- Click any call or put to log a trade on it, prefilled at the mid price.

**Builder tab.** Design a position and see how it pays off before you trade it.
- Pick a ready-made strategy: long call/put, covered call, cash-secured put, the four vertical spreads, straddles and strangles, iron condor, iron butterfly, call butterfly, collar, and calendar or diagonal spreads. Strikes are placed around the stock's expected move.
- Or build your own: add, remove and edit legs (buy/sell, call/put/stock, expiration, strike, quantity, price). On the Chain tab, clicking a contract adds it as a leg: a click on the bid sells, a click anywhere else buys.
- Summary boxes: net debit or credit, max profit and max loss, breakevens, chance of profit, buying power (estimated as max loss), and combined delta, gamma, theta and vega.
- Profit/loss chart at expiration and on any date before it (date slider), with an implied volatility slider and a price range slider. Hover over the chart to read exact values.
- Profit/loss grid by stock price and date, in dollars or as a percent of max risk.
- **Save to My Trades** stores the whole position. Each open position in My Trades has a **Builder** button that opens it back up at its entry prices.

**My Trades tab.** Open positions grouped Hop / Skip / Leap. A multi-leg position is grouped by its nearest expiration and shown as one row with its legs underneath.
- Live quote, unrealized P&L, position delta, days left, breakeven, max profit and max loss for each position.
- Assignment warnings on short options.
- Close a trade as bought/sold to close, expired, or assigned. A multi-leg position closes all at once, with an exit price for each leg.
- Results: win rate and realized P&L by group, by bought vs sold puts and calls, multi-leg, and by ticker. A spread counts as one position.

## Command line

The same trades file also works from the terminal (use `.venv/bin/python` or any Python with yfinance):

```bash
# Sell (short) a put: price is per share, as on your broker screen
python3 tracker.py add SPY --side sell --strike 550 --exp 2026-12-18 --premium 6.40 --fees 1.30

# Buy 2 LEAP calls (--type defaults to put)
python3 tracker.py add AAPL --type call --side buy --strike 250 --exp 2027-12-17 --premium 14.10 --contracts 2

# Open trades with live price, P&L, delta and assignment warnings
python3 tracker.py list --live
python3 tracker.py list --live --category leap --type put
python3 tracker.py list --status closed

# Close a trade
python3 tracker.py close 3 --premium 1.05          # bought/sold back at 1.05
python3 tracker.py close 4 --expired               # expired worthless
python3 tracker.py close 5 --assigned --premium 8  # assigned; 8 = intrinsic value

# Win rate and realized P&L
python3 tracker.py report
```

## Notes

- **Greeks** are calculated with Black-Scholes from Yahoo's implied volatility, a 4% rate and no dividends. Yahoo's IV is sometimes missing or near zero (deep in the money, or outside market hours); those strikes show "–".
- **Builder projections** before expiration use Black-Scholes with each leg's IV, with option values never below their exercise value (US options are American). Chance of profit assumes the stock moves randomly with the near-term at-the-money IV. These are estimates, not your broker's numbers, and buying power for naked options is set by your broker.
- **Sold calls** show unlimited max loss and no collateral, because the tracker doesn't know whether you own the shares.
- **Max pain** is the strike where option holders' total payout at expiration would be smallest, based on open interest.
- **Switching to a broker feed later** (Schwab, Tastytrade, Tradier): replace `expirations`, `spot_price` and `chain_rows` in `options_data.py`. Everything else builds on them.
