#!/usr/bin/env python3
"""Sound Investment Solutions Option Tracker in the browser: any ticker's option chain plus your logged trades.

  python3 app.py            # opens http://127.0.0.1:8765
  python3 app.py --port 9000 --no-browser

Runs only on this computer (127.0.0.1). Trades are read from and saved to
trades.json, the same file tracker.py uses, which is kept out of git.
"""
import argparse
import json
import os
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import tracker
from econ_calendar import usd_events
from fees import order_fees
from options_data import QuoteError, chain, news, vix

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "web", "index.html")
DB = tracker.DEFAULT_DB
VIX_LEVELS = os.path.join(HERE, "vix_levels.json")   # your VIX levels and what to do at each (kept out of git)
# Starting levels, copied from the lines on the CBOE:VIX chart. "up" alerts when VIX rises through
# the level, "down" when it falls through it.
DEFAULT_VIX_LEVELS = [
    {"price": 13.37, "dir": "down", "label": "Cash Up", "color": "#1848CC",
     "note": "Volatility is low and markets are complacent. Take profits and raise cash."},
    {"price": 20.03, "dir": "up", "label": "Caution", "color": "#E6C200",
     "note": "Fear is building. Tighten stops, trim risk, and get your buy list ready."},
    {"price": 30.01, "dir": "up", "label": "Fear: start buying", "color": "#FF9800",
     "note": "Start scaling into quality names with your cash."},
    {"price": 40.17, "dir": "up", "label": "Buy Everything!", "color": "#F23645",
     "note": "Buy everything."},
    {"price": 59.86, "dir": "up", "label": "Extreme panic", "color": "#9C27B0",
     "note": "Crisis-level fear, historically near major market bottoms. Deploy remaining cash."},
]
STATIC = {"/sis-mark.png": "image/png", "/favicon.png": "image/png"}
db_lock = threading.Lock()


def positions():
    with db_lock:
        trades = tracker.load(DB)
    open_, closed = [], []
    for t in trades:
        row = dict(t, type=tracker.kind(t), label=tracker.label(t), stats=tracker.stats(t))
        if t["status"] == "open":
            row["dte"] = tracker.dte(t["exp"]) if t["exp"] else None
            try:
                row["live"] = tracker.live(t)
            except QuoteError as e:
                row["live_error"] = str(e)
            open_.append(row)
        else:
            closed.append(row)
    report = [{"group": n, "trades": c, "wins": w, "realized": r}
              for n, c, w, r in tracker.report_groups(trades)]
    collateral, long_cost = tracker.open_totals(trades)
    return {"open": open_, "closed": closed, "report": report, "premium": tracker.premium_report(trades),
            "collateral": collateral, "long_cost": long_cost}


def news_feed(extra):
    """Headlines for every ticker you hold open plus any asked for (e.g. the chain you're viewing)."""
    with db_lock:
        trades = tracker.load(DB)
    held = sorted({t["ticker"] for t in trades if t["status"] == "open"})
    asked = [x.strip().upper() for x in extra.split(",") if x.strip()][:5]
    tickers = list(dict.fromkeys(asked + held))
    return {"tickers": tickers, "held": held, "stories": news(tickers)[:60]}


def vix_levels():
    try:
        with open(VIX_LEVELS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return DEFAULT_VIX_LEVELS


def save_vix_levels(levels):
    out = []
    for l in levels or []:
        try:
            price = float(l.get("price"))
        except (TypeError, ValueError):
            raise tracker.TradeError("Each VIX level needs a number")
        if price <= 0:
            raise tracker.TradeError("VIX levels must be above 0")
        out.append({"price": round(price, 2), "dir": "down" if l.get("dir") == "down" else "up",
                    "label": str(l.get("label", "")).strip()[:60] or f"VIX {price:g}",
                    "note": str(l.get("note", "")).strip()[:300],
                    "color": str(l.get("color", "#6faee9"))[:20]})
    out.sort(key=lambda l: l["price"])
    with open(VIX_LEVELS, "w") as f:
        json.dump(out, f, indent=2)
    return out


def num(body, key, cast=float, default=None):
    v = body.get(key)
    if v in (None, ""):
        return default
    try:
        return cast(v)
    except (TypeError, ValueError):
        raise tracker.TradeError(f"{key} must be a number")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def guard(self, fn):
        try:
            self.send(200, fn())
        except (QuoteError, tracker.TradeError) as e:
            self.send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self.send(500, {"error": f"{type(e).__name__}: {e}"})

    def local_host(self):
        # Refuse requests addressed to any other host name (DNS-rebinding protection).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host in ("127.0.0.1", "localhost"):
            return True
        self.send(403, {"error": "forbidden"})
        return False

    def do_GET(self):
        if not self.local_host():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            with open(INDEX, "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif url.path in STATIC:
            with open(os.path.join(HERE, "web", url.path.lstrip("/")), "rb") as f:
                self.send(200, f.read(), STATIC[url.path])
        elif url.path == "/api/chain":
            ticker = q.get("ticker", "").strip()
            if not ticker:
                return self.send(400, {"error": "Enter a ticker"})
            self.guard(lambda: chain(ticker, q.get("exp")))
        elif url.path == "/api/positions":
            self.guard(positions)
        elif url.path == "/api/vix":
            self.guard(lambda: dict(vix(), levels=vix_levels()))
        elif url.path == "/api/econ":
            self.guard(usd_events)
        elif url.path == "/api/news":
            self.guard(lambda: news_feed(q.get("tickers", "")))
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.local_host():
            return
        # Only accept requests from this app's own page.
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self.send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad JSON"})
        path = urlparse(self.path).path

        def add():
            with db_lock:
                trades = tracker.load(DB)
                t = tracker.add_trade(trades, str(body.get("ticker", "")), body.get("type"), body.get("side"),
                                      num(body, "strike"), str(body.get("exp", "")), num(body, "premium"),
                                      num(body, "contracts", int, 1), num(body, "fees"),
                                      body.get("opened") or None, body.get("notes", ""))
                tracker.save(DB, trades)
            return t

        def close():
            with db_lock:
                trades = tracker.load(DB)
                t = tracker.close_trade(trades, num(body, "id", int), num(body, "premium"),
                                        bool(body.get("expired")), bool(body.get("assigned")),
                                        num(body, "fees"), body.get("closed") or None)
                tracker.save(DB, trades)
            return t

        def add_position():
            legs = [{"type": l.get("type"), "side": l.get("side"), "strike": num(l, "strike"),
                     "exp": l.get("exp") or None, "premium": num(l, "premium"),
                     "contracts": num(l, "contracts", int, 1)} for l in body.get("legs") or []]
            with db_lock:
                trades = tracker.load(DB)
                out = tracker.add_position(trades, str(body.get("ticker", "")), legs, str(body.get("strategy", "")),
                                           num(body, "fees"), body.get("opened") or None,
                                           body.get("notes", ""))
                tracker.save(DB, trades)
            return out

        def close_position():
            legs = [{"id": num(l, "id", int), "premium": num(l, "premium"), "contracts": num(l, "contracts", int)}
                    for l in body.get("legs") or []]
            with db_lock:
                trades = tracker.load(DB)
                out = tracker.close_position(trades, legs, num(body, "fees"), body.get("closed") or None)
                tracker.save(DB, trades)
            return out

        def fee_quote():
            legs = [{"type": l.get("type"), "action": l.get("action"), "price": num(l, "price", float, 0.0),
                     "contracts": num(l, "contracts", int, 0) or 0} for l in body.get("legs") or []]
            total, parts = order_fees(str(body.get("ticker", "")), [l for l in legs if l["contracts"] > 0])
            return {"total": total, "parts": parts}

        def delete():
            with db_lock:
                trades = tracker.load(DB)
                out = tracker.delete_position(trades, num(body, "id", int))
                tracker.save(DB, trades)
            return out

        if path == "/api/fees":
            self.guard(fee_quote)
        elif path == "/api/trades":
            self.guard(add)
        elif path == "/api/trades/delete":
            self.guard(delete)
        elif path == "/api/trades/close":
            self.guard(close)
        elif path == "/api/positions":
            self.guard(add_position)
        elif path == "/api/vix/levels":
            self.guard(lambda: {"levels": save_vix_levels(body.get("levels"))})
        elif path == "/api/positions/close":
            self.guard(close_position)
        else:
            self.send(404, {"error": "not found"})


def main():
    global DB
    p = argparse.ArgumentParser(description="Option chain and trade tracker in your browser.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--db", default=tracker.DEFAULT_DB)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    DB = args.db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Sound Investment Solutions Option Tracker running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
