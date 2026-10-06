"""USD economic calendar from ForexFactory: the "red folder" (high-impact) events, plus orange (medium)
and bank holidays, for the left-side panel in the tracker.

Source: ForexFactory's public weekly export (https://nfs.faireconomy.media/ff_calendar_thisweek.json),
the same events as forexfactory.com/calendar. It covers the current week only (Sunday to Saturday,
ForexFactory's week) and has forecast and previous values but no actuals. ForexFactory asks that the
export not be polled hard, so it is fetched at most every 15 minutes and the last good copy is kept in
econ_cache.json (gitignored), which also covers restarts and outages.
"""
import json
import os
import re
import time
import urllib.request
from datetime import datetime

URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "econ_cache.json")
REFRESH_SECONDS = 15 * 60
RETRY_SECONDS = 2 * 60          # after a failed fetch, wait this long before trying again
IMPACTS = {"High": "red", "Medium": "orange", "Holiday": "holiday"}

# Tag the events traders watch most. First match wins, so the specific ones come first.
TAGS = [
    ("CPI", r"\bCPI\b|Consumer Price"),
    ("PPI", r"\bPPI\b|Producer Price"),
    ("PCE", r"\bPCE\b|Personal Consumption"),
    ("JOLTS", r"JOLTS|Job Openings"),
    ("Jobs", r"Non-Farm|NFP|Unemployment Rate|Average Hourly Earnings|ADP|Unemployment Claims|Employment Change"),
    ("Fed", r"FOMC|Federal Funds|Fed Chair|Fed\b|Powell|Beige Book"),
    ("President", r"President|Trump"),
    ("GDP", r"\bGDP\b"),
    ("Retail", r"Retail Sales"),
    ("ISM", r"\bISM\b|PMI"),
    ("Sentiment", r"Consumer Sentiment|Consumer Confidence|Inflation Expectations"),
    ("Treasury", r"Bond Auction|Treasury"),
]

_mem = {"at": 0, "events": None, "fetched": None, "error": None, "tried": 0}


def tag(title):
    for name, pat in TAGS:
        if re.search(pat, title, re.I):
            return name
    return ""


def _download():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (SIS Option Tracker)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    if not isinstance(data, list):
        raise ValueError("unexpected calendar format")
    return data


def _load_disk():
    try:
        with open(CACHE_FILE) as f:
            d = json.load(f)
        return d["events"], d["fetched"]
    except (OSError, ValueError, KeyError):
        return None, None


def _raw():
    """The week's raw events, refreshed at most every 15 minutes, falling back to the last good copy."""
    now = time.time()
    if _mem["events"] is None:
        _mem["events"], _mem["fetched"] = _load_disk()
        if _mem["fetched"]:
            _mem["at"] = _mem["fetched"]
    stale = now - _mem["at"] > REFRESH_SECONDS
    if (_mem["events"] is None or stale) and now - _mem["tried"] > RETRY_SECONDS:
        _mem["tried"] = now
        try:
            events = _download()
            _mem.update(events=events, fetched=now, at=now, error=None)
            try:
                with open(CACHE_FILE, "w") as f:
                    json.dump({"fetched": now, "events": events}, f)
            except OSError:
                pass
        except Exception as e:   # network, HTTP 429, bad JSON: keep serving the last good copy
            _mem["error"] = f"{type(e).__name__}: {e}"
    return _mem["events"] or []


def usd_events():
    """USD high/medium-impact events and holidays this week, in time order."""
    out = []
    for e in _raw():
        level = IMPACTS.get(e.get("impact"))
        if e.get("country") != "USD" or not level:
            continue
        try:
            when = datetime.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        title = (e.get("title") or "").strip()
        out.append({"title": title, "impact": level, "tag": tag(title),
                    "time": when.isoformat(), "ts": int(when.timestamp()),
                    "all_day": when.hour == 0 and when.minute == 0 and level == "holiday",
                    "forecast": e.get("forecast") or "", "previous": e.get("previous") or ""})
    out.sort(key=lambda x: x["ts"])
    return {"events": out, "fetched": _mem["fetched"], "error": _mem["error"],
            "source": "ForexFactory", "link": "https://www.forexfactory.com/calendar"}
