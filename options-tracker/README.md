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

## Open it automatically on weekday mornings (macOS)

`launch-morning.sh` starts the tracker if it isn't running and opens it in the browser. A launchd agent in `~/Library/LaunchAgents/com.soundinvestmentsolutions.optiontracker.plist` runs it Monday to Friday at 6:00 AM local time, ahead of the 6:30 AM PT market open. It logs to `~/Library/Logs/option-tracker.log`.

- Turn it off: `launchctl bootout gui/$(id -u)/com.soundinvestmentsolutions.optiontracker`
- Turn it back on: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.soundinvestmentsolutions.optiontracker.plist`
- Run it now: `launchctl kickstart gui/$(id -u)/com.soundinvestmentsolutions.optiontracker`

If the Mac is asleep at 6:00 it runs when the Mac wakes up; if the Mac is shut down it doesn't run. To have the Mac wake itself first: `sudo pmset repeat wakeorpoweron MTWRF 05:55:00`.

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
- Profit/loss chart at expiration and on any date before it (date slider), with an implied volatility slider (shows the actual IV, from 0% up to 4x the quoted IV like OptionStrat; it scales every leg together, and a double-click returns it to the quoted IV) and a price range slider. Hover over the chart to read exact values.
- **Price slider** under the chart: drag it across to light up any stock price on the chart and in the grid, with the P/L there today (or on the chosen date) and at expiration.
- Profit/loss grid by stock price and date.
- **P/L $ / P/L % switch** at the top of the Builder: shows the summary boxes, chart and grid in dollars or as a percent of max risk (or of the premium when risk is unlimited).
- **Contract value switch** next to it: shows what the position is worth at each price and date (what you would receive, or pay if negative, to close it) instead of profit/loss. A dashed line marks your entry cost or credit; grid colors still show profit (green) or loss (red).
- **Save to My Trades** stores the whole position. Each open position in My Trades has a **Builder** button that opens it back up at its entry prices.

**My Trades tab.** Open positions grouped Hop / Skip / Leap. A multi-leg position is grouped by its nearest expiration and shown as one row with its legs underneath.
- Live quote, unrealized P&L, position delta, days left, breakeven, max profit and max loss for each position.
- Assignment warnings on short options.
- **50% profit target:** a glowing green dot and note appear on a position once it has captured half its max profit. For sold options and credit spreads, that means buying it back costs 50% or less of the premium collected; for bought options and debit spreads, that means it is up 50% or more. Only the option legs count. A summary box counts how many positions are at the target.
- Close a trade as bought/sold to close, expired, or assigned. A multi-leg position closes all at once, with an exit price for each leg.
- **Delete** a trade logged by mistake (open or closed). It asks first, a multi-leg position is deleted as a whole, and the file from just before is kept as `trades.json.bak`.
- **Premium income:** premium collected and kept from cash-secured puts, covered calls, covered puts, short calls, credit spreads (verticals, iron condors, iron butterflies) and short straddles/strangles. Shows each type's collected, kept (after buybacks, assignment and fees), kept %, premium still open, what the open ones would cost to close now, and a month-by-month table. Debit trades are left out; a short call is listed as a short call unless the shares are a stock leg in the same position.
- Results: win rate and realized P&L by group, by bought vs sold puts and calls, multi-leg, and by ticker. A spread counts as one position.

**Red folder calendar (left panel).** This week's high-impact USD events from [ForexFactory](https://www.forexfactory.com/calendar): CPI, PPI, PCE, JOLTS, jobs reports, FOMC and Fed Chair, President speeches, GDP, retail sales and so on, plus US bank holidays.
- Times are in your computer's time zone (hover a row for Eastern time). Each event shows forecast and previous, a tag (CPI, Jobs, Fed…), and a countdown. Released events dim, and events within the hour glow red.
- A "Next red folder" box counts down to the next high-impact release. **+ Orange** adds medium-impact events, such as jobless claims and Fed member speeches.
- **Alert:** a pop-up and chime 5, 15, 30 or 60 minutes before each red-folder event and again at release time (default 15 min; set it to off to silence). These alerts also show as Mac notifications if you turned those on for VIX alerts.
- ForexFactory's feed covers only the current Sunday–Saturday week and has no actual figures; check ForexFactory for the number once it's out. The tracker fetches the feed at most every 15 minutes and keeps the last copy in `econ_cache.json` (not in git). Hide/Show the panel from its header.

**Fear & Greed dial (left panel, under the calendar).** [CNN's Fear & Greed Index](https://www.cnn.com/markets/fear-and-greed), 0 to 100, drawn as a half-circle dial colored by CNN's bands: 0–24 Extreme Fear (red), 25–44 Fear, 45–55 Neutral (gray), 56–75 Greed, 76–100 Extreme Greed (green).
- Shows today's score and rating, with the previous close, 1 week, 1 month and 1 year ago and how far today has moved from each. The needle points at today's score, the current band is lit, a small triangle on the dial marks the previous close, and ▲/▼ shows the move since then.
- **The seven indicators** (click to open): market momentum, stock price strength and breadth, put/call options, market volatility, safe-haven demand and junk bond demand, each with its own rating and position on the fear-to-greed scale.
- The tracker fetches it at most every 10 minutes and keeps the last copy in `fng_cache.json` (not in git). Hide/Show it from its header.

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

# Premium collected and kept from selling options, by type and month
python3 tracker.py premium

# Delete a trade logged by mistake (shows what would go; add --yes to delete)
python3 tracker.py delete 7
python3 tracker.py delete 7 --yes
```

## Notes

- **Greeks** are calculated with Black-Scholes from Yahoo's implied volatility, a 4% rate and no dividends. Yahoo's IV is sometimes missing or near zero (deep in the money, or outside market hours); those strikes show "–".
- **Builder projections** before expiration use Black-Scholes with each leg's IV, with option values never below their exercise value (US options are American). Chance of profit assumes the stock moves randomly with the near-term at-the-money IV. These are estimates, not your broker's numbers, and buying power for naked options is set by your broker.
- **Sold calls** show unlimited max loss and no collateral, because the tracker doesn't know whether you own the shares.
- **Max pain** is the strike where option holders' total payout at expiration would be smallest, based on open interest.
- **Switching to a broker feed later** (Schwab, Tastytrade, Tradier): replace `expirations`, `spot_price` and `chain_rows` in `options_data.py`. Everything else builds on them.
