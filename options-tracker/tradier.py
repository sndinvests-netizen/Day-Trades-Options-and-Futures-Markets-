"""Tradier market data (real-time quotes and option chains with greeks).

The API token lives in tradier_key.txt (git-ignored). Tradier allows 120 market-data requests a
minute and reports what's left in each response's X-Ratelimit headers; requests here are paced
from those headers, so we wait only as long as Tradier says instead of guessing.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from options_data import QuoteError

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "tradier_key.txt")
BASE = "https://api.tradier.com/v1"
MIN_GAP = 0.5            # background scans: 120 a minute at most
SPARE = {False: 25, True: 1}   # requests a scan leaves free each minute so lookups you type never wait behind it

_gate = threading.Lock()
_state = {"last": 0.0, "available": None, "expiry": 0.0}
_ctx = threading.local()


class priority:
    """with tradier.priority(): ... -- a lookup the user is waiting on goes ahead of background scans."""
    def __enter__(self):
        self.prev = getattr(_ctx, "hi", False)
        _ctx.hi = True

    def __exit__(self, *a):
        _ctx.hi = self.prev


def _slot():
    """Wait for a request slot without holding the lock while waiting, so lookups can pass a waiting scan."""
    hi = getattr(_ctx, "hi", False)
    gap = 0.0 if hi else MIN_GAP
    deadline = time.time() + 70
    while True:
        with _gate:
            now = time.time()
            if _state["available"] is not None and now >= _state["expiry"]:
                _state["available"] = None          # Tradier's minute rolled over
            short = _state["available"] is not None and _state["available"] <= SPARE[hi]
            if (not short and now >= _state["last"] + gap) or now > deadline:
                _state["last"] = now
                if _state["available"] is not None:
                    _state["available"] -= 1
                return
        time.sleep(0.05 if hi else 0.25)


class RateLimited(QuoteError):
    pass


def _key():
    try:
        with open(KEY_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def enabled():
    return bool(_key())


def _get(path, **params):
    key = _key()
    if not key:
        raise QuoteError("No Tradier key (tradier_key.txt)")
    _slot()
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as f:
            h = f.headers
            try:
                with _gate:
                    _state["available"] = int(h.get("X-Ratelimit-Available"))
                    _state["expiry"] = int(h.get("X-Ratelimit-Expiry")) / 1000
            except (TypeError, ValueError):
                pass
            return json.load(f)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            with _gate:
                _state["available"], _state["expiry"] = 0, time.time() + 60
            raise RateLimited("Tradier HTTP 429")
        raise QuoteError(f"Tradier HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise QuoteError(f"Tradier: {e}")


def _list(x):
    return x if isinstance(x, list) else ([x] if x else [])


def spot(sym):
    q = _get("/markets/quotes", symbols=sym).get("quotes") or {}
    q = _list(q.get("quote"))
    price = q and (q[0].get("last") or q[0].get("close") or q[0].get("prevclose"))
    if not price:
        raise QuoteError(f"{sym}: no price from Tradier")
    return float(price)


def expirations(sym):
    e = (_get("/markets/options/expirations", symbol=sym).get("expirations") or {})
    return _list(e.get("date"))


def chain(sym, expiration):
    """Every contract for one expiration, greeks included (Tradier's greeks refresh about hourly)."""
    o = (_get("/markets/options/chains", symbol=sym, expiration=expiration, greeks="true").get("options") or {})
    return _list(o.get("option"))
