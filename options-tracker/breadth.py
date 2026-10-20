"""S&P 500 market breadth for the panel under the Fear & Greed dial.

Members come from Wikipedia's "List of S&P 500 companies" table (re-read at most once a week, last good
list kept in breadth_cache.json). Daily bars for all of them come from Yahoo through one batched
`yf.download` (about 15 seconds), so during the session the last bar is today's, about 15 minutes delayed.

From those bars:
  - % of members above their 50-day and 200-day simple moving averages, now, a week ago (5 sessions)
    and a month ago (21 sessions). The 50-day figure is the headline; its bands are 0-20 Oversold,
    20-40 Weak, 40-60 Neutral, 60-80 Healthy, 80-100 Overbought.
  - advancers, decliners and unchanged today (last close vs the one before)
  - new 52-week highs and lows today (today's high/low vs the 252 sessions before it)

Fetched at most every 15 minutes; the last good result is kept in breadth_cache.json (gitignored),
which also covers restarts and outages.
"""
import json
import os
import re
import threading
import time
import urllib.request

from options_data import QuoteError, _yf

MEMBERS_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "breadth_cache.json")
REFRESH_SECONDS = 15 * 60
RETRY_SECONDS = 2 * 60          # after a failed fetch, wait this long before trying again
MEMBERS_SECONDS = 7 * 24 * 3600
WEEK, MONTH, YEAR = 5, 21, 252  # sessions
BANDS = [(20, "Oversold"), (40, "Weak"), (60, "Neutral"), (80, "Healthy"), (101, "Overbought")]

_mem = {"at": 0, "data": None, "fetched": None, "error": None, "tried": 0, "members": None, "members_at": 0}
_lock = threading.Lock()


def rating(pct):
    return next(name for top, name in BANDS if round(pct) < top)


def _members():
    """Yahoo symbols of the S&P 500 members (BRK.B -> BRK-B), from Wikipedia, cached for a week."""
    if _mem["members"] and time.time() - _mem["members_at"] < MEMBERS_SECONDS:
        return _mem["members"]
    req = urllib.request.Request(MEMBERS_URL, headers={"User-Agent": "Mozilla/5.0 options-tracker"})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    start = html.find('id="constituents"')
    if start < 0:
        raise ValueError("no constituents table on Wikipedia")
    table = html[start:html.find("</table>", start)]
    syms = re.findall(r"<tr[^>]*>\s*<td[^>]*><a[^>]*>([A-Z.]+)</a>", table)
    if len(syms) < 450:
        raise ValueError(f"only {len(syms)} members found on Wikipedia")
    _mem["members"], _mem["members_at"] = [s.replace(".", "-") for s in syms], time.time()
    return _mem["members"]


def _pct(mask):
    return round(100.0 * float(mask.sum()) / max(1, int(mask.count())), 1)


def _download():
    syms = _members()
    df = _yf().download(syms, period="15mo", interval="1d", auto_adjust=True, progress=False, threads=True)
    close, high, low = df["Close"], df["High"], df["Low"]
    close = close.loc[:, close.iloc[-1].notna()]        # drop members with no bar for the last session
    if close.shape[1] < 400 or len(close) < 200 + MONTH:
        raise ValueError(f"Yahoo returned usable prices for only {close.shape[1]} members")
    sma50, sma200 = close.rolling(50).mean(), close.rolling(200).mean()

    def above(sma, back):
        row, avg = close.iloc[-1 - back], sma.iloc[-1 - back]
        ok = row.notna() & avg.notna()
        return _pct((row > avg)[ok])

    chg = (close.iloc[-1] - close.iloc[-2]).dropna()
    hi, lo = high[close.columns], low[close.columns]
    new_hi = int((hi.iloc[-1] >= hi.iloc[-1 - YEAR:-1].max()).sum())
    new_lo = int((lo.iloc[-1] <= lo.iloc[-1 - YEAR:-1].min()).sum())
    a50 = above(sma50, 0)
    return {"members": int(close.shape[1]), "as_of": close.index[-1].strftime("%Y-%m-%d"),
            "above50": a50, "rating": rating(a50),
            "above50_week": above(sma50, WEEK), "above50_month": above(sma50, MONTH),
            "above200": above(sma200, 0), "above200_week": above(sma200, WEEK), "above200_month": above(sma200, MONTH),
            "advancers": int((chg > 0).sum()), "decliners": int((chg < 0).sum()), "unchanged": int((chg == 0).sum()),
            "new_highs": new_hi, "new_lows": new_lo}


def _load_disk():
    try:
        with open(CACHE_FILE) as f:
            d = json.load(f)
        if d.get("members"):
            _mem["members"], _mem["members_at"] = d["members"], d.get("members_at", 0)
        return d["data"], d["fetched"]
    except (OSError, ValueError, KeyError):
        return None, None


def breadth():
    """Today's breadth readings, falling back to the last good copy."""
    with _lock:   # one download at a time; other requests wait and then get the fresh copy
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
                        json.dump({"fetched": now, "data": data, "members": _mem["members"],
                                   "members_at": _mem["members_at"]}, f)
                except OSError:
                    pass
            except Exception as e:   # network, Yahoo rate limit, Wikipedia layout change: keep the last good copy
                _mem["error"] = f"{type(e).__name__}: {e}"
        if _mem["data"] is None:
            raise QuoteError(f"Couldn't load market breadth ({_mem['error'] or 'try again shortly'})")
        return dict(_mem["data"], fetched=_mem["fetched"], error=_mem["error"], source="Yahoo Finance, Wikipedia")
