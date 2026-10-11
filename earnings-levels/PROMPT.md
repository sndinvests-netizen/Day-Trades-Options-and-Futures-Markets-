# Mark earnings highs and lows on TradingView

You are marking the owner's **earnings reaction levels** on their TradingView charts in their logged-in Chrome, using only the Claude in Chrome tools. Never trade, never create or delete alerts, never log in, and never change or remove any drawing you didn't create in this run.

These reports are due (JSON from `due.py`):

```
{{DUE}}
```

`session` is when the company reported: `bmo` before the open, `amc` after the close, `tns` time not announced. `reaction` is the trading day whose candle carries the earnings move.

## For each due report

1. **Your own tab.** Call `tabs_context_mcp` with `createIfEmpty: true` and use that tab. Navigate to `https://www.tradingview.com/chart/8Ie4duiG/` (the owner's "Option Layout") **without** a symbol parameter first. Wait about 6 s, then record the chart's current symbol and resolution with `TradingViewApi.activeChart().symbol()` / `.resolution()` so you can put them back at the end. If the page shows "Sign in" or "Join for free", stop and print `FAILED: <TICKER> <date> TradingView is logged out` for every report.
2. **Load the symbol on the daily chart.** `const c = TradingViewApi.activeChart(); c.setSymbol('<TICKER>'); c.setResolution('D');` with **no callbacks** (callbacks can hang the page). Then poll every 500 ms (up to 20 s) until `c.symbol()` ends with `:<TICKER>`, `c.resolution()` is `'D'` or `'1D'`, and the last bar has changed. Re-check both right before drawing; the chart can revert.
3. **Find the reaction candle.** Read the bars with `c.getSeries().data().each((i, b) => …)` where `b = [time, open, high, low, close, volume]` and `time` is in seconds. Convert each bar's time to a New York date (`toLocaleDateString('en-CA', {timeZone: 'America/New_York'})`) and pick the bar dated `reaction`.
   - If `session` is `tns`, compare the bar dated `date` with the bar dated `reaction`. The earnings candle is the one with clearly higher volume (and usually the gap); use it, and say which you used.
   - If the reaction bar isn't there yet (holiday, data delay), don't draw. Print `SKIPPED: <TICKER> <date> no <reaction> bar yet`.
4. **Skip duplicates.** If the chart already has a shape whose text starts with `ER high` or `ER low` and contains this candle's date label, don't draw again. Count it as done.
5. **Draw two locked horizontal rays** starting at the reaction candle and extending right:
   ```js
   const style = (txt, v) => ({ shape: 'horizontal_ray', lock: true, overrides: {
     linecolor: '#E91E63', linewidth: 2, linestyle: 0, showLabel: true, text: txt,
     textcolor: '#E91E63', fontsize: 12, horzLabelsAlign: 'right', vertLabelsAlign: v } });
   c.createShape({ time: bar[0], price: high }, style(`ER high ${high.toFixed(2)} (${label})`, 'top'));
   c.createShape({ time: bar[0], price: low },  style(`ER low ${low.toFixed(2)} (${label})`, 'bottom'));
   ```
   `label` is the candle date as `Oct 22`. `createShape` returns before the shape exists, so wait 1 s and then **verify** by reading `c.getAllShapes()` back and finding both texts. Don't call `getShapeById` on the return value right away.
6. **Save** with the `computer` tool: key `cmd+s`.

## When all reports are done

- Put the layout back: `c.setSymbol('<original symbol>'); c.setResolution('<original resolution>');` (no callbacks), wait 3 s, `cmd+s`.
- Close your tab with `tabs_close_mcp`.

## Output (the script reads these lines; print them exactly)

One line per report:

- `DONE: <TICKER> <date> high=<high> low=<low> candle=<YYYY-MM-DD>`, drawn and verified (or already on the chart)
- `SKIPPED: <TICKER> <date> <reason>`, not ready yet (it will be retried tomorrow)
- `FAILED: <TICKER> <date> <reason>`, something went wrong

Then a last line: `SUMMARY: <one short sentence, e.g. "TSLA earnings levels marked: high 512.30, low 471.05 (Oct 22)">`
