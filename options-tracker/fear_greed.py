"""CNN Fear & Greed Index for the dial meter under the red-folder calendar.

Source: the JSON behind https://www.cnn.com/markets/fear-and-greed (no API key). The index runs 0 to 100
and is built from seven market indicators; CNN's bands are 0-25 Extreme Fear, 25-45 Fear, 45-55 Neutral,
55-75 Greed and 75-100 Extreme Greed. It is fetched at most every 10 minutes and the last good copy is
kept in fng_cache.json (gitignored), which also covers restarts and outages.
"""
import json
import os
import time
import urllib.request

from options_data import QuoteError

URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "fng_cache.json")
REFRESH_SECONDS = 10 * 60
RETRY_SECONDS = 2 * 60          # after a failed fetch, wait this long before trying again
# CNN answers a bare script with HTTP 418, so send the headers a browser on cnn.com would.
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
           "Accept": "application/json", "Referer": "https://www.cnn.com/", "Origin": "https://www.cnn.com"}

# The seven indicators, in CNN's order, with a plain name for each.
PARTS = [
    ("market_momentum_sp500", "Market momentum", "S&P 500 vs its 125-day average"),
    ("stock_price_strength", "Stock price strength", "NYSE 52-week highs vs lows"),
    ("stock_price_breadth", "Stock price breadth", "McClellan volume summation"),
    ("put_call_options", "Put and call options", "5-day put/call ratio"),
    ("market_volatility_vix", "Market volatility", "VIX vs its 50-day average"),
    ("safe_haven_demand", "Safe-haven demand", "Stocks vs bonds, last 20 days"),
    ("junk_bond_demand", "Junk bond demand", "Junk vs investment-grade yield spread"),
]

_mem = {"at": 0, "data": None, "fetched": None, "error": None, "tried": 0}


def _num(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _download():
    req = urllib.request.Request(URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = json.load(r)
    fg = raw["fear_and_greed"]
    score = _num(fg.get("score"))
    if score is None:
        raise ValueError("no score in CNN's reply")
    parts = []
    for key, name, what in PARTS:
        p = raw.get(key) or {}
        if _num(p.get("score")) is not None:
            parts.append({"name": name, "what": what, "score": _num(p["score"]), "rating": p.get("rating") or ""})
    return {"score": score, "rating": fg.get("rating") or "", "as_of": fg.get("timestamp") or "",
            "previous_close": _num(fg.get("previous_close")), "week": _num(fg.get("previous_1_week")),
            "month": _num(fg.get("previous_1_month")), "year": _num(fg.get("previous_1_year")), "parts": parts}


def _load_disk():
    try:
        with open(CACHE_FILE) as f:
            d = json.load(f)
        return d["data"], d["fetched"]
    except (OSError, ValueError, KeyError):
        return None, None


def fear_greed():
    """Today's index, its earlier readings and the seven parts, falling back to the last good copy."""
    now = time.time()
    if _mem["data"] is None:
        _mem["data"], _mem["fetched"] = _load_disk()
        if _mem["fetched"]:
            _mem["at"] = _mem["fetched"]
    stale = now - _mem["at"] > REFRESH_SECONDS
    if (_mem["data"] is None or stale) and now - _mem["tried"] > RETRY_SECONDS:
        _mem["tried"] = now
        try:
            data = _download()
            _mem.update(data=data, fetched=now, at=now, error=None)
            try:
                with open(CACHE_FILE, "w") as f:
                    json.dump({"fetched": now, "data": data}, f)
            except OSError:
                pass
        except Exception as e:   # network, HTTP 418/429, changed format: keep serving the last good copy
            _mem["error"] = f"{type(e).__name__}: {e}"
    if _mem["data"] is None:
        raise QuoteError(f"Couldn't reach CNN Fear & Greed ({_mem['error'] or 'try again shortly'})")
    return dict(_mem["data"], fetched=_mem["fetched"], error=_mem["error"],
                source="CNN", link="https://www.cnn.com/markets/fear-and-greed")
