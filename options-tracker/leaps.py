"""LEAPS Finder: ranks the stocks on a watchlist for buying long-dated calls (LEAPS).

Two passes for each ticker:

1. The stock. LEAPS are a one-to-two-year bet on a stock trending up, so the stock is scored on
   - trend: close above its 200-day and 50-day SMAs, and the 50-day above the 200-day
   - relative strength: 6- and 12-month return minus SPY's over the same span
   - Rule of 40: revenue growth % + free-cash-flow margin % (Yahoo `info`), scored up to 40

2. The contract. Every call expiring at least MIN_DAYS out with a Black-Scholes delta between
   DELTA_LO and DELTA_HI (deep in the money, so it tracks the stock) is a candidate. For each one:
   - cost: the mid of a real two-sided quote, else the tracker's Black-Scholes estimate (flagged "est")
   - time value per year: (cost - intrinsic) / years to expiry, as a % of the stock price. This is
     what the position costs you to hold, the number that matters most for a stock replacement.
   - breakeven: strike + cost, as a % move from today, and per year
   - spread: (ask - bid) / mid; open interest; leverage = delta x price / cost
   The best contract is the liquid one (OI >= MIN_OI, spread <= MAX_SPREAD, real quote) with the
   least time value per year; if none is liquid, the cheapest one is shown, flagged.
   - IV / HV: the contract's IV over the stock's 1-year realized volatility. Yahoo has no free IV
     history, so a real IV rank isn't available; IV well above realized vol means LEAPS are pricey.

Score (0-100) = stock 40 (trend 15, relative strength 15, Rule of 40 10)
              + contract 60 (time value/yr 25, breakeven/yr 10, IV/HV 10, spread 10, open interest 5)
              - 15 when no candidate contract is liquid.

Earnings within EARNINGS_WARN_DAYS days are flagged, not scored. It's a scanner, not advice.

Scans run in a background thread (a request never waits on Yahoo) and the last one is kept in
leaps_cache.json (gitignored). The watchlist is leaps_watch.json (gitignored); until it exists, the
tracker's earnings watchlist is used.
"""
import json
import math
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import earnings
from options_data import (ET, QuoteError, _price_ok, _yf, chain_rows, expirations, greeks, option_spot,
                          years_to_expiry, yahoo_symbol)

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "leaps_cache.json")
WATCH_FILE = os.path.join(HERE, "leaps_watch.json")

MIN_DAYS = 365
DELTA_LO, DELTA_HI = 0.70, 0.85
MIN_OI = 100
MAX_SPREAD = 0.05
EARNINGS_WARN_DAYS = 14
FRESH_SECONDS = 4 * 3600      # a scan is reused for 4 hours unless Refresh is pressed
WORKERS = 3                   # more gets Yahoo rate limiting
MAX_TICKERS = 60
ALTERNATES = 4                # other candidate contracts returned per ticker
THIN_PENALTY = 15             # points off when no candidate is liquid (hard to get filled, hard to exit)

_lock = threading.Lock()
_data = None
_job = {"running": False, "done": 0, "total": 0, "phase": "", "started": 0, "finished": 0, "error": None}


# ---------- watchlist ----------

def watchlist():
    try:
        with open(WATCH_FILE) as f:
            tickers = json.load(f)
        if isinstance(tickers, list) and tickers:
            return tickers
    except (OSError, ValueError):
        pass
    return earnings.watchlist()


def save_watchlist(tickers):
    if not isinstance(tickers, list):
        raise ValueError("tickers must be a list")
    clean = []
    for t in tickers:
        t = str(t).strip().upper()
        if not t:
            continue
        if not all(c.isalnum() or c in ".-^" for c in t) or len(t) > 10:
            raise ValueError(f"bad ticker: {t}")
        if t not in clean:
            clean.append(t)
    if not clean:
        raise ValueError("add at least one ticker")
    if len(clean) > MAX_TICKERS:
        raise ValueError(f"at most {MAX_TICKERS} tickers")
    with open(WATCH_FILE, "w") as f:
        json.dump(clean, f, indent=2)
    return clean


# ---------- scoring helpers ----------

def _lin(x, best, worst):
    """1.0 at `best`, 0.0 at `worst`, straight line between (either direction), clipped."""
    if x is None:
        return 0.0
    f = (x - worst) / (best - worst)
    return max(0.0, min(1.0, f))


def _sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _ret(xs, n):
    return xs[-1] / xs[-1 - n] - 1 if len(xs) > n and xs[-1 - n] > 0 else None


def _realized_vol(closes, n=252):
    xs = [c for c in closes[-(n + 1):] if c > 0]
    rets = [math.log(b / a) for a, b in zip(xs, xs[1:])]
    if len(rets) < 60:
        return None
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1) * 252)


def _closes(sym, period="2y"):
    yf = _yf()
    try:
        h = yf.Ticker(sym).history(period=period, auto_adjust=True)
    except Exception as e:
        raise QuoteError(f"{sym}: no price history ({e})")
    xs = [float(c) for c in h["Close"].tolist() if c == c and c > 0]
    if len(xs) < 210:
        raise QuoteError(f"{sym}: not enough price history")
    return xs


def _fundamentals(sym):
    yf = _yf()
    try:
        info = yf.Ticker(sym).info or {}
    except Exception:
        info = {}
    growth = info.get("revenueGrowth")
    fcf, rev = info.get("freeCashflow"), info.get("totalRevenue")
    margin = fcf / rev if fcf is not None and rev else None
    r40 = (growth + margin) * 100 if growth is not None and margin is not None else None
    e = earnings._get(sym)   # same dates as the tracker's Earnings dropdown (cached there)
    return {"name": info.get("shortName") or info.get("longName"), "sector": info.get("sector"),
            "mktcap": info.get("marketCap"), "rev_growth": growth, "fcf_margin": margin, "rule40": r40,
            "earnings": e.get("date"), "earnings_session": e.get("session"),
            "earnings_est": bool(e.get("estimated"))}


# ---------- contracts ----------

def _candidates(sym, spot_hint):
    today = datetime.now(ET).date()
    exps = [e for e in expirations(sym) if (datetime.strptime(e, "%Y-%m-%d").date() - today).days >= MIN_DAYS]
    out = []
    for exp in exps:
        rows = chain_rows(sym, exp)["call"]
        spot = option_spot(sym, exp) or spot_hint
        t = years_to_expiry(exp)
        dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        for r in rows:
            g = greeks("call", spot, r["strike"], t, r["iv"])
            if not g or not DELTA_LO <= g["delta"] <= DELTA_HI:
                continue
            real = _price_ok(r)
            cost = (r["bid"] + r["ask"]) / 2 if real else r["mark"]
            if not cost or cost <= 0:
                continue
            intrinsic = max(0.0, spot - r["strike"])
            extrinsic = max(0.0, cost - intrinsic)
            be = (r["strike"] + cost) / spot - 1
            spread = (r["ask"] - r["bid"]) / cost if real else None
            out.append({
                "exp": exp, "dte": dte, "strike": r["strike"], "delta": g["delta"], "theta": g["theta"],
                "iv": r["iv"], "iv_est": r.get("iv_est", False), "est": not real,
                "bid": r["bid"], "ask": r["ask"], "cost": cost, "contract_cost": cost * 100,
                "intrinsic": intrinsic, "extrinsic": extrinsic,
                "tv_year": extrinsic / t / spot, "tv_pct": extrinsic / cost,
                "breakeven": r["strike"] + cost, "be_pct": be, "be_year": be / t,
                "spread": spread, "oi": r["oi"], "volume": r["volume"],
                "leverage": g["delta"] * spot / cost,
                "liquid": real and r["oi"] >= MIN_OI and spread is not None and spread <= MAX_SPREAD,
            })
    return out


def _contract_score(c, rv):
    iv_hv = c["iv"] / rv if rv else None
    parts = {
        "time_value": 25 * _lin(c["tv_year"], 0.03, 0.15),
        "breakeven": 10 * _lin(c["be_year"], 0.03, 0.20),
        "iv_hv": 10 * (_lin(iv_hv, 1.0, 1.6) if iv_hv is not None else 0.5),
        "spread": 10 * (_lin(c["spread"], 0.02, 0.12) if c["spread"] is not None else 0.0),
        "oi": 5 * (_lin(math.log10(max(c["oi"], 1)), 3, 1)),
    }
    return parts, iv_hv


def _stock_score(s):
    trend = (7 if s["vs200"] is not None and s["vs200"] > 0 else 0) \
        + (5 if s["sma50"] and s["sma200"] and s["sma50"] > s["sma200"] else 0) \
        + (3 if s["vs50"] is not None and s["vs50"] > 0 else 0)
    rs = 7.5 * _lin(s["rs6"], 0.15, -0.10) + 7.5 * _lin(s["rs12"], 0.25, -0.15)
    r40 = 10 * _lin(s["rule40"], 40, 0) if s["rule40"] is not None else 5.0   # unknown: half credit
    return {"trend": trend, "rel_strength": rs, "rule40": r40}


# ---------- one ticker ----------

def _analyze(sym, spy):
    sym = yahoo_symbol(sym)
    closes = _closes(sym)
    price = closes[-1]
    sma50, sma200 = _sma(closes, 50), _sma(closes, 200)
    r6, r12 = _ret(closes, 126), _ret(closes, 252)
    s6, s12 = _ret(spy, 126), _ret(spy, 252)
    row = {
        "ticker": sym, "price": price, "sma50": sma50, "sma200": sma200,
        "vs50": price / sma50 - 1 if sma50 else None, "vs200": price / sma200 - 1 if sma200 else None,
        "ret6": r6, "ret12": r12,
        "rs6": r6 - s6 if r6 is not None and s6 is not None else None,
        "rs12": r12 - s12 if r12 is not None and s12 is not None else None,
        "rv": _realized_vol(closes),
        "spark": closes[-252::5],
    }
    row.update(_fundamentals(sym))
    if row["earnings"]:
        days = (datetime.strptime(row["earnings"], "%Y-%m-%d").date() - datetime.now(ET).date()).days
        row["earnings_days"] = days
        row["earnings_soon"] = 0 <= days <= EARNINGS_WARN_DAYS
    stock = _stock_score(row)
    cands = _candidates(sym, price)
    for c in cands:
        c["parts"], c["iv_hv"] = _contract_score(c, row["rv"])
        c["score"] = sum(c["parts"].values())
    liquid = [c for c in cands if c["liquid"]]
    pool = liquid or cands
    pool.sort(key=lambda c: (-c["score"], c["tv_year"]))
    best = pool[0] if pool else None
    others = [c for c in sorted(cands, key=lambda c: -c["score"]) if c is not best][:ALTERNATES]
    penalty = THIN_PENALTY if best and not best["liquid"] else 0
    row.update(stock_parts=stock, stock_score=sum(stock.values()), best=best, alternates=others,
               candidates=len(cands), liquid_count=len(liquid), penalty=penalty,
               score=max(0.0, sum(stock.values()) + (best["score"] if best else 0) - penalty))
    if not cands:
        row["note"] = f"No calls {MIN_DAYS}+ days out with delta {DELTA_LO:.2f}-{DELTA_HI:.2f}"
    elif not liquid:
        row["note"] = f"None liquid (OI >= {MIN_OI}, spread <= {MAX_SPREAD:.0%}); best shown anyway"
    return row


# ---------- scan ----------

def _save():
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_data, f)
    os.replace(tmp, CACHE_FILE)


def _load():
    global _data
    try:
        with open(CACHE_FILE) as f:
            _data = json.load(f)
    except (OSError, ValueError):
        _data = None


def _scan(tickers):
    global _data
    try:
        _job.update(phase="SPY baseline", done=0, total=len(tickers))
        spy = _closes("SPY")
        rows, failed = {}, {}

        def one(sym):
            try:
                rows[sym] = _analyze(sym, spy)
            except QuoteError as e:
                failed[sym] = str(e)
            except Exception as e:   # one bad ticker shouldn't sink the scan
                failed[sym] = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            with _lock:
                _job["done"] += 1

        _job["phase"] = "Scanning"
        with ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(one, tickers))
        with _lock:
            _data = {"scanned_at": time.time(), "tickers": tickers, "rows": list(rows.values()), "failed": failed}
            _save()
            _job.update(running=False, finished=time.time(), error=None, phase="")
    except Exception as e:
        traceback.print_exc()
        with _lock:
            _job.update(running=False, finished=time.time(), error=str(e), phase="")


def _start(tickers):
    with _lock:
        if _job["running"]:
            return
        _job.update(running=True, started=time.time(), error=None)
    threading.Thread(target=_scan, args=(tickers,), daemon=True).start()


def scan(refresh=False):
    """Latest results; starts a background scan when there's none, it's stale, the watchlist changed,
    or refresh is set. Poll while job.running is true."""
    if _data is None:
        with _lock:
            _load()
    tickers = [yahoo_symbol(t) for t in watchlist()]
    data = _data
    stale = (data is None or time.time() - data["scanned_at"] > FRESH_SECONDS
             or sorted(data.get("tickers", [])) != sorted(tickers))
    if (stale or refresh) and not _job["running"]:
        _start(tickers)
    with _lock:
        job = dict(_job)
    base = {"watchlist": tickers, "job": job, "stale": stale,
            "settings": {"min_days": MIN_DAYS, "delta_lo": DELTA_LO, "delta_hi": DELTA_HI, "min_oi": MIN_OI,
                         "max_spread": MAX_SPREAD, "earnings_warn_days": EARNINGS_WARN_DAYS}}
    if data is None:
        return dict(base, status="loading", rows=[], failed={})
    rows = sorted(data["rows"], key=lambda r: -r["score"])
    return dict(base, status="ok", rows=rows, failed=data.get("failed", {}), scanned_at=data["scanned_at"])
