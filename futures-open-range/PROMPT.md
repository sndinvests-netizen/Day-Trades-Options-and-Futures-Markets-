# Mark the Sunday Globex open on MES and MNQ

You are marking the owner's **Sunday opening range and weekend gap** on their TradingView charts in their logged-in Chrome, using only the Claude in Chrome tools. Never trade, never create or delete alerts, never log in, and never change or remove any drawing you didn't create, except the previous week's lines described in step 4.

The levels (JSON from `levels.py`; prices from Yahoo's continuous front-month contracts):

```
{{LEVELS}}
```

`start_ts` is the 3:00 PM Pacific candle (Unix seconds) and `fri_close_ts` is Friday's last hourly candle. `or_high`/`or_low` are the first hour's high and low. `gap = open − fri_close`; `gap_kind` is `up`, `down` or `none`.

## For each symbol

1. **Your own tab.** Call `tabs_context_mcp` with `createIfEmpty: true` and use that tab. Navigate to `https://www.tradingview.com/chart/8Ie4duiG/` (the owner's "Option Layout") **without** a symbol parameter. Wait about 6 s, then record `TradingViewApi.activeChart().symbol()` and `.resolution()` so you can restore them at the end. If the page shows "Sign in" or "Join for free", print `FAILED: <SYMBOL> TradingView is logged out` for both symbols and stop.
2. **Load the chart.** `const c = TradingViewApi.activeChart(); c.setSymbol('<tradingview>'); c.setResolution('60');` with **no callbacks**. Poll every 500 ms (up to 20 s) until `c.symbol()` ends with `<SYMBOL>1!` and `c.resolution()` is `'60'`.
3. **Cross-check with TradingView's own bars.** Read bars with `c.getSeries().data().each((i, b) => …)` (`b = [time, open, high, low, close, volume]`). Find the bar at `start_ts`. If TradingView has it, **use TradingView's high/low/open** (they can differ from Yahoo by a tick or two) and use TradingView's last bar at or before `fri_close_ts` as Friday's close. Recompute the gap from those. If the 3:00 PM bar isn't there, use the JSON values and say so.
4. **Remove last week's lines.** Delete only shapes on this symbol whose text starts with `Sun OR high`, `Sun OR low`, `Fri close` or `Gap ` (`c.removeEntity(id)`). Leave everything else alone.
5. **Draw** (all locked, label font size 14, extend right):
   ```js
   const ray = (t, p, txt, col, v) => c.createShape({ time: t, price: p }, { shape: 'horizontal_ray', lock: true,
     overrides: { linecolor: col, linewidth: 2, linestyle: 0, showLabel: true, text: txt, textcolor: col,
                  fontsize: 14, horzLabelsAlign: 'right', vertLabelsAlign: v } });
   ray(start_ts, or_high, `Sun OR high ${or_high}`, '#FF9800', 'top');
   ray(start_ts, or_low,  `Sun OR low ${or_low}`,   '#FF9800', 'bottom');
   ```
   If there is a gap (`up` or `down`), also draw:
   - `ray(fri_close_ts, fri_close, 'Fri close ' + fri_close, '#9C27B0', 'bottom')`
   - a rectangle between Friday's close and the Sunday open, from `fri_close_ts` to `start_ts + 3600`, with `{shape:'rectangle', lock:true, overrides:{color:'#9C27B0', backgroundColor:'rgba(156,39,176,0.12)', fillBackground:true, extendRight:true, showLabel:true, text:'Gap up +N' (or 'Gap down −N'), textColor:'#9C27B0', fontSize:14}}` created with `c.createMultipointShape([{time, price}, {time, price}], …)`.
   `createShape` returns before the shape exists: wait 1 s, then **verify** by reading `c.getAllShapes()` and each shape's text and points.
6. **Save** with the `computer` tool: key `cmd+s`.

## When both are done

Put the layout back (`setSymbol` / `setResolution` to the originals, no callbacks), wait 3 s, `cmd+s`, and close your tab.

## Output (the script reads these lines; print them exactly)

One line per symbol:

- `DONE: <SYMBOL> or_high=<h> or_low=<l> open=<o> fri_close=<fc> gap=<signed points> kind=<up|down|none> full=<yes|no> filled=<yes|no> source=<tradingview|yahoo>`
- `FAILED: <SYMBOL> <reason>`

Then a last line, written as a text message to the owner (plain text, no markdown, under 300 characters), for example:
`SUMMARY: Sunday open 1st hour closed. MES range 7783.00-7789.25 (6.25 pts), gap up +9.50 from Fri 7777.25, not filled. MNQ range 31117-31170 (53 pts), gap up +68.75, not filled. Lines are on TradingView.`
