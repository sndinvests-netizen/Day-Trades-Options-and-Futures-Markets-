# Weekly TradingView watchlist organizer

You are tidying the owner's main TradingView watchlist. Use only the `mcp-tradingview` watchlist and symbol-search tools. Never trade, never delete a watchlist, and never remove a symbol from the main watchlist. Every symbol that is in it now must still be in it when you finish.

Main watchlist: **"Watchlist", id `119849585`**.

## Sections, in this order, with their colour flag

| # | Section header (exact text) | Colour | What goes here |
|---|---|---|---|
| 1 | `###Indices & Volatility` | blue | Index levels (SPX, NDQ, DJI, sector indices), VIX and volatility indices, market ratios such as the Shiller P/E |
| 2 | `###Index ETFs` | blue | Broad-market ETFs, including leveraged and income ones (SPY, QQQ, IWM, EEM, TQQQ, TNA, QQQI) |
| 3 | `###Sector & Theme ETFs` | blue | Sector, industry, theme and commodity-miner ETFs (XLV, XBI, XME, XRT, ITB, GDXJ, URA, SHLD) |
| 4 | `###Single-Stock ETFs` | blue | Leveraged, inverse or option-income ETFs on one stock (TSLL, METU, METW, NVDW, AMZW) |
| 5 | `###Futures` | orange | Index futures (ES/MES, NQ/MNQ, YM, RTY, any month) |
| 6 | `###Commodities` | orange | Metals, oil, gas, ags: spot, futures, CFDs and commodity ETFs (GOLD, SI1!, SLV, CL1!, USO) |
| 7 | `###Rates & Bonds` | cyan | Government yields and bond ETFs (US02Y, US10Y, TNX, JP10Y, IEF, TLT, TMF) |
| 8 | `###Economy` | cyan | FRED and ECONOMICS data series (CPI, jobs, Fed funds, M2, mortgage rates, debt) |
| 9 | `###Dollar & Forex` | cyan | DXY, UUP and currency pairs |
| 10 | `###Crypto & Miners` | purple | Coins, crypto ETFs (BITX, BTCI, IBIT) and bitcoin miners (MARA, IREN, HIVE, CLSK) |
| 11 | `###Tech & Semiconductors` | green | Hardware, chips, semicap, electronic components |
| 12 | `###Software & Cybersecurity` | green | Software, cloud, AI platforms, cybersecurity, quantum computing |
| 13 | `###Internet & Communication` | green | Internet platforms, social media, streaming, e-commerce, telecom, satellite communications |
| 14 | `###Financials` | pink | Banks, brokers, insurers, asset managers, BDCs, fintech, payments |
| 15 | `###Healthcare` | pink | Pharma, biotech, medical devices, health insurers, healthcare services |
| 16 | `###Consumer Discretionary` | red | Autos, restaurants, apparel, retail (non-staples), leisure, casinos, cruise, education |
| 17 | `###Consumer Staples` | red | Food, beverages, household and personal products, discount and staples retail |
| 18 | `###Industrials, Aerospace & Defense` | red | Industrial machinery, electrical equipment, aerospace, defense, drones, robotics |
| 19 | `###Transport & Travel` | red | Airlines, shipping, logistics, freight, rail |
| 20 | `###Energy` | orange | Oil and gas producers, refiners, midstream pipelines, energy tech |
| 21 | `###Utilities & Nuclear` | red | Utilities, nuclear power, uranium miners |
| 22 | `###Materials & Mining` | red | Metals and mining stocks (non-uranium), chemicals, paper, lithium, seabed minerals |
| 23 | `###Real Estate` | pink | REITs and real-estate companies |

Colour flag lists (these are "colored" watchlists in TradingView; a symbol should be in exactly one):
red `120872801`, orange `157950783`, green `157950902`, purple `157065500`, blue `156818019`, cyan `160899806`, pink `161329225`.

## Steps

1. Get watchlist `119849585`. Print one line `BEFORE: ` followed by its full symbols array as JSON, so the log keeps a backup.
2. Work out the correct section for every symbol:
   - A symbol already sitting inside its correct section stays there, in its current position relative to the others.
   - A symbol that is outside its correct section (newly added ones usually land at the end of the list) moves to the end of its correct section.
   - If you're unsure what a ticker is, look it up with the symbol-search tool. If it still fits nowhere, put it under `###Unsorted – review` as the very last section, and only create that section when something needs it.
   - Ignore any section headers that aren't in the table above (drop them), except `###Unsorted – review`.
3. If the order is already correct, skip to step 5.
4. Rebuild the list: call add-to-watchlist on `119849585` **once**, with the complete list in the new order: every header and every symbol. Adding a symbol that is already present moves it to the end, so this reorders the whole list. Then get the watchlist again and check:
   - the symbols (excluding `###` headers) are exactly the same set as BEFORE, with none missing and none extra;
   - the order matches what you sent.
   If anything is missing, add it back immediately and report the problem.
5. Colour flags: get each colour list. Find every symbol in the main watchlist whose flag isn't its section's colour. Ignore flagged symbols that aren't in the main watchlist. The colour lists are read-only through this tool, so don't try to change them. Instead, list each needed change on its own line as `FLAG NEEDED: <SYMBOL> -> <colour>`, so the owner can apply them.
6. The very last line of your reply must be `SUMMARY: ` followed by one short sentence, for example "SUMMARY: Moved 3 symbols (HOOD to Financials, IBIT to Crypto & Miners, XLE to Sector & Theme ETFs); 3 flags need updating." or "SUMMARY: No changes." Before that line, give a short report: which symbols moved and to which section, which flags changed, and anything under Unsorted. If nothing changed, say "Watchlist already organized, no changes."
