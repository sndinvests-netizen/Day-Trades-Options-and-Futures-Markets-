"""Stocks sitting at support, for the Screener tab.

Universe: US-listed stocks (NYSE, Nasdaq, NYSE American) at or above the minimum market cap, from
Yahoo's equity screener (yfinance `screen` + `EquityQuery`, one query per sector so each stock comes
with its sector). If Yahoo's screener fails, a built-in list of liquid optionable large caps is used.
A short list of big optionable ETFs is scanned too, and shown only when "stocks only" is off.

For each stock, one year of daily bars (batched through `yf.download`) gives:
  - price, day %, 52-week high and % off it, 200-day SMA and % vs it, 1-year change
  - a weekly close series for the sparkline
  - support: pivot (swing) lows from the last 12 months of daily lows, a low that is the lowest of the
    5 bars on each side, clustered within 1.5% (the level is the cluster's average, touches = how many
    swing lows it holds), plus the 200-day and 50-day SMAs. The nearest of these at or below the last
    close is the stock's support.
  - at support: the close is 0% to `near`% (default 3%) above that level, and no close in the last 5
    sessions was more than 2% below it (a level that just broke isn't support).

Total option open interest is the sum of openInterest over every listed expiration. It takes one
request per expiration, so it is fetched only for stocks near support (and for anything else a
request asks to see), at most once per day, since the exchanges update OI overnight.

Everything runs in a background thread so a request never waits on Yahoo; the last scan is kept in
screener_cache.json (gitignored). A scan is reused until the next session close, or for an hour while
the market is open.
"""
import json
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from options_data import ET, QuoteError, _yf

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "screener_cache.json")

DEFAULT_MIN_CAP = 10e9
DEFAULT_MIN_OI = 100_000
DEFAULT_NEAR = 3.0
LOWEST_CAP = 2e9              # smallest min_cap the screener will scan down to
EXCHANGES = ["NMS", "NGM", "NCM", "NYQ", "ASE"]   # Nasdaq (3 tiers), NYSE, NYSE American
SECTORS = ["Technology", "Communication Services", "Consumer Cyclical", "Consumer Defensive", "Healthcare",
           "Financial Services", "Industrials", "Energy", "Basic Materials", "Real Estate", "Utilities"]
PIVOT_BARS = 5                # a swing low is the lowest low of 5 bars on each side
CLUSTER = 0.015               # swing lows within 1.5% of each other are one level
BREAK = 0.02                  # a close more than 2% under the level ...
BREAK_DAYS = 5                # ... in the last 5 sessions means it broke
OI_NEAR = DEFAULT_NEAR / 100  # after a scan, fetch OI for stocks at support at the default distance
HIST_BATCH = 100
OI_WORKERS = 3               # parallel option-chain lookups; more gets Yahoo rate limiting
INTRADAY_SECONDS = 3600       # while the market is open, rescan after an hour
RETRY_SECONDS = 300           # after a failed scan (Yahoo down or rate limiting), wait 5 minutes before trying again

# Big optionable ETFs, scanned only so "stocks only" can be turned off.
ETFS = {"SPY": "SPDR S&P 500 ETF", "QQQ": "Invesco QQQ Trust", "IWM": "iShares Russell 2000 ETF",
        "DIA": "SPDR Dow Jones Industrial Average ETF", "XLF": "Financial Select Sector SPDR",
        "XLE": "Energy Select Sector SPDR", "XLK": "Technology Select Sector SPDR",
        "XLV": "Health Care Select Sector SPDR", "XLI": "Industrial Select Sector SPDR",
        "XLU": "Utilities Select Sector SPDR", "XLP": "Consumer Staples Select Sector SPDR",
        "SMH": "VanEck Semiconductor ETF", "KRE": "SPDR S&P Regional Banking ETF", "XBI": "SPDR S&P Biotech ETF",
        "GLD": "SPDR Gold Shares", "SLV": "iShares Silver Trust", "GDX": "VanEck Gold Miners ETF",
        "TLT": "iShares 20+ Year Treasury Bond ETF", "HYG": "iShares iBoxx High Yield Corporate Bond ETF",
        "EEM": "iShares MSCI Emerging Markets ETF", "EFA": "iShares MSCI EAFE ETF", "FXI": "iShares China Large-Cap ETF",
        "KWEB": "KraneShares CSI China Internet ETF", "USO": "United States Oil Fund", "ARKK": "ARK Innovation ETF"}

# Used only when Yahoo's screener is down: liquid, optionable S&P 500 / Nasdaq-100 names over $10B.
FALLBACK = """AAPL MSFT NVDA AMZN GOOGL GOOG META AVGO TSLA BRK-B JPM LLY V UNH XOM MA COST HD PG JNJ ORCL ABBV WMT NFLX
BAC CRM KO CVX MRK AMD PEP ADBE TMO LIN ACN MCD CSCO ABT WFC DHR TXN QCOM INTU IBM GE AMGN CAT PM VZ ISRG NOW DIS GS
UBER CMCSA AMAT PFE SPGI RTX T NEE LOW UNP BKNG HON AXP PGR COP SYK MS ELV BLK TJX LRCX C SCHW VRTX BSX PLD MDT ETN
REGN CB ADP MU PANW ADI MMC BX SBUX KLAC DE GILD LMT CI ANET FI SO MDLZ AMT BMY TMUS SHW MO CME DUK ICE CL ZTS PYPL
CMG SNPS CDNS EQIX WM APH TT MCO ITW PH CRWD ORLY ABNB MAR NKE TGT USB PNC EMR FDX GM F CVS HCA MRVL INTC PLTR COIN
MSTR DELL SMCI SNOW DDOG NET SHOP SQ HOOD DASH APP ARM TSM ASML BABA PDD NU RIVN LULU DAL UAL AAL CCL OXY SLB HAL
DVN FCX NEM KMI WMB MPC PSX VLO EOG""".split()

_lock = threading.Lock()
_data = None                  # the last scan: see _scan()
_job = {"running": False, "phase": "", "done": 0, "total": 0, "started": 0, "finished": 0, "error": None}
_oi = {}                      # ticker -> {"oi", "calls", "puts", "exps", "date"} or {"error", "date"}
_oi_queue = []
_oi_running = False


def _today():
    return datetime.now(ET).strftime("%Y-%m-%d")


def _fresh(scanned_at, now=None):
    """Reuse a scan until the next session close (4:15 PM ET on a weekday), and for at most an hour
    while the market is open. Holidays count as trading days, which only means an extra rescan."""
    now = now or time.time()
    n, s = datetime.fromtimestamp(now, ET), datetime.fromtimestamp(scanned_at, ET)
    close = n.replace(hour=16, minute=15, second=0, microsecond=0)
    last_close = close if n >= close else close - timedelta(days=1)
    while last_close.weekday() >= 5:
        last_close -= timedelta(days=1)
    if s < last_close:
        return False
    market_open = n.weekday() < 5 and n.replace(hour=9, minute=30, second=0, microsecond=0) <= n < close
    return not market_open or now - scanned_at < INTRADAY_SECONDS


def _rate_limited(e):
    return "rate" in type(e).__name__.lower() or "too many" in str(e).lower() or "429" in str(e)


# ---------- universe ----------

def _keep(sym):
    """Common shares only: drop preferreds, units and warrants (JPM-PD, XYZ-U, XYZ-WT); keep BRK-B."""
    return bool(re.match(r"^[A-Z]{1,5}(-[A-Z])?$", sym)) and not re.search(r"-[PUW]$", sym)


def _yahoo_universe(floor):
    yf = _yf()
    Q = yf.EquityQuery
    out = {}
    for sector in SECTORS:
        q = Q("and", [Q("eq", ["region", "us"]), Q("gte", ["intradaymarketcap", floor]),
                      Q("is-in", ["exchange"] + EXCHANGES), Q("eq", ["sector", sector])])
        offset, total = 0, None
        while total is None or offset < total:
            for attempt in range(3):
                try:
                    r = yf.screen(q, size=250, offset=offset, sortField="intradaymarketcap", sortAsc=False)
                    break
                except Exception as e:
                    if attempt == 2 or not _rate_limited(e):
                        raise
                    time.sleep(10 * (attempt + 1))
            quotes = r.get("quotes") or []
            total = min(r.get("total") or 0, 2000)
            for x in quotes:
                sym = x.get("symbol", "")
                if x.get("quoteType") == "EQUITY" and _keep(sym) and (x.get("marketCap") or 0) >= floor:
                    out[sym] = {"ticker": sym, "name": x.get("shortName") or x.get("longName") or sym,
                                "sector": sector, "mktcap": x.get("marketCap"), "type": "stock",
                                "q": _quote(x.get("regularMarketPrice"), x.get("regularMarketDayHigh"),
                                            x.get("regularMarketDayLow"), x.get("regularMarketTime"))}
            if not quotes:
                break
            offset += len(quotes)
    if len(out) < 50:
        raise QuoteError(f"Yahoo's screener returned only {len(out)} stocks")
    return list(out.values())


def _quote(price, high, low, ts):
    """Today's numbers; date is the session they belong to, or None when unknown (fast_info)."""
    if not price:
        return None
    return {"price": price, "high": high or price, "low": low or price,
            "date": datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d") if ts else None}


def _fast_quote(sym):
    """(market cap, today's quote) from Yahoo's fast_info, for tickers the screener didn't supply."""
    try:
        fi = _yf().Ticker(sym).fast_info
        cap = float(fi.get("market_cap") or 0) or None
        q = _quote(fi["last_price"], fi.get("day_high"), fi.get("day_low"), None)
        return cap, q
    except Exception:
        return None, None


def _fallback_universe(floor):
    def info(sym):
        cap, q = _fast_quote(sym)
        return {"ticker": sym, "name": sym, "sector": "", "mktcap": cap, "type": "stock", "q": q}
    with ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(info, dict.fromkeys(FALLBACK)))
    return [r for r in rows if not r["mktcap"] or r["mktcap"] >= floor]


# ---------- per-stock numbers ----------

def _sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _rsi(xs, n=14):
    """Wilder's RSI of the last close, or None with fewer than n + 1 closes."""
    if len(xs) < n + 1:
        return None
    gains = [max(b - a, 0) for a, b in zip(xs, xs[1:])]
    losses = [max(a - b, 0) for a, b in zip(xs, xs[1:])]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def _support(lows, closes):
    """Nearest support at or below the last close: (level, kind, touches) or None."""
    lows12 = lows[-252:]
    pivots = [lows12[i] for i in range(PIVOT_BARS, len(lows12) - PIVOT_BARS)
              if lows12[i] == min(lows12[i - PIVOT_BARS:i + PIVOT_BARS + 1])
              and lows12[i] < min(lows12[i - PIVOT_BARS:i])]
    # cluster: repeatedly take the 1.5%-wide band holding the most swing lows (the tightest one on a
    # tie), so lows that sit together are never split by where a fixed grid happens to start
    levels, rest = [], sorted(pivots)

    def band(i):
        return [p for p in rest[i:] if p <= rest[i] * (1 + CLUSTER)]
    while rest:
        best = max(range(len(rest)), key=lambda i: (len(band(i)), rest[i] / band(i)[-1]))
        group = band(best)
        levels.append((sum(group) / len(group), "swing low", len(group)))
        rest = rest[:best] + rest[best + len(group):]
    for n, kind in ((200, "200D"), (50, "50D")):
        sma = _sma(closes, n)
        if sma:
            levels.append((sma, kind, None))
    last = closes[-1]
    below = [l for l in levels if l[0] <= last]
    if not below:
        return None
    # highest level under price; on a near-tie prefer the swing low (it has touches behind it)
    best = max(below, key=lambda l: (round(l[0] / last, 3), l[1] == "swing low"))
    return best


def _metrics(base, dates, highs, lows, closes):
    last, prev = closes[-1], closes[-2] if len(closes) > 1 else None
    high52 = max(highs[-252:])
    sma200 = _sma(closes, 200)
    sup = _support(lows, closes)
    weekly, week = [], None
    for d, c in zip(dates, closes):
        k = d.isocalendar()[:2]
        if k != week:
            weekly.append(c)
            week = k
        else:
            weekly[-1] = c
    base = {k: v for k, v in base.items() if k != "q"}
    row = dict(base, price=round(last, 2), day_pct=(last / prev - 1) if prev else None,
               high52=round(high52, 2), off_high=last / high52 - 1,
               sma200=round(sma200, 2) if sma200 else None, vs200=(last / sma200 - 1) if sma200 else None,
               sma50=round(_sma(closes, 50), 2) if _sma(closes, 50) else None,
               chg1y=last / closes[0] - 1, spark=[round(c, 2) for c in weekly[-53:]],
               last_date=dates[-1].strftime("%Y-%m-%d"), support=None, support_type=None, touches=None,
               dist=None, held=False)
    rd, rw = _rsi(closes), _rsi(weekly)
    row.update(rsi_d=round(rd, 1) if rd is not None else None, rsi_w=round(rw, 1) if rw is not None else None)
    for n, key in ((50, "50"), (200, "200")):
        sma = _sma(closes, n)
        row["held" + key] = bool(sma and min(closes[-BREAK_DAYS:]) >= sma * (1 - BREAK))
    if sup:
        level, kind, touches = sup
        row.update(support=round(level, 2), support_type=kind, touches=touches, dist=last / level - 1,
                   held=min(closes[-BREAK_DAYS:]) >= level * (1 - BREAK))
    return row


def _with_today(d, q):
    """Yahoo's daily download often has no close yet for the current session (NaN until the bar is
    finalized), so fill today's bar from the live quote."""
    if q and len(d.index):
        import pandas as pd
        day = pd.Timestamp(q["date"]) if q["date"] else d.index.max()   # no date: only fill a blank last bar
        if day > d.index.max():
            d.loc[day] = [None, None, None]
        if day in d.index and d.loc[day].isna().any():
            d.loc[day] = [max(q["high"], q["price"]), min(q["low"], q["price"]), q["price"]]
    return d.dropna()


def _history(universe):
    """Daily bars for every ticker, HIST_BATCH at a time. Returns (rows, failed tickers)."""
    yf = _yf()
    rows, failed = {}, []
    by_sym = {u["ticker"]: u for u in universe}
    syms = list(by_sym)
    for i in range(0, len(syms), HIST_BATCH):
        batch = syms[i:i + HIST_BATCH]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(batch, period="1y", interval="1d", auto_adjust=False, group_by="ticker",
                                 threads=True, progress=False)
                break
            except Exception as e:
                if attempt == 2:
                    df = None
                else:
                    time.sleep(10 * (attempt + 1) if _rate_limited(e) else 2)
        for sym in batch:
            try:
                d = df[sym] if len(batch) > 1 else df
                d = _with_today(d[["High", "Low", "Close"]].copy(), by_sym[sym].get("q"))
                if len(d) < 60:
                    raise ValueError("not enough history")
                rows[sym] = _metrics(by_sym[sym], list(d.index), d["High"].tolist(), d["Low"].tolist(),
                                     d["Close"].tolist())
            except Exception:
                failed.append(sym)
        with _lock:
            _job["done"] = min(i + HIST_BATCH, len(syms))
    return rows, failed


# ---------- option open interest ----------

def _fetch_oi(sym):
    yf = _yf()
    for attempt in range(3):
        try:
            tk = yf.Ticker(sym)
            exps = list(tk.options)
            calls = puts = 0
            for e in exps:
                ch = tk.option_chain(e)
                calls += int(ch.calls["openInterest"].fillna(0).sum())
                puts += int(ch.puts["openInterest"].fillna(0).sum())
            return {"oi": calls + puts, "calls": calls, "puts": puts, "exps": len(exps), "date": _today()}
        except Exception as e:
            if attempt == 2 or not _rate_limited(e):
                return {"error": f"{type(e).__name__}: {str(e)[:100]}", "date": _today(), "at": time.time()}
            time.sleep(15 * (attempt + 1))


def _oi_known(sym):
    """Today's OI is in hand (or a lookup failed under 30 minutes ago; it is retried after that)."""
    hit = _oi.get(sym)
    if hit is None or hit.get("date") != _today():
        return False
    return "error" not in hit or time.time() - hit.get("at", 0) < 1800


def _queue_oi(syms, front=False):
    """Fetch OI for these tickers in the background (front=True jumps the queue)."""
    global _oi_running
    with _lock:
        new = [s for s in syms if not _oi_known(s) and s not in _oi_queue]
        _oi_queue[:0] = new if front else []
        if not front:
            _oi_queue.extend(new)
        if not _oi_queue or _oi_running:
            return
        _oi_running = True
    threading.Thread(target=_oi_worker, daemon=True).start()


def _oi_worker():
    global _oi_running
    done = 0
    try:
        with ThreadPoolExecutor(OI_WORKERS) as pool:
            while True:
                with _lock:
                    batch = [_oi_queue.pop(0) for _ in range(min(OI_WORKERS, len(_oi_queue)))]
                if not batch:
                    break
                for sym, res in zip(batch, pool.map(_fetch_oi, batch)):
                    with _lock:
                        _oi[sym] = res
                done += len(batch)
                if done % 20 < OI_WORKERS:
                    _save()
    finally:
        with _lock:
            _oi_running = False
        _save()


# ---------- scan ----------

def _save():
    with _lock:
        snap = {"data": _data, "oi": _oi}
        try:
            with open(CACHE_FILE + ".tmp", "w") as f:
                json.dump(snap, f)
            os.replace(CACHE_FILE + ".tmp", CACHE_FILE)
        except (OSError, TypeError, ValueError):
            pass


def _load():
    global _data, _oi
    try:
        with open(CACHE_FILE) as f:
            snap = json.load(f)
        _data, _oi = snap.get("data"), snap.get("oi") or {}
    except (OSError, ValueError):
        pass


def _scan(floor):
    global _data
    with _lock:
        _job.update(running=True, phase="Finding stocks", done=0, total=0, started=time.time(), error=None)
    try:
        try:
            universe, source = _yahoo_universe(floor), "Yahoo Finance screener"
        except Exception as e:
            traceback.print_exc()
            prev = [r for r in ((_data or {}).get("rows") or {}).values()
                    if r["type"] == "stock" and (r["mktcap"] or 0) >= floor and r.get("sector")]
            if len(prev) >= 50:   # usually rate limiting: rescan the stocks from the last good scan
                universe = [{k: r[k] for k in ("ticker", "name", "sector", "mktcap", "type")} for r in prev]
                source = f"last scan's stock list (Yahoo screener failed: {e})"
            else:
                universe, source = _fallback_universe(floor), f"built-in large-cap list (Yahoo screener failed: {e})"
        with ThreadPoolExecutor(8) as pool:
            etf_quotes = list(pool.map(_fast_quote, ETFS))
        universe += [{"ticker": s, "name": n, "sector": "ETF", "mktcap": None, "type": "etf", "q": q}
                     for (s, n), (_, q) in zip(ETFS.items(), etf_quotes)]
        with _lock:
            _job.update(phase="Downloading 1 year of prices", total=len(universe))
        rows, failed = _history(universe)
        if len(rows) < len(universe) * 0.5:
            raise QuoteError(f"Yahoo returned prices for only {len(rows)} of {len(universe)} tickers "
                             "(rate limited?). Try again in a few minutes.")
        with _lock:
            _data = {"scanned_at": time.time(), "floor": floor, "source": source, "rows": rows, "failed": failed}
            _job.update(phase="Checking option open interest", done=0, total=0)
        _save()
        near = sorted((r for r in rows.values() if r["dist"] is not None and 0 <= r["dist"] <= OI_NEAR and r["held"]),
                      key=lambda r: -(r["mktcap"] or 0))
        _queue_oi([r["ticker"] for r in near], front=True)
    except Exception as e:
        traceback.print_exc()
        with _lock:
            _job["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _lock:
            _job.update(running=False, finished=time.time())


def _start_scan(floor):
    with _lock:
        if _job["running"]:
            return
        _job["running"] = True
    threading.Thread(target=_scan, args=(floor,), daemon=True).start()


RSI_MODES = ("any", "oversold", "overbought")   # daily AND weekly RSI below RSI_LOW / above RSI_HIGH
RSI_LOW, RSI_HIGH = 40, 70
LEVELS = ("any", "50d", "200d", "ma")   # support-level filter: any level, the 50-day, the 200-day, or either


def _at_ma(r, level, near):
    """The row re-pointed at its 50-day / 200-day MA if the close is 0% to near% above it (and it hasn't
    just broken it), else None. With level "ma" the closer of the two that qualify wins."""
    hits = []
    for key, kind in (("50", "50D"), ("200", "200D")):
        if level not in ("ma", key + "d"):
            continue
        sma = r.get("sma" + key)
        if not sma or not r.get("held" + key, True):   # rows cached before this filter lack held50/held200
            continue
        dist = r["price"] / sma - 1
        if 0 <= dist <= near / 100:
            hits.append(dict(r, support=sma, support_type=kind, touches=None, dist=dist, held=True))
    return min(hits, key=lambda h: h["dist"]) if hits else None


RSI_MODES = ("any", "oversold", "overbought")   # daily AND weekly RSI below RSI_LOW / above RSI_HIGH
RSI_LOW, RSI_HIGH = 40, 70


def screen(min_cap=DEFAULT_MIN_CAP, min_oi=DEFAULT_MIN_OI, near=DEFAULT_NEAR, stocks_only=True, at_support=True,
           refresh=False, level="any", rsi="any"):
    if _data is None and not _oi:
        with _lock:
            _load()
    min_cap = max(LOWEST_CAP, min_cap)
    floor = min(min_cap, DEFAULT_MIN_CAP)
    data = _data
    stale = data is None or data["floor"] > floor or not _fresh(data["scanned_at"])
    recent = data and time.time() - data["scanned_at"] < 60
    failed_lately = _job["error"] and time.time() - _job["finished"] < RETRY_SECONDS
    if (stale or refresh) and not _job["running"] and not recent and not (failed_lately and not refresh):
        _start_scan(floor)

    def progress():
        with _lock:
            return dict(_job, oi_queue=len(_oi_queue), oi_running=_oi_running)
    base = {"params": {"min_cap": min_cap, "min_oi": min_oi, "near": near, "stocks_only": stocks_only,
                       "at_support": at_support, "level": level, "rsi": rsi},
            "job": progress(), "stale": stale, "now": time.time(),
            "oi_note": "Total open interest across every listed expiration (calls + puts), from Yahoo; "
                       "exchanges update it once a day, overnight."}
    if data is None:
        return dict(base, status="loading", rows=[], scanned=0, matches=0, oi_pending=0)
    rows = data["rows"].values()
    pool = [r for r in rows if r["type"] == "stock" or not stocks_only]
    picked, want_oi = [], []
    for r in pool:
        if r["type"] == "stock" and r["mktcap"] is not None and r["mktcap"] < min_cap:   # None: built-in list, all large caps
            continue
        if level != "any":
            ma = _at_ma(r, level, near)
            if ma:
                r = ma
            elif at_support and rsi == "any":
                continue
            flag = bool(ma)
        else:
            flag = bool(r["dist"] is not None and 0 <= r["dist"] <= near / 100 and r["held"])
        if rsi != "any":
            d, w = r.get("rsi_d"), r.get("rsi_w")   # rows cached before RSI existed have none until the next scan
            if d is None or w is None or not ((d < RSI_LOW and w < RSI_LOW) if rsi == "oversold" else (d > RSI_HIGH and w > RSI_HIGH)):
                continue
        elif at_support and not flag:
            continue
        r = dict(r, at_support=flag)
        hit = _oi.get(r["ticker"]) or {}
        today = _oi_known(r["ticker"])
        r.update(oi=hit.get("oi"), oi_calls=hit.get("calls"), oi_puts=hit.get("puts"), oi_exps=hit.get("exps"),
                 oi_date=hit.get("date"), oi_error=hit.get("error") if today else None)
        if not today and flag:
            # none yet, or yesterday's number: show what we have, fetch today's. Only stocks at support
            # are looked up; checking all ~950 would take ~15 minutes and get Yahoo to rate limit the tracker.
            want_oi.append(r["ticker"])
        picked.append(r)
    if want_oi:
        want_oi.sort(key=lambda s: -(data["rows"][s]["mktcap"] or 0))
        _queue_oi(want_oi)
    pending = [r for r in picked if r["oi"] is None and r["at_support"] and not r.get("oi_error")]
    # stocks not at support (shown only with at_support off) whose OI was never looked up stay in the list
    out = [r for r in picked if (r["oi"] >= min_oi if r["oi"] is not None else not r["at_support"])]
    out.sort(key=lambda r: -(r["mktcap"] or 0))
    return dict(base, status="ok", rows=out, scanned=len(pool), matches=len(out), oi_pending=len(pending),
                oi_errors=sum(1 for r in picked if r.get("oi_error")), scanned_at=data["scanned_at"],
                last_date=max((r["last_date"] for r in rows), default=None), source=data["source"],
                failed=len(data.get("failed") or []), job=progress())
