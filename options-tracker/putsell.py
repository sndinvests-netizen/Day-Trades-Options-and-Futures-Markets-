"""Sell Puts: cash-secured puts that pay a target return per week.

For a stock, every put 30 days out or less with delta between 0.08 and 0.30 is priced at the bid.
Return per week = bid / strike x 7 / max(days, 7), on the full cash set aside (strike x 100), so a
6-day put and a 27-day put compare fairly (a put with under a week left counts as a full week).

lookup(ticker): every such put for one stock, plus its earnings date. The page filters them
  (target delta, minimum return, open interest, spread, earnings) so its controls are instant.
scan(): the same check across the Screener's $10B+ stocks in the background; for each stock it
  keeps the best-paying put near 20 delta and near 15 delta.
"""
import json
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import earnings
import options_data as od
from options_data import ET, QuoteError, yahoo_symbol

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "putsell_cache.json")

MAX_DAYS = 30
DELTA_MIN, DELTA_MAX = 0.08, 0.30     # puts outside this band aren't returned
CALL_DELTA = (0.15, 0.60)             # covered calls are written at 30 delta or more; show 0.15 up for context
DELTA = {"put": (DELTA_MIN, DELTA_MAX), "call": CALL_DELTA}
TARGETS = {"20": (0.175, 0.225), "15": (0.125, 0.175)}   # |delta| band for each target
MIN_OI = 100
MAX_SPREAD = 0.30                    # (ask - bid) / mid
EARN_CHECK_RET = 0.010               # the scan looks up earnings only for stocks paying at least 1%/wk
MIN_CAP = 10e9
WORKERS = 3
CBOE_GAP = 0.6                      # seconds between CBOE requests (faster gets HTTP 429)
RATE_WAIT = (20, 60, 120)            # back-off after a rate limit, then retry
FRESH_SECONDS = 4 * 3600             # a scan is reused for 4 hours unless Refresh is pressed
RETRY_SECONDS = 300
LOOKUP_SECONDS = 300                 # one ticker's lookup is reused for 5 minutes
SCREENER_WAIT = 600

_lock = threading.Lock()
_job = {"running": False, "done": 0, "total": 0, "phase": "", "started": 0, "finished": 0, "error": None}
_data = None
_loaded = False
_partial = None
_one = {}


def _today():
    return datetime.now(ET).date()


def _earn(sym):
    try:
        e = earnings._get(sym)
    except Exception:
        return {"earn": None}
    return {"earn": e.get("date"), "earn_est": bool(e.get("estimated")), "earn_session": e.get("session"),
            "name": e.get("name")}


def _row(right, spot, k, exp, dte, d, bid, ask, oi, iv):
    """One contract. A cash-secured put ties up the strike; a covered call ties up the shares at
    today's price, so its return is on the stock price."""
    basis = k if right == "put" else spot
    r = {"strike": k, "exp": exp, "dte": dte, "delta": round(d, 3), "bid": bid, "ask": ask, "oi": oi,
         "iv": round(iv, 3), "spread": round((ask - bid) / ((bid + ask) / 2), 3),
         "ret_week": round(bid / basis * 7 / max(dte, 7), 5), "ret_trade": round(bid / basis, 5)}
    if right == "call":   # shares called away at the strike: premium plus the gain up to the strike
        r["ret_called"] = round((bid + k - spot) / spot, 5)
    return r


def _otm(right, k, spot):
    return k < spot if right == "put" else k > spot


def _puts(sym, spot=None, right="put"):
    """(spot, contracts): every out-of-the-money put (or call) 1..MAX_DAYS days out with |delta| in
    [DELTA_MIN, DELTA_MAX] and a bid."""
    spot = spot or od.spot_price(sym)
    today = _today()
    out = []
    for e in od.expirations(sym):
        dte = (date.fromisoformat(e) - today).days
        if dte < 0 or dte > MAX_DAYS:
            continue
        try:
            rows = od.chain_rows(sym, e)[right]
        except QuoteError:
            continue
        t = od.years_to_expiry(e)
        if t <= 0:
            continue
        for p in rows:
            k, bid, ask = p["strike"], p["bid"], p["ask"]
            if not _otm(right, k, spot) or bid <= 0 or ask <= 0 or ask < bid:
                continue
            mid = (bid + ask) / 2
            iv = od.implied_vol(right, spot, k, t, mid) or p["iv"]
            if not iv:
                continue
            d = od.greeks(right, spot, k, t, iv)["delta"]
            if not DELTA[right][0] <= abs(d) <= DELTA[right][1]:
                continue
            out.append(_row(right, spot, k, e, dte, d, bid, ask, p["oi"], iv))
    out.sort(key=lambda p: -p["ret_week"])
    return spot, out


_gate = threading.Lock()
_last = [0.0]


def _throttle():
    with _gate:
        wait = _last[0] + CBOE_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()


class RateLimited(QuoteError):
    pass


def _cboe_puts(sym, right="put"):
    """Same as _puts, from CBOE's delayed-quote feed: the whole chain in one request, with delta."""
    import urllib.error
    import urllib.request
    _throttle()
    url = od.CBOE_URL.format(sym.replace("-", "."))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25) as f:
            data = json.load(f)["data"]
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise RateLimited(f"CBOE HTTP {e.code}")
        raise QuoteError(f"{sym}: CBOE HTTP {e.code}")
    spot = od._num(data.get("current_price"))
    if not spot:
        raise QuoteError(f"{sym}: no price from CBOE")
    today = _today()
    out = []
    for o in data.get("options") or []:
        m = re.search(r"(\d{2})(\d{2})(\d{2})%s(\d{8})$" % ("P" if right == "put" else "C"), o.get("option", ""))
        if not m:
            continue
        exp = f"20{m[1]}-{m[2]}-{m[3]}"
        dte = (date.fromisoformat(exp) - today).days
        k = int(m[4]) / 1000
        bid, ask, d = od._num(o.get("bid")), od._num(o.get("ask")), od._num(o.get("delta"))
        if dte < 0 or dte > MAX_DAYS or not _otm(right, k, spot) or bid <= 0 or ask < bid or not DELTA[right][0] <= abs(d) <= DELTA[right][1]:
            continue
        out.append(_row(right, spot, k, exp, dte, -abs(d) if right == "put" else abs(d), bid, ask,
                        int(od._num(o.get("open_interest"))), od._num(o.get("iv"))))
    out.sort(key=lambda p: -p["ret_week"])
    return spot, out


_cboe_off = [0.0]                    # CBOE is skipped until this time after it rate limits us
CBOE_COOLDOWN = 120


def _limited(e):
    return isinstance(e, RateLimited) or "Too Many Requests" in str(e) or "Rate limited" in str(e)


def _any_puts(sym, spot=None):
    """CBOE first (one request per stock), Yahoo when CBOE has nothing or is rate limiting us.
    Waits and retries only when both are rate limited."""
    for wait in (0,) + RATE_WAIT:
        if wait:
            with _lock:
                _job["phase"] = f"Rate limited, waiting {wait}s"
            time.sleep(wait)
            with _lock:
                _job["phase"] = "Pricing puts 30 days out or less"
        if time.time() >= _cboe_off[0]:
            try:
                got = _cboe_puts(sym)
                if got[1]:
                    return got
            except RateLimited:
                _cboe_off[0] = time.time() + CBOE_COOLDOWN
            except QuoteError:
                pass
        try:
            return _puts(sym, spot)
        except QuoteError as e:
            if not _limited(e):
                raise
    raise QuoteError(f"{sym}: rate limited")


def _usable(p):
    return p["oi"] >= MIN_OI and p["spread"] <= MAX_SPREAD


def _best(puts, band, earn):
    """Best-paying put in the delta band: usable and expiring before earnings first, then anything."""
    inb = [p for p in puts if band[0] <= abs(p["delta"]) <= band[1]]
    if not inb:
        return None
    clean = [p for p in inb if _usable(p) and not (earn and earn <= p["exp"])]
    p = dict(max(clean or inb, key=lambda p: p["ret_week"]))
    p["usable"] = _usable(p)
    p["earn_before"] = bool(earn and earn <= p["exp"])
    return p


def _screener_row(sym):
    import screener
    data = screener._data
    return (data or {}).get("rows", {}).get(sym) or {}


def lookup(ticker, right="put"):
    """Every put (right="put", cash-secured) or call (right="call", covered) for one stock."""
    if right not in ("put", "call"):
        raise QuoteError("right must be put or call")
    sym = yahoo_symbol(str(ticker or "").strip().upper())
    if not re.fullmatch(r"[A-Z0-9.^=-]{1,12}", sym):
        raise QuoteError("Enter a ticker")
    hit = _one.get((sym, right))
    if hit and time.time() - hit[0] < LOOKUP_SECONDS:   # quotes reused; your holding is always re-read
        return dict(hit[1], holding=_holding(sym) if right == "call" else None)
    try:
        spot, puts = _puts(sym, right=right)
    except QuoteError as e:
        # Yahoo rate limits (sometimes as an empty option list): try CBOE, keep Yahoo's error if that fails too
        try:
            spot, puts = _cboe_puts(sym, right)
        except QuoteError:
            raise e
    sr = _screener_row(sym)
    e = _earn(sym)
    row = {"ticker": sym, "name": sr.get("name") or e.get("name") or sym, "price": round(spot, 2),
           "sma200": sr.get("sma200"), "sma50": sr.get("sma50"), "earn": e.get("earn"),
           "earn_est": e.get("earn_est"), "earn_session": e.get("earn_session"), "puts": puts, "right": right,
           "holding": _holding(sym) if right == "call" else None,
           "settings": settings(), "at": time.time()}
    _one[(sym, right)] = (time.time(), row)
    return row


def _holding(sym):
    """Your open share lots in `sym` from My Trades: shares, broker average cost, the average cost
    after the option P&L you've realized on the ticker (calls and puts closed, expired or assigned),
    and how many of the 100-share lots already have a call written against them."""
    import tracker
    try:
        trades = tracker.load(tracker.DEFAULT_DB)
    except Exception:
        return None
    h = next((x for x in tracker.share_holdings(trades) if x["ticker"] == sym), None)
    if not h or not h["shares"]:
        return None
    opt = [t for t in trades if t["ticker"] == sym and tracker.kind(t) != "stock"]
    realized = sum(t.get("realized") or 0 for t in opt if t["status"] != "open")
    short_calls = sum(t["contracts"] for t in opt if t["status"] == "open" and tracker.kind(t) == "call" and t["side"] == "sell")
    lots = h["shares"] // tracker.MULTIPLIER
    return {"shares": h["shares"], "avg_cost": round(h["avg_cost"], 4), "option_pnl": round(realized, 2),
            "net_cost": round((h["cost"] + h["fees"] - realized) / h["shares"], 4),
            "lots": lots, "calls_open": short_calls, "lots_free": max(0, lots - short_calls)}


def settings():
    return {"max_days": MAX_DAYS, "targets": TARGETS, "min_oi": MIN_OI, "max_spread": MAX_SPREAD,
            "delta_min": DELTA_MIN, "delta_max": DELTA_MAX, "call_delta": CALL_DELTA, "min_cap": MIN_CAP}


# ---------- market scan ----------

def _universe():
    import screener
    if screener._data is None:
        screener._load()
    if screener._data is None or not screener._fresh(screener._data["scanned_at"]):
        _job["phase"] = "Waiting for the Screener's market scan"
        screener._start_scan(min(MIN_CAP, screener.DEFAULT_MIN_CAP))
        start = time.time()
        while time.time() - start < SCREENER_WAIT:
            time.sleep(3)
            if not screener._job["running"] and (screener._data is not None or screener._job.get("error")):
                break
    data = screener._data
    if data is None:
        raise QuoteError("The Screener has no market scan yet (Yahoo rate limiting?). Try again in a few minutes.")
    rows = [r for r in data["rows"].values()
            if r.get("type") == "stock" and (r.get("mktcap") or 0) >= MIN_CAP and r.get("price")]
    rows.sort(key=lambda r: -(r.get("mktcap") or 0))
    return rows, data["scanned_at"]


def _scan():
    global _data, _partial
    try:
        universe, screener_at = _universe()
        rows, failed = {}, {}
        _partial = rows
        with _lock:
            _job.update(phase="Pricing puts 30 days out or less", done=0, total=len(universe))

        def one(r):
            sym = r["ticker"]
            try:
                spot, puts = _any_puts(sym, r["price"])
                bands = {k: [p for p in puts if b[0] <= abs(p["delta"]) <= b[1]] for k, b in TARGETS.items()}
                top = max((p["ret_week"] for ps in bands.values() for p in ps), default=0)
                if top:
                    e = _earn(sym) if top >= EARN_CHECK_RET else {"earn": None}
                    row = {"ticker": sym, "name": r.get("name") or sym, "price": round(spot, 2),
                           "sma200": r.get("sma200"), "sma50": r.get("sma50"), "sector": r.get("sector"),
                           "mktcap": r.get("mktcap"), "earn": e.get("earn"), "earn_est": e.get("earn_est"),
                           "earn_checked": top >= EARN_CHECK_RET,
                           "best": {k: _best(puts, b, e.get("earn")) for k, b in TARGETS.items()}}
                    with _lock:
                        rows[sym] = row
            except QuoteError as e:
                failed[sym] = str(e)
            except Exception as e:   # one bad ticker shouldn't sink the scan
                failed[sym] = f"{type(e).__name__}: {e}"
            with _lock:
                _job["done"] += 1

        with ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(one, universe))
        with _lock:
            _data = {"scanned_at": time.time(), "screener_at": screener_at, "universe": len(universe),
                     "rows": list(rows.values()), "failed": failed}
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(_data, f)
            os.replace(tmp, CACHE_FILE)
            _partial = None
            _job.update(running=False, finished=time.time(), error=None, phase="")
    except Exception as e:
        traceback.print_exc()
        with _lock:
            _partial = None
            _job.update(running=False, finished=time.time(), error=str(e), phase="")


def scan(refresh=False):
    """Latest market scan; starts one in the background when there's none, it's stale, or refresh
    is set. Poll while job.running is true."""
    global _data, _loaded
    with _lock:
        if not _loaded:
            _loaded = True
            try:
                with open(CACHE_FILE) as f:
                    _data = json.load(f)
            except (OSError, ValueError):
                _data = None
        data = _data
        stale = data is None or time.time() - data["scanned_at"] > FRESH_SECONDS
        failed_lately = _job["error"] and time.time() - _job["finished"] < RETRY_SECONDS
        start = (refresh or (stale and not failed_lately)) and not _job["running"]
        if start:
            _job.update(running=True, started=time.time(), error=None, done=0, total=0, phase="Starting")
        job = dict(_job)
    if start:
        threading.Thread(target=_scan, daemon=True).start()
    base = {"job": job, "stale": stale, "settings": settings()}
    if data is None:
        with _lock:
            part = list((_partial or {}).values())
        return dict(base, status="loading", rows=part, failed={})
    return dict(base, status="ok", rows=data["rows"], failed=data.get("failed", {}),
                scanned_at=data["scanned_at"], screener_at=data.get("screener_at"), universe=data.get("universe"))
