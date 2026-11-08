"""LEAPS Finder: ranks the stocks on a watchlist for buying long-dated calls (LEAPS).

Two passes for each ticker:

1. The stock. LEAPS are a one-to-two-year bet on a stock trending up, so the stock is scored on
   - trend: close above its 200-day and 50-day SMAs, and the 50-day above the 200-day
   - relative strength: 6- and 12-month return minus SPY's over the same span
   - Rule of 40: revenue growth % + free-cash-flow margin % (Yahoo `info`), scored up to 40

2. The contract. Every call expiring at least MIN_DAYS out with a Black-Scholes delta between
   DELTA_LO and DELTA_HI (deep in the money, so it tracks the stock) is a candidate. For each one:
   - cost: the mid of a real two-sided quote, else the tracker's Black-Scholes estimate (flagged "est")
   - time value per year: (cost - intrinsic + dividends you give up) / years to expiry, as a % of the
     stock price. This is what holding the call costs you vs owning the shares, the number that matters
     most for a stock replacement. Intrinsic uses the live stock price, and the dividends are the stock's
     annual dividend rate x years: a call holder doesn't get them, so high-yield stocks aren't flattered.
   - a quote whose time value (dividends included) is under 80% of the interest on the strike is below
     what the call can be worth: a stale or placeholder quote, so it is skipped.
   - breakeven: strike + cost, as a % move from today, and per year
   - spread: (ask - bid) / mid; open interest; leverage = delta x price / cost
   The best contract is the liquid one (OI >= MIN_OI, spread <= MAX_SPREAD, real quote) with the
   highest contract score; if none is liquid, the best-scoring one is shown, flagged "thin".
   - IV / HV: the contract's IV over the stock's 1-year realized volatility. Yahoo has no free IV
     history, so a real IV rank isn't available; IV well above realized vol means LEAPS are pricey.

Score (0-100) = stock 40 (trend 15, relative strength 15, Rule of 40 10)
              + contract 60 (time value/yr 25, breakeven/yr 10, IV/HV 10, spread 10, open interest 5)
              - 15 when no candidate contract is liquid.

Short-term OTM terms (owner request 2026-11-05: "skips or hops, 90 days, 10% / 20% out of the money"):
the same stock score, but the contract pass looks at calls SHORT_MIN_DAYS-SHORT_MAX_DAYS out with the strike
at or just above 10% (or 20%) over the live stock price (never below it). These are all premium, so time value/yr doesn't
apply; the contract score (60) = breakeven vs the stock's 1-sigma move to expiry 25, IV/HV 15, spread 10, OI 10.

Puts (owner request 2026-11-07: "leap puts, same criteria as leap calls, 10 and 20 out of the money as well"):
every term has a put twin ("pleap", "potm10", "potm20") that mirrors it.
- stock: a downtrend scores (close below the 200-day and 50-day, 50-day below the 200-day), relative
  weakness vs SPY scores, and a weak Rule of 40 scores (unknown: half credit).
- LEAPS puts: 365+ days out, delta -0.70 to -0.85. Time value/yr = (cost - intrinsic) / years / price,
  with no dividend charge (a put holder doesn't owe them the way a short seller does). A mid below
  intrinsic is a stale quote and is skipped. Breakeven = strike - cost.
- OTM puts: 85-150 days out, nearest strikes at or below 10% (or 20%) under the live price.
- whole market: stocks over $10B below the 200-day, 50-day under the 200-day, 1-year return behind SPY.
Breakeven moves are scored by the move needed in the trade's direction (down for puts).

Earnings within EARNINGS_WARN_DAYS days are flagged, not scored. It's a scanner, not advice.

Two lists: your watchlist (leaps_watch.json, gitignored; until it exists, the tracker's Earnings
watchlist), and the whole market: the Screener's ~1,000-stock scan filtered to uptrends beating SPY,
the strongest MARKET_TOP of which get the full LEAPS check. Scans run in background threads (a request
never waits on Yahoo); the last ones are kept in leaps_cache.json and leaps_market_cache.json (gitignored).
"""
import json
import math
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import earnings
from options_data import (ET, RISK_FREE_RATE, QuoteError, _price_ok, _yf, am_put, am_put_iv, chain_rows, expirations, greeks,
                          option_spot, spot_price, years_to_expiry, yahoo_symbol)

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "leaps_cache.json")
WATCH_FILE = os.path.join(HERE, "leaps_watch.json")

MIN_DAYS = 365
DELTA_LO, DELTA_HI = 0.70, 0.85
MIN_OI = 100
MAX_SPREAD = 0.05
EARNINGS_WARN_DAYS = 14
FRESH_SECONDS = 4 * 3600      # a scan is reused for 4 hours unless Refresh is pressed
RETRY_SECONDS = 300           # after a failed scan, wait 5 minutes before starting another on its own
WORKERS = 3                   # more gets Yahoo rate limiting
MAX_TICKERS = 60
ALTERNATES = 4                # other candidate contracts returned per ticker
STALE_FLOOR = 0.8             # carry (time value + dividends) below 80% of the interest on the strike = stale quote
SHORT_MIN_DAYS, SHORT_MAX_DAYS = 85, 150   # "90-day" contracts: Hop/Skip boundary, a few monthly expirations
SIX_MIN_DAYS, SIX_MAX_DAYS = 160, 240      # "6-month" contracts (owner request 2026-11-07), same OTM rules
OTM_BAND = 0.05               # strike from the target up to 5% over it (e.g. 10%-15.5% above price for the 10% term)
OTM_STRIKES = 2               # nearest strikes at or above the target, per expiration
SHORT_MAX_SPREAD = 0.10       # OTM premiums are small, so allow a wider spread before "thin"
TERMS = {
    "leap":   {"label": "LEAPS (365+ days, deep ITM)", "right": "call"},
    "otm10":  {"label": "90-day calls, 10% OTM", "otm": 0.10, "right": "call"},
    "otm20":  {"label": "90-day calls, 20% OTM", "otm": 0.20, "right": "call"},
    "s10":    {"label": "6-month calls, 10% OTM", "otm": 0.10, "right": "call", "days": (SIX_MIN_DAYS, SIX_MAX_DAYS)},
    "s20":    {"label": "6-month calls, 20% OTM", "otm": 0.20, "right": "call", "days": (SIX_MIN_DAYS, SIX_MAX_DAYS)},
    "pleap":  {"label": "LEAPS puts (365+ days, deep ITM)", "right": "put"},
    "potm10": {"label": "90-day puts, 10% OTM", "otm": 0.10, "right": "put"},
    "potm20": {"label": "90-day puts, 20% OTM", "otm": 0.20, "right": "put"},
    "ps10":   {"label": "6-month puts, 10% OTM", "otm": 0.10, "right": "put", "days": (SIX_MIN_DAYS, SIX_MAX_DAYS)},
    "ps20":   {"label": "6-month puts, 20% OTM", "otm": 0.20, "right": "put", "days": (SIX_MIN_DAYS, SIX_MAX_DAYS)},
}
LONG_TERMS = ("leap", "pleap")   # deep in-the-money, 365+ days; the rest are 90-day OTM


def _is_long(term):
    return term in LONG_TERMS


def _right(term):
    return TERMS[term]["right"]


def _days(term):
    """Expiration window (min, max days) for an OTM term: 90-day by default, 6-month for s10/s20."""
    return TERMS[term].get("days", (SHORT_MIN_DAYS, SHORT_MAX_DAYS))
THIN_PENALTY = 15             # points off when no candidate is liquid (hard to get filled, hard to exit)

_lock = threading.Lock()


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
    with open(WATCH_FILE + ".tmp", "w") as f:
        json.dump(clean, f, indent=2)
    os.replace(WATCH_FILE + ".tmp", WATCH_FILE)
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
    div = info.get("dividendRate") or info.get("trailingAnnualDividendRate") or 0.0
    return {"name": info.get("shortName") or info.get("longName"), "sector": info.get("sector"), "div_rate": float(div),
            "mktcap": info.get("marketCap"), "rev_growth": growth, "fcf_margin": margin, "rule40": r40,
            "earnings": e.get("date"), "earnings_session": e.get("session"),
            "earnings_est": bool(e.get("estimated"))}


# ---------- contracts ----------

def _candidates(sym, spot_hint, div_rate=0.0, term="leap"):
    right = _right(term)
    if not _is_long(term):
        return _otm_candidates(sym, spot_hint, TERMS[term]["otm"], right, _days(term))
    put = right == "put"
    today = datetime.now(ET).date()
    try:
        price = spot_price(sym)
    except QuoteError:
        price = spot_hint
    skipped = 0
    exps = [e for e in expirations(sym) if (datetime.strptime(e, "%Y-%m-%d").date() - today).days >= MIN_DAYS]
    out = []
    for exp in exps:
        rows = chain_rows(sym, exp)[right]
        spot = option_spot(sym, exp) or price   # the price the quotes were made at: for delta, like the Chain tab
        t = years_to_expiry(exp)
        dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        for r in rows:
            real = _price_ok(r)
            cost = (r["bid"] + r["ask"]) / 2 if real else r["mark"]
            if put:
                # Listed puts can be exercised early: Black-Scholes delta badly understates deep in-the-money
                # puts (it picked a $600 TSLA put with the stock at $358). Solve IV and delta on an American tree.
                if r["strike"] < spot or not cost or cost <= 0:
                    continue          # out-of-the-money puts never reach delta DELTA_LO
                intrinsic = max(0.0, r["strike"] - price)
                if cost < intrinsic:
                    skipped += 1  # mid under intrinsic: a stale or placeholder quote
                    continue
                iv_am = am_put_iv(spot, r["strike"], t, cost) if real else (r["iv"] or None)
                if not iv_am:
                    continue          # no time value left to solve from: delta is about -1, too deep
                val, dl = am_put(spot, r["strike"], t, iv_am, with_delta=True)
                if not DELTA_LO <= -dl <= DELTA_HI:
                    continue
                theta = am_put(spot, r["strike"], max(t - 1 / 365, 1 / 365), iv_am) - val
                g = {"delta": dl, "theta": theta}
                r = dict(r, iv=iv_am)
                # A put's price carries the dividends the stock will pay (they lower the price); a short seller
                # would pay them, so they aren't a cost of the put: take them out of its time value.
                divs = div_rate * t
                carry = cost - intrinsic - divs
            else:
                g = greeks(right, spot, r["strike"], t, r["iv"])
                if not g or not DELTA_LO <= abs(g["delta"]) <= DELTA_HI:
                    continue
                if not cost or cost <= 0:
                    continue
                intrinsic = max(0.0, price - r["strike"])
                divs = div_rate * t
                carry = cost - intrinsic + divs
                if carry < STALE_FLOOR * r["strike"] * (1 - math.exp(-RISK_FREE_RATE * t)):
                    skipped += 1  # priced under what the call can be worth: stale or placeholder quote
                    continue
            extrinsic = max(0.0, cost - intrinsic)
            bkeven = r["strike"] - cost if put else r["strike"] + cost
            be = bkeven / price - 1
            need = -be if put else be            # the move needed in the trade's direction
            spread = (r["ask"] - r["bid"]) / cost if real else None
            out.append({
                "exp": exp, "dte": dte, "strike": r["strike"], "delta": g["delta"], "theta": g["theta"],
                "iv": r["iv"], "iv_est": r.get("iv_est", False), "est": not real,
                "bid": r["bid"], "ask": r["ask"], "cost": cost, "contract_cost": cost * 100,
                "intrinsic": intrinsic, "extrinsic": extrinsic, "dividends": divs,
                "tv_year": max(0.0, carry) / t / price, "tv_pct": extrinsic / cost,
                "breakeven": bkeven, "be_pct": be, "be_need": need, "be_year": need / t, "right": right,
                "spread": spread, "oi": r["oi"], "volume": r["volume"],
                "leverage": abs(g["delta"]) * price / cost,
                "liquid": real and r["oi"] >= MIN_OI and spread is not None and spread <= MAX_SPREAD,
            })
    return out, skipped


def _otm_candidates(sym, spot_hint, otm, right="call", days=(SHORT_MIN_DAYS, SHORT_MAX_DAYS)):
    """Calls (puts) SHORT_MIN_DAYS-SHORT_MAX_DAYS out, strike within OTM_BAND of price x (1 + otm) (puts: x (1 - otm))."""
    put = right == "put"
    today = datetime.now(ET).date()
    try:
        price = spot_price(sym)
    except QuoteError:
        price = spot_hint
    target = price * (1 - otm) if put else price * (1 + otm)
    exps = [e for e in expirations(sym)
            if days[0] <= (datetime.strptime(e, "%Y-%m-%d").date() - today).days <= days[1]]
    out = []
    for exp in exps:
        # the OTM_STRIKES nearest strikes at or above price x (1 + otm), never below it
        # (the first one always counts, even when strikes are spaced wider than OTM_BAND)
        # puts: the nearest strikes at or below price x (1 - otm), never above it
        if put:
            near = sorted((r for r in chain_rows(sym, exp)["put"] if r["strike"] <= target), key=lambda r: -r["strike"])
            rows = [r for i, r in enumerate(near[:OTM_STRIKES]) if i == 0 or r["strike"] >= target * (1 - OTM_BAND)]
        else:
            above = sorted((r for r in chain_rows(sym, exp)["call"] if r["strike"] >= target), key=lambda r: r["strike"])
            rows = [r for i, r in enumerate(above[:OTM_STRIKES]) if i == 0 or r["strike"] <= target * (1 + OTM_BAND)]
        spot = option_spot(sym, exp) or price
        t = years_to_expiry(exp)
        dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        for r in rows:
            g = greeks(right, spot, r["strike"], t, r["iv"])
            if not g:
                continue
            real = _price_ok(r)
            cost = (r["bid"] + r["ask"]) / 2 if real else r["mark"]
            if not cost or cost <= 0:
                continue
            bkeven = r["strike"] - cost if put else r["strike"] + cost
            be = bkeven / price - 1
            need = -be if put else be
            spread = (r["ask"] - r["bid"]) / cost if real else None
            out.append({
                "exp": exp, "dte": dte, "strike": r["strike"], "delta": g["delta"], "theta": g["theta"],
                "iv": r["iv"], "iv_est": r.get("iv_est", False), "est": not real,
                "bid": r["bid"], "ask": r["ask"], "cost": cost, "contract_cost": cost * 100,
                "intrinsic": 0.0, "extrinsic": cost, "dividends": 0.0,
                "otm_pct": r["strike"] / price - 1,
                "tv_year": cost / t / price, "tv_pct": 1.0,
                "breakeven": bkeven, "be_pct": be, "be_need": need, "be_year": need / t, "t": t, "right": right,
                "spread": spread, "oi": r["oi"], "volume": r["volume"],
                "leverage": abs(g["delta"]) * price / cost,
                "liquid": real and r["oi"] >= MIN_OI and spread is not None and spread <= SHORT_MAX_SPREAD,
            })
    return out, 0


def _otm_score(c, rv):
    iv_hv = c["iv"] / rv if rv else None
    sig = rv * math.sqrt(c["t"]) if rv else None          # the stock's 1-sigma move to expiry
    c["be_sigma"] = c["be_need"] / sig if sig else None
    parts = {
        "be_sigma": 25 * (_lin(c["be_sigma"], 0.75, 2.5) if c["be_sigma"] is not None else 0.0),
        "iv_hv": 15 * (_lin(iv_hv, 1.0, 1.6) if iv_hv is not None else 0.5),
        "spread": 10 * (_lin(c["spread"], 0.03, 0.20) if c["spread"] is not None else 0.0),
        "oi": 10 * (_lin(math.log10(max(c["oi"], 1)), 3, 1)),
    }
    return parts, iv_hv


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


def _stock_score(s, put=False):
    if put:   # the mirror image: a downtrend, lagging SPY, weak fundamentals
        trend = (7 if s["vs200"] is not None and s["vs200"] < 0 else 0) \
            + (5 if s["sma50"] and s["sma200"] and s["sma50"] < s["sma200"] else 0) \
            + (3 if s["vs50"] is not None and s["vs50"] < 0 else 0)
        rs = 7.5 * _lin(s["rs6"], -0.15, 0.10) + 7.5 * _lin(s["rs12"], -0.25, 0.15)
        r40 = 10 * _lin(s["rule40"], 0, 40) if s["rule40"] is not None else 5.0
        return {"trend": trend, "rel_strength": rs, "rule40": r40}
    trend = (7 if s["vs200"] is not None and s["vs200"] > 0 else 0) \
        + (5 if s["sma50"] and s["sma200"] and s["sma50"] > s["sma200"] else 0) \
        + (3 if s["vs50"] is not None and s["vs50"] > 0 else 0)
    rs = 7.5 * _lin(s["rs6"], 0.15, -0.10) + 7.5 * _lin(s["rs12"], 0.25, -0.15)
    r40 = 10 * _lin(s["rule40"], 40, 0) if s["rule40"] is not None else 5.0   # unknown: half credit
    return {"trend": trend, "rel_strength": rs, "rule40": r40}


# ---------- one ticker ----------

def _analyze(sym, spy, term="leap"):
    sym = yahoo_symbol(sym)
    closes = _closes(sym)
    try:
        price = spot_price(sym)     # live, so Price / vs 200D / OTM % all use the same number on a big move day
    except QuoteError:
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
    stock = _stock_score(row, _right(term) == "put")
    cands, skipped = _candidates(sym, price, row.get("div_rate") or 0.0, term)
    row["skipped_stale"] = skipped
    row["div_yield"] = (row.get("div_rate") or 0.0) / price if price else None
    for c in cands:
        c["parts"], c["iv_hv"] = (_contract_score if _is_long(term) else _otm_score)(c, row["rv"])
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
        kind = _right(term) + "s"
        row["note"] = (f"No {kind} {MIN_DAYS}+ days out with delta {DELTA_LO:.2f}-{DELTA_HI:.2f}" if _is_long(term) else
                       f"No {kind} {_days(term)[0]}-{_days(term)[1]} days out near {TERMS[term]['otm']:.0%} OTM")
    elif not liquid:
        row["note"] = f"None liquid (OI >= {MIN_OI}, spread <= {(MAX_SPREAD if _is_long(term) else SHORT_MAX_SPREAD):.0%}); best shown anyway"
    return row


# ---------- scan ----------
# Two lists, each scanned in its own background thread and cached on disk:
#   watch:  the tickers in leaps_watch.json (or the Earnings watchlist)
#   market: the strongest uptrending stocks from the Screener's ~1,000-stock scan (see _market_universe)

MARKET_TOP = 50               # how many market stocks get the full LEAPS check
MARKET_MIN_CAP = 10e9
SCREENER_WAIT = 600           # seconds to wait for the Screener's own scan when it has none

def _new_state(mode, term):
    if term == "leap":   # the original cache files, so existing scans carry over
        f = CACHE_FILE if mode == "watch" else os.path.join(HERE, "leaps_market_cache.json")
    else:
        f = os.path.join(HERE, f"leaps_{mode}_{term}_cache.json")
    return {"file": f, "data": None, "loaded": False,
            "job": {"running": False, "done": 0, "total": 0, "phase": "", "started": 0, "finished": 0, "error": None}}


_state = {(m, t): _new_state(m, t) for m in ("watch", "market") for t in TERMS}


def _save(st):
    tmp = st["file"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st["data"], f)
    os.replace(tmp, st["file"])


def _load(st):
    st["loaded"] = True
    try:
        with open(st["file"]) as f:
            st["data"] = json.load(f)
    except (OSError, ValueError):
        st["data"] = None


def _market_universe(job, put=False):
    """The strongest uptrending stocks from the Screener's last scan: close above the 200-day SMA,
    50-day above the 200-day, 1-year return ahead of SPY's. Ranked by the same trend + relative
    strength points the LEAPS score uses (Rule of 40 needs a slow per-stock lookup, so it's
    scored later, for the top MARKET_TOP only)."""
    import screener
    if screener._data is None:
        screener._load()
    if screener._data is None or not screener._fresh(screener._data["scanned_at"]):
        job["phase"] = "Waiting for the Screener's market scan"
        screener._start_scan(min(MARKET_MIN_CAP, screener.DEFAULT_MIN_CAP))
        start = time.time()
        while time.time() - start < SCREENER_WAIT:
            time.sleep(3)
            if not screener._job["running"] and screener._data is not None and screener._fresh(screener._data["scanned_at"]):
                break
            if not screener._job["running"] and screener._job.get("error"):
                break
    data = screener._data
    if data is None:
        raise QuoteError("The Screener has no market scan yet (Yahoo rate limiting?). Try again in a few minutes.")
    rows = data["rows"]
    spy = rows.get("SPY") or {}
    spy12 = spy.get("chg1y")
    sp = spy.get("spark") or []
    spy6 = sp[-1] / sp[-27] - 1 if len(sp) >= 27 and sp[-27] else None
    picks = []
    for r in rows.values():
        if r.get("type") != "stock" or (r.get("mktcap") or 0) < MARKET_MIN_CAP:
            continue
        if not (r.get("vs200") is not None and r.get("sma50") and r.get("sma200") and r.get("price")):
            continue
        if put:   # downtrend lagging SPY
            if not (r["vs200"] < 0 and r["sma50"] < r["sma200"]):
                continue
            if spy12 is None or r.get("chg1y") is None or r["chg1y"] >= spy12:
                continue
        else:
            if not (r["vs200"] > 0 and r["sma50"] > r["sma200"]):
                continue
            if spy12 is None or r.get("chg1y") is None or r["chg1y"] <= spy12:
                continue
        w = r.get("spark") or []
        r6 = w[-1] / w[-27] - 1 if len(w) >= 27 and w[-27] else None
        pre = _stock_score({"vs200": r["vs200"], "sma50": r["sma50"], "sma200": r["sma200"],
                            "vs50": r["price"] / r["sma50"] - 1, "rule40": 0,
                            "rs6": r6 - spy6 if r6 is not None and spy6 is not None else None,
                            "rs12": r["chg1y"] - spy12}, put)
        picks.append((pre["trend"] + pre["rel_strength"], r.get("mktcap") or 0, r["ticker"]))
    picks.sort(reverse=True)
    stats = {"screened": sum(1 for r in rows.values() if r.get("type") == "stock"), "uptrend": len(picks),
             "screener_at": data["scanned_at"]}
    return [t for _, _, t in picks[:MARKET_TOP]], stats


def _scan(mode, term, tickers):
    st = _state[(mode, term)]
    job = st["job"]
    try:
        stats = {}
        if mode == "market":
            tickers, stats = _market_universe(job, _right(term) == "put")
        job.update(phase="SPY baseline", done=0, total=len(tickers))
        spy = _closes("SPY")
        rows, failed = {}, {}

        def one(sym):
            try:
                r = _analyze(sym, spy, term)
                with _lock:
                    rows[sym] = r
            except QuoteError as e:
                failed[sym] = str(e)
            except Exception as e:   # one bad ticker shouldn't sink the scan
                failed[sym] = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            with _lock:
                job["done"] += 1

        st["partial"] = rows   # scan() shows these while the first scan of a list is still running
        job["phase"] = "Checking " + TERMS[term]["label"]
        with ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(one, tickers))
        with _lock:
            st["data"] = dict(stats, scanned_at=time.time(), tickers=tickers, rows=list(rows.values()), failed=failed)
            _save(st)
            st["partial"] = None
            job.update(running=False, finished=time.time(), error=None, phase="")
    except Exception as e:
        traceback.print_exc()
        with _lock:
            job.update(running=False, finished=time.time(), error=str(e), phase="")


def _start(mode, term, tickers):
    job = _state[(mode, term)]["job"]
    with _lock:
        if job["running"]:
            return
        job.update(running=True, started=time.time(), error=None, done=0, total=len(tickers))
    threading.Thread(target=_scan, args=(mode, term, tickers), daemon=True).start()


def scan(refresh=False, mode="watch", term="leap"):
    """Latest results for one list ("watch" or "market") and term (see TERMS); starts a
    background scan when there's none, it's stale, the watchlist changed, or refresh is set.
    Poll while job.running is true."""
    term = term or "leap"
    if (mode, term) not in _state:
        raise QuoteError("mode must be watch or market; term must be " + ", ".join(TERMS))
    st = _state[(mode, term)]
    if not st["loaded"]:
        with _lock:
            _load(st)
    watch = [yahoo_symbol(t) for t in watchlist()]
    data = st["data"]
    stale = data is None or time.time() - data["scanned_at"] > FRESH_SECONDS
    if mode == "watch" and data is not None and sorted(data.get("tickers", [])) != sorted(watch):
        stale = True
    job = st["job"]
    failed_lately = job["error"] and time.time() - job["finished"] < RETRY_SECONDS   # e.g. Yahoo rate limiting
    if (refresh or (stale and not failed_lately)) and not job["running"]:
        _start(mode, term, watch if mode == "watch" else [])
    with _lock:
        job = dict(st["job"])
    base = {"mode": mode, "term": term, "watchlist": watch, "job": job, "stale": stale,
            "terms": {k: v["label"] for k, v in TERMS.items()},
            "settings": {"term": term, "right": _right(term), "long": _is_long(term), "otm": TERMS[term].get("otm"), "short_min_days": _days(term)[0],
                         "short_max_days": _days(term)[1], "otm_band": OTM_BAND, "min_days": MIN_DAYS, "delta_lo": DELTA_LO, "delta_hi": DELTA_HI, "min_oi": MIN_OI,
                         "max_spread": MAX_SPREAD if _is_long(term) else SHORT_MAX_SPREAD, "earnings_warn_days": EARNINGS_WARN_DAYS,
                         "market_top": MARKET_TOP, "market_min_cap": MARKET_MIN_CAP}}
    if data is None:
        with _lock:
            part = sorted((st.get("partial") or {}).values(), key=lambda r: -r["score"])
        for r in part:
            r["watched"] = r["ticker"] in watch
        return dict(base, status="loading", rows=part, failed={})
    rows = sorted(data["rows"], key=lambda r: -r["score"])
    for r in rows:
        r["watched"] = r["ticker"] in watch
    extra = {k: data[k] for k in ("screened", "uptrend", "screener_at") if k in data}
    return dict(base, **extra, status="ok", rows=rows, failed=data.get("failed", {}), scanned_at=data["scanned_at"])


# ---------- one ticker, on demand ----------
# The Find box looks up any ticker that isn't in the current list (e.g. a stock outside the market top 50).
_one_cache = {}
_spy_cache = [0.0, None]


def lookup(ticker, term="leap"):
    """Full LEAPS Finder check for a single ticker and term, cached for FRESH_SECONDS."""
    term = term or "leap"
    if term not in TERMS:
        raise QuoteError("term must be " + ", ".join(TERMS))
    sym = yahoo_symbol(str(ticker or "").strip().upper())
    if not re.fullmatch(r"[A-Z0-9.^=-]{1,12}", sym):
        raise QuoteError("Enter a ticker")
    hit = _one_cache.get((sym, term))
    if hit and time.time() - hit[0] < FRESH_SECONDS:
        row = hit[1]
    else:
        if _spy_cache[1] is None or time.time() - _spy_cache[0] > FRESH_SECONDS:
            _spy_cache[:] = [time.time(), _closes("SPY")]
        row = _analyze(sym, _spy_cache[1], term)
        row["lookup"] = True
        _one_cache[(sym, term)] = (time.time(), row)
    row["watched"] = sym in [yahoo_symbol(t) for t in watchlist()]
    return {"term": term, "row": row}
