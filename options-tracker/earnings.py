"""Upcoming earnings for your earnings watchlist plus every ticker you hold open, for the Earnings
dropdown in the tracker.

Source: Yahoo Finance via yfinance. `Ticker.info` gives the report time (earningsTimestamp) and whether
the date is confirmed (isEarningsDateEstimate). `Ticker.calendar` gives the analysts' EPS estimate.
ETFs and funds have no earnings and are skipped. Each ticker is cached for 6 hours in memory and in
earnings_cache.json (gitignored), so restarts don't refetch everything. The watchlist itself is saved
in earnings_watch.json (gitignored).
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "earnings_cache.json")
WATCH_FILE = os.path.join(HERE, "earnings_watch.json")
# The stocks from the trading plan watchlist (TRADING_PLAN_CHECKLIST.md 1.6); QQQ and SPY are ETFs.
DEFAULT_WATCH = ["INTC", "TSLA", "AMD", "AAPL", "PLTR"]
REFRESH_SECONDS = 6 * 3600
ET = ZoneInfo("America/New_York")
TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

_lock = threading.Lock()
_cache = None


def watchlist():
    try:
        with open(WATCH_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return list(DEFAULT_WATCH)


def save_watchlist(tickers):
    out = []
    for t in tickers or []:
        t = str(t).strip().upper()
        if not TICKER.match(t):
            raise ValueError(f"{t or 'blank'} is not a ticker")
        if t not in out:
            out.append(t)
    with open(WATCH_FILE, "w") as f:
        json.dump(out[:60], f, indent=2)
    return out[:60]


def _load_cache():
    global _cache
    if _cache is None:
        try:
            with open(CACHE_FILE) as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _session(ts):
    """'bmo' before the 9:30 ET open, 'amc' at or after the 4:00 ET close, else 'dmh' (during market hours)."""
    t = datetime.fromtimestamp(ts, ET)
    hm = t.hour * 60 + t.minute
    return "bmo" if hm < 9 * 60 + 30 else "amc" if hm >= 16 * 60 else "dmh"


def _fetch(sym):
    tk = yf.Ticker(sym)
    info = tk.info or {}
    row = {"ticker": sym, "name": info.get("shortName") or sym, "fetched": time.time()}
    if info.get("quoteType") not in (None, "EQUITY"):
        return dict(row, kind=info.get("quoteType").lower())   # ETF, index, etc.: no earnings
    now = time.time()
    # earningsTimestamp is the next confirmed report, or the LAST one when the next isn't set yet;
    # earningsTimestampStart is Yahoo's (maybe estimated) next date.
    ts = next((t for t in (info.get("earningsTimestamp"), info.get("earningsTimestampStart"))
               if t and t > now - 86400), None)
    if not ts:
        return dict(row, kind="none")
    est = bool(info.get("isEarningsDateEstimate")) or ts != info.get("earningsTimestamp")
    eps = None
    try:
        cal = tk.calendar or {}
        eps = cal.get("Earnings Average")
    except Exception:
        pass
    d = datetime.fromtimestamp(ts, ET)
    session = _session(ts)
    if session == "dmh":
        # Yahoo puts unknown report times at 3:00 PM ET; the earnings call time (same day) is a better guide,
        # and if that doesn't settle it the time isn't announced yet ("tns", time not supplied).
        call = info.get("earningsCallTimestampStart")
        same_day = call and datetime.fromtimestamp(call, ET).date() == d.date()
        session = _session(call) if same_day and _session(call) != "dmh" else "tns"
    return dict(row, kind="equity", ts=ts, date=d.strftime("%Y-%m-%d"), session=session,
                estimated=est, eps_est=round(eps, 2) if isinstance(eps, (int, float)) else None)


def _get(sym):
    cache = _load_cache()
    hit = cache.get(sym)
    if hit and time.time() - hit["fetched"] < REFRESH_SECONDS and not (hit.get("ts") and hit["ts"] < time.time() - 86400):
        return hit
    try:
        row = _fetch(sym)
    except Exception as e:
        if hit:
            return dict(hit, stale=True)
        return {"ticker": sym, "kind": "error", "error": str(e)[:120], "fetched": 0}
    with _lock:
        cache[sym] = row
    return row


def upcoming(held):
    """held: {ticker: latest open expiration 'YYYY-MM-DD'} for open positions."""
    watch = watchlist()
    tickers = list(dict.fromkeys(watch + sorted(held)))
    with ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(_get, tickers))
    with _lock:
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump(_load_cache(), f)
        except OSError:
            pass
    out = []
    for r in rows:
        r = dict(r, watch=r["ticker"] in watch, held=r["ticker"] in held)
        exp = held.get(r["ticker"])
        if exp:
            r["exp"] = exp
            # an open position still alive when the report lands carries the earnings move
            # (an after-the-close report on expiration day lands after the options have expired)
            r["at_risk"] = bool(r.get("date")) and (exp > r["date"] or exp == r["date"] and r["session"] != "amc")
        out.append(r)
    out.sort(key=lambda r: (r.get("ts") or float("inf"), r["ticker"]))
    return {"watch": watch, "rows": out, "now": time.time()}
