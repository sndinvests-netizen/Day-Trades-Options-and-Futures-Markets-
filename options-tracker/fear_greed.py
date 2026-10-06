"""CNN Fear & Greed Index for the meter under the red-folder calendar.

Source: the JSON behind https://www.cnn.com/markets/fear-and-greed (production.dataviz.cnn.io). It has
no official API; the endpoint answers browsers only, so the request sends a browser User-Agent and a
cnn.com Referer. CNN recalculates during market hours, so the score is cached for 5 minutes and the last
good copy is kept in memory if a fetch fails.
"""
import json
import time
import urllib.request

URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/129.0 Safari/537.36",
    "Referer": "https://www.cnn.com/",
    "Accept": "application/json",
}
REFRESH_SECONDS = 5 * 60
RETRY_SECONDS = 60
# CNN's seven indicators, in the order its page lists them.
INDICATORS = [
    ("market_momentum_sp500", "Market momentum", "S&P 500 vs its 125-day average"),
    ("stock_price_strength", "Stock price strength", "NYSE 52-week highs vs lows"),
    ("stock_price_breadth", "Stock price breadth", "McClellan volume summation"),
    ("put_call_options", "Put/call options", "5-day put/call ratio"),
    ("market_volatility_vix", "Market volatility", "VIX vs its 50-day average"),
    ("safe_haven_demand", "Safe haven demand", "Stocks vs bonds, 20-day returns"),
    ("junk_bond_demand", "Junk bond demand", "Junk vs investment-grade yield spread"),
]

_mem = {"at": 0, "data": None, "error": None, "tried": 0}


def _download():
    req = urllib.request.Request(URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = json.load(r)
    fg = raw["fear_and_greed"]
    hist = raw.get("fear_and_greed_historical", {}).get("data", [])
    return {
        "score": round(float(fg["score"]), 1),
        "rating": fg.get("rating", ""),
        "updated": fg.get("timestamp"),
        "previous": {k: round(float(fg[f"previous_{k}"]), 1)
                     for k in ("close", "1_week", "1_month", "1_year") if fg.get(f"previous_{k}") is not None},
        "indicators": [{"key": k, "name": name, "what": what,
                        "score": round(float(raw[k]["score"]), 1), "rating": raw[k].get("rating", "")}
                       for k, name, what in INDICATORS if isinstance(raw.get(k), dict) and "score" in raw[k]],
        # last 30 points for a sparkline (CNN gives a year of daily closes plus today)
        "history": [round(float(p["y"]), 1) for p in hist[-30:] if "y" in p],
    }


def fear_greed():
    now = time.time()
    stale = now - _mem["at"] > REFRESH_SECONDS
    if (_mem["data"] is None or stale) and now - _mem["tried"] > RETRY_SECONDS:
        _mem["tried"] = now
        try:
            _mem.update(data=_download(), at=now, error=None)
        except Exception as e:   # network, HTTP 418/403, format change: keep the last good copy
            _mem["error"] = f"{type(e).__name__}: {e}"
    if _mem["data"] is None:
        raise RuntimeError(f"Couldn't load CNN Fear & Greed ({_mem['error']})")
    return dict(_mem["data"], fetched=_mem["at"], error=_mem["error"],
                source="CNN", link="https://www.cnn.com/markets/fear-and-greed")
