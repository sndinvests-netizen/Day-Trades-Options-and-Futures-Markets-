"""Yahoo Finance option data and Black-Scholes Greeks, shared by tracker.py and app.py.

Data comes from the free yfinance library and may be delayed about 15 minutes.
To switch to a broker feed later (Schwab, Tastytrade, Tradier), replace
expirations(), spot_price() and chain_rows(); everything else builds on them.
"""
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

RISK_FREE_RATE = 0.04
ET = ZoneInfo("America/New_York")
CACHE_SECONDS = 30            # don't ask Yahoo for the same thing more than twice a minute
NEWS_CACHE_SECONDS = 300      # headlines are re-fetched at most every 5 minutes per ticker
NEWS_PER_TICKER = 10
UNUSUAL_MIN_VOLUME = 500      # unusual = at least this many contracts traded ...
UNUSUAL_VOL_OI = 2            # ... and at least this multiple of open interest
IV_FLOOR = 0.03               # Yahoo IV below this is a placeholder, not a real number
FALLBACK_IV = 0.5             # last resort when nothing better is available


class QuoteError(Exception):
    pass


def _yf():
    try:
        import yfinance as yf
    except ImportError:
        raise QuoteError("yfinance not installed: run  pip3 install -r requirements.txt")
    return yf


_cache = {}


def _cached(key, fn, ttl=CACHE_SECONDS):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(x) else x


# ---------- math ----------

def years_to_expiry(exp, now=None):
    """Time from now to 4pm ET on the expiration date, in years (at least one hour)."""
    now = now or datetime.now(ET)
    close = datetime.strptime(exp, "%Y-%m-%d").replace(hour=16, tzinfo=ET)
    return max((close - now).total_seconds(), 3600) / (365 * 86400)


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def norm_pdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def greeks(kind, spot, strike, t, iv, r=RISK_FREE_RATE):
    """Black-Scholes Greeks per share of one long contract.

    theta is per calendar day, vega per 1 point of IV. Returns None when Yahoo's IV
    is missing or junk (it reports ~0 for some deep in-the-money and weekend quotes).
    """
    if not spot or not strike or not iv or iv < 0.01 or t <= 0:
        return None
    st = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * t) / st
    d2 = d1 - st
    pdf = norm_pdf(d1)
    disc = strike * math.exp(-r * t)
    decay = -spot * pdf * iv / (2 * math.sqrt(t))
    if kind == "call":
        delta, theta = norm_cdf(d1), decay - r * disc * norm_cdf(d2)
    else:
        delta, theta = norm_cdf(d1) - 1, decay + r * disc * norm_cdf(-d2)
    return {"delta": delta, "gamma": pdf / (spot * st),
            "theta": theta / 365, "vega": spot * pdf * math.sqrt(t) / 100}


def bs_price(kind, spot, strike, t, iv, r=RISK_FREE_RATE):
    """Black-Scholes value per share of one contract."""
    if t <= 0 or not iv or iv <= 0:
        return max(0.0, spot - strike) if kind == "call" else max(0.0, strike - spot)
    st = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * t) / st
    d2 = d1 - st
    disc = strike * math.exp(-r * t)
    if kind == "call":
        return spot * norm_cdf(d1) - disc * norm_cdf(d2)
    return disc * norm_cdf(-d2) - spot * norm_cdf(-d1)


def max_pain(calls, puts):
    """Strike where option holders' total payout at expiration is smallest."""
    strikes = sorted({r["strike"] for r in calls + puts})
    if not strikes:
        return None

    def payout(price):
        return (sum(r["oi"] * max(0.0, price - r["strike"]) for r in calls)
                + sum(r["oi"] * max(0.0, r["strike"] - price) for r in puts))
    return min(strikes, key=payout)


# ---------- Yahoo ----------

def yahoo_symbol(ticker):
    """Yahoo spells class shares with a dash: BRK.B -> BRK-B."""
    return ticker.upper().strip().replace(".", "-")


def expirations(ticker):
    yf = _yf()
    ticker = yahoo_symbol(ticker)

    def fetch():
        try:
            return list(yf.Ticker(ticker).options)
        except Exception as e:
            raise QuoteError(f"{ticker}: {e}")
    exps = _cached(("exps", ticker), fetch)
    if not exps:
        raise QuoteError(f"No options listed for {ticker}")
    return exps


def spot_price(ticker):
    yf = _yf()
    ticker = yahoo_symbol(ticker)

    def fetch():
        try:
            return float(yf.Ticker(ticker).fast_info["last_price"])
        except Exception as e:
            raise QuoteError(f"{ticker}: no stock price ({e})")
    return _cached(("spot", ticker), fetch)


def historical_vol(ticker):
    """Annualized 30-trading-day close-to-close volatility, or None."""
    yf = _yf()
    ticker = yahoo_symbol(ticker)

    def fetch():
        try:
            closes = [c for c in yf.Ticker(ticker).history(period="3mo")["Close"].tolist() if c > 0][-31:]
        except Exception:
            return None
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        if len(rets) < 10:
            return None
        mean = sum(rets) / len(rets)
        return math.sqrt(sum((x - mean) ** 2 for x in rets) / (len(rets) - 1) * 252)
    return _cached(("hv", ticker), fetch)


def _fill_missing(ticker, exp, out):
    """Yahoo sends 0 bid / 0 ask (and placeholder IVs) for contracts with no live quote,
    e.g. before the delayed feed catches up after the open or on thinly traded strikes.
    Rather than pass off the last trade, which can be days old, price those contracts
    with Black-Scholes and flag them as estimates (est=True).

    IV for an unquoted contract comes from the nearest quoted strike in the same
    expiration, then the stock's 30-day historical volatility, then FALLBACK_IV.
    """
    good = sorted((r["strike"], r["iv"]) for rows in out.values() for r in rows if r["quoted"] and r["iv"] >= IV_FLOOR)
    hv = None
    spot = None
    t = years_to_expiry(exp)
    for kind, rows in out.items():
        for r in rows:
            r["est"] = r["iv_est"] = False
            if r["quoted"] and r["iv"] >= IV_FLOOR:
                continue
            if good:
                r["iv"] = min(good, key=lambda g: abs(g[0] - r["strike"]))[1]
            else:
                if hv is None:
                    hv = historical_vol(ticker) or FALLBACK_IV
                r["iv"] = hv
            r["iv_est"] = True
            if not r["quoted"]:
                spot = spot or spot_price(ticker)
                r["mark"] = bs_price(kind, spot, r["strike"], t, r["iv"])
                r["est"] = True


def chain_rows(ticker, exp):
    """{'call': [...], 'put': [...]}: strike, bid, ask, last, last_trade, mark, volume, oi, iv,
    quoted (has a live ask), est (mark is a Black-Scholes estimate), iv_est (IV is borrowed)."""
    yf = _yf()
    ticker = yahoo_symbol(ticker)

    def fetch():
        if exp not in expirations(ticker):
            raise QuoteError(f"{ticker} has no {exp} expiration listed")
        try:
            ch = yf.Ticker(ticker).option_chain(exp)
        except Exception as e:
            raise QuoteError(f"{ticker}: {e}")
        out = {}
        for kind, df in (("call", ch.calls), ("put", ch.puts)):
            rows = []
            for r in df.to_dict("records"):
                bid, ask, last = _num(r.get("bid")), _num(r.get("ask")), _num(r.get("lastPrice"))
                lt = r.get("lastTradeDate")
                rows.append({"strike": _num(r.get("strike")), "bid": bid, "ask": ask, "last": last,
                             "last_trade": lt.isoformat() if hasattr(lt, "isoformat") and lt == lt else None,
                             "quoted": ask > 0, "mark": (bid + ask) / 2 if ask > 0 else last,
                             "volume": int(_num(r.get("volume"))), "oi": int(_num(r.get("openInterest"))),
                             "iv": _num(r.get("impliedVolatility"))})
            out[kind] = rows
        _fill_missing(ticker, exp, out)
        return out
    return _cached(("chain", ticker, exp), fetch)


def quote(ticker, exp, strike, kind="put"):
    """One contract: dict(spot, bid, ask, last, mark, volume, oi, iv)."""
    row = next((r for r in chain_rows(ticker, exp)[kind] if abs(r["strike"] - strike) < 1e-6), None)
    if row is None:
        raise QuoteError(f"{ticker} {exp} {strike:g}{kind[0].upper()} not found in chain")
    return dict(row, spot=spot_price(ticker))


def news(tickers):
    """Recent Yahoo Finance headlines for these tickers, newest first, one entry per story:
    title, publisher, link, time (unix seconds), tickers (which of yours it mentions)."""
    yf = _yf()
    wanted = [yahoo_symbol(t) for t in tickers if t.strip()]
    stories = {}
    for t in dict.fromkeys(wanted):
        def fetch(t=t):
            try:
                return yf.Search(t, news_count=NEWS_PER_TICKER, max_results=1).news or []
            except Exception:
                return []
        for n in _cached(("news", t), fetch, NEWS_CACHE_SECONDS):
            related = n.get("relatedTickers") or []
            if related and t not in related:
                continue
            key = n.get("uuid") or n.get("link")
            s = stories.setdefault(key, {"title": n.get("title", ""), "publisher": n.get("publisher", ""),
                                         "link": n.get("link", ""), "time": n.get("providerPublishTime", 0),
                                         "tickers": []})
            if t not in s["tickers"]:
                s["tickers"].append(t)
    return sorted((s for s in stories.values() if s["title"] and s["link"].startswith("http")),
                  key=lambda s: s["time"], reverse=True)


def chain(ticker, exp=None):
    """Full chain for one expiration with Greeks, flags and summary numbers."""
    ticker = yahoo_symbol(ticker)
    exps = expirations(ticker)
    exp = exp if exp in exps else exps[0]
    spot = spot_price(ticker)
    rows = chain_rows(ticker, exp)
    t = years_to_expiry(exp)
    sides = {}
    for kind in ("call", "put"):
        sides[kind] = [dict(r, **(greeks(kind, spot, r["strike"], t, r["iv"]) or {}),
                            itm=(r["strike"] < spot) if kind == "call" else (r["strike"] > spot),
                            unusual=r["volume"] >= UNUSUAL_MIN_VOLUME and r["volume"] >= UNUSUAL_VOL_OI * r["oi"])
                       for r in rows[kind]]
    calls, puts = sides["call"], sides["put"]
    call_vol, put_vol = sum(r["volume"] for r in calls), sum(r["volume"] for r in puts)
    call_oi, put_oi = sum(r["oi"] for r in calls), sum(r["oi"] for r in puts)
    near = [r for r in calls + puts if r["iv"] >= 0.01]
    atm_iv = None
    if near:
        atm_strike = min((r["strike"] for r in near), key=lambda k: abs(k - spot))
        ivs = [r["iv"] for r in near if r["strike"] == atm_strike]
        atm_iv = sum(ivs) / len(ivs)
    unusual = sorted(({"kind": k, **r} for k in ("call", "put") for r in sides[k] if r["unusual"]),
                     key=lambda r: r["volume"] / max(r["oi"], 1), reverse=True)[:10]
    return {
        "ticker": ticker, "spot": spot, "expirations": exps, "exp": exp,
        "dte": (datetime.strptime(exp, "%Y-%m-%d").date() - datetime.now(ET).date()).days,
        "calls": calls, "puts": puts,
        "summary": {"call_volume": call_vol, "put_volume": put_vol,
                    "call_oi": call_oi, "put_oi": put_oi,
                    "pc_volume": put_vol / call_vol if call_vol else None,
                    "pc_oi": put_oi / call_oi if call_oi else None,
                    "max_pain": max_pain(calls, puts), "atm_iv": atm_iv},
        "unusual": unusual,
        "as_of": datetime.now(ET).isoformat(timespec="seconds"),
    }
