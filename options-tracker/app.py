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
import re
import subprocess
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.parse import quote as _urlquote
from http.cookies import SimpleCookie
import hmac
import ipaddress

import tracker
import earnings
import screener
import leaps
import putsell
import smartmoney
from econ_calendar import usd_events
from fear_greed import fear_greed
from breadth import breadth
from fees import order_fees
from options_data import GEX_DAYS, GEX_MAX_EXPS, QuoteError, chain, gex, news, quote, vix, vix_history

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
            "collateral": collateral, "long_cost": long_cost, "holdings": tracker.share_holdings(trades)}


LAN_KEY_FILE = os.path.join(HERE, "lan_key.txt")    # exists = home-network mode (kept out of git)
def private_ip(s):
    """True for a home-network address like 192.168.0.112 (not loopback, not a host name)."""
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback


def lan_key():
    try:
        with open(LAN_KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


NEWS_WATCH = os.path.join(HERE, "news_watch.json")   # extra tickers to always follow in the News panel (kept out of git)


def news_watch():
    try:
        with open(NEWS_WATCH) as f:
            return [str(t).strip().upper() for t in json.load(f) if str(t).strip()]
    except (OSError, ValueError, TypeError):
        return []


def news_feed(extra):
    """Headlines for every ticker you hold open, your news list, and any asked for (e.g. the chain you're viewing)."""
    with db_lock:
        trades = tracker.load(DB)
    held = sorted({t["ticker"] for t in trades if t["status"] == "open"})
    watch = news_watch()
    asked = [x.strip().upper() for x in extra.split(",") if x.strip()][:5]
    tickers = list(dict.fromkeys(asked + held + watch))
    return {"tickers": tickers, "held": held, "watch": watch, "stories": news(tickers)[:60]}


def earnings_feed():
    """Upcoming reports for the earnings watchlist plus every ticker held open, with each one's latest expiration."""
    with db_lock:
        trades = tracker.load(DB)
    held = {}
    for t in trades:
        if t["status"] == "open":
            held[t["ticker"]] = max(held.get(t["ticker"], ""), t.get("exp") or "")
    return earnings.upcoming(held)


def save_earnings_watch(tickers):
    try:
        earnings.save_watchlist(tickers)
    except ValueError as e:
        raise tracker.TradeError(str(e))
    return earnings_feed()


def save_leaps_watch(tickers, term=None):
    try:
        leaps.save_watchlist(tickers)
    except ValueError as e:
        raise tracker.TradeError(str(e))
    return leaps.scan(term=term or "leap")


def screener_feed(q):
    """Stocks at support. Query: min_cap, min_oi, near (%), stocks_only, at_support (true/false), level (any|50d|200d|ma), refresh=1."""
    def flag(key, default):
        v = q.get(key)
        return default if v in (None, "") else v.lower() not in ("0", "false", "no", "off")
    min_cap = num(q, "min_cap", float, screener.DEFAULT_MIN_CAP)
    min_oi = num(q, "min_oi", float, screener.DEFAULT_MIN_OI)
    near = num(q, "near", float, screener.DEFAULT_NEAR)
    if min_cap < 0 or min_oi < 0 or not 0 < near <= 25:
        raise tracker.TradeError("min_cap and min_oi can't be negative, and near must be between 0 and 25%")
    level = (q.get("level") or "any").lower()
    if level not in screener.LEVELS:
        raise tracker.TradeError("level must be any, 50d, 200d or ma")
    rsi = (q.get("rsi") or "any").lower()
    if rsi not in screener.RSI_MODES:
        raise tracker.TradeError("rsi must be any, oversold or overbought")
    return screener.screen(min_cap, min_oi, near, flag("stocks_only", True), flag("at_support", True),
                           flag("refresh", False), level, rsi)


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
    with open(VIX_LEVELS + ".tmp", "w") as f:
        json.dump(out, f, indent=2)
    os.replace(VIX_LEVELS + ".tmp", VIX_LEVELS)
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
        client = self.client_address[0]
        if host in ("127.0.0.1", "localhost") and client in ("127.0.0.1", "::1"):
            return True
        # Home-network mode (lan_key.txt exists): a phone on the same Wi-Fi may connect by this
        # Mac's private IP, but only with the secret key: once in the link (?key=...), then a cookie.
        key = lan_key()
        if key and private_ip(host) and private_ip(client):
            q = parse_qs(urlparse(self.path).query)
            given = (q.get("key") or [""])[0]
            if given and hmac.compare_digest(given, key):
                self.send_response(302)
                self.send_header("Set-Cookie", f"trk_key={key}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict")
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            jar = SimpleCookie(self.headers.get("Cookie") or "")
            if "trk_key" in jar and hmac.compare_digest(jar["trk_key"].value, key):
                return True
            self.send(403, b"Locked. Open the tracker with the link that has the key.", "text/plain; charset=utf-8")
            return False
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
        elif url.path == "/api/quote":
            # One contract's bid/ask/mark, e.g. the new leg in the Roll dialog.
            ticker, kind = q.get("ticker", "").strip(), q.get("type", "put")
            if not ticker or not q.get("exp") or not q.get("strike") or kind not in ("put", "call"):
                return self.send(400, {"error": "ticker, exp, strike and type put/call needed"})
            self.guard(lambda: quote(ticker, q["exp"], num(q, "strike"), kind))
        elif url.path == "/api/gex":
            ticker = q.get("ticker", "").strip()
            if not ticker:
                return self.send(400, {"error": "Enter a ticker"})
            self.guard(lambda: gex(ticker, num(q, "days", int, GEX_DAYS), num(q, "exps", int, GEX_MAX_EXPS)))
        elif url.path == "/api/positions":
            self.guard(positions)
        elif url.path == "/api/vix":
            self.guard(lambda: dict(vix(), levels=vix_levels()))
        elif url.path == "/api/vix/history":
            self.guard(vix_history)
        elif url.path == "/api/econ":
            self.guard(usd_events)
        elif url.path == "/api/feargreed":
            self.guard(fear_greed)
        elif url.path == "/api/breadth":
            self.guard(breadth)
        elif url.path == "/api/earnings":
            self.guard(earnings_feed)
        elif url.path == "/api/news":
            self.guard(lambda: news_feed(q.get("tickers", "")))
        elif url.path == "/api/screener":
            self.guard(lambda: screener_feed(q))
        elif url.path == "/api/leaps/one":
            self.guard(lambda: leaps.lookup(q.get("ticker"), q.get("term") or "leap"))
        elif url.path == "/api/putsell/one":
            self.guard(lambda: putsell.lookup(q.get("ticker"), q.get("right") or "put"))
        elif url.path == "/api/putsell":
            self.guard(lambda: putsell.scan(q.get("refresh") == "1"))
        elif url.path == "/api/smartmoney":
            self.guard(lambda: smartmoney.feed(q.get("refresh") == "1"))
        elif url.path == "/api/leaps":
            self.guard(lambda: leaps.scan(q.get("refresh") == "1", q.get("mode") or "watch", q.get("term") or "leap"))
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
        # A body that isn't an object, or legs/levels that aren't lists of objects, would crash the
        # handlers below with a 500: answer 400 instead.
        if not isinstance(body, dict):
            return self.send(400, {"error": "the request body must be a JSON object"})
        for k in ("legs", "levels"):
            if body.get(k) is not None and not (isinstance(body[k], list) and all(isinstance(x, dict) for x in body[k])):
                return self.send(400, {"error": f"{k} must be a list of objects"})
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

        def add_shares():
            with db_lock:
                trades = tracker.load(DB)
                t = tracker.add_shares(trades, str(body.get("ticker", "")), num(body, "lots", int, 1),
                                       num(body, "price"), num(body, "fees", float, 0.0),
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

        def roll():
            with db_lock:
                trades = tracker.load(DB)
                old, new = tracker.roll_trade(trades, num(body, "id", int), num(body, "premium"), num(body, "strike"),
                                              body.get("exp") or None, num(body, "new_premium"), num(body, "contracts", int),
                                              num(body, "new_contracts", int), body.get("type") or None,
                                              num(body, "fees"), body.get("closed") or None)
                tracker.save(DB, trades)
            return {"old": old, "new": new}

        def add_to():
            with db_lock:
                trades = tracker.load(DB)
                t = tracker.add_to_trade(trades, num(body, "id", int), num(body, "contracts", int),
                                         num(body, "premium"), num(body, "fees"), body.get("added") or None)
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
        elif path == "/api/shares":
            self.guard(add_shares)
        elif path == "/api/trades/delete":
            self.guard(delete)
        elif path == "/api/trades/close":
            self.guard(close)
        elif path == "/api/trades/roll":
            self.guard(roll)
        elif path == "/api/trades/add-to":
            self.guard(add_to)
        elif path == "/api/positions":
            self.guard(add_position)
        elif path == "/api/vix/levels":
            self.guard(lambda: {"levels": save_vix_levels(body.get("levels"))})
        elif path == "/api/earnings/watch":
            self.guard(lambda: save_earnings_watch(body.get("tickers")))
        elif path == "/api/smartmoney/alerts":
            self.guard(lambda: {"alerts": smartmoney.save_alert_settings(body)})
        elif path == "/api/leaps/watch":
            self.guard(lambda: save_leaps_watch(body.get("tickers"), body.get("term")))
        elif path == "/api/positions/close":
            self.guard(close_position)
        elif path == "/api/open-chart":
            self.guard(lambda: open_chart(body.get("symbol")))
        elif path == "/api/tv-line":
            self.guard(lambda: tv_lines(body.get("symbol"), body.get("lines") or [body], body.get("clear_prefix")))
        else:
            self.send(404, {"error": "not found"})


def open_chart(symbol):
    """Open a TradingView chart in Google Chrome, where the TradingView login lives, whatever browser shows the tracker."""
    symbol = str(symbol or "").upper()
    if not re.fullmatch(r"[A-Z0-9.^=!:_-]{1,20}", symbol):
        raise QuoteError("bad symbol")
    url = "https://www.tradingview.com/chart/?symbol=" + symbol
    if subprocess.run(["open", "-a", "Google Chrome", url], capture_output=True).returncode:
        raise QuoteError("couldn't open Google Chrome")
    return {"opened": url}


TV_LAYOUT = "https://www.tradingview.com/chart/8Ie4duiG/"   # the owner's "Option Layout" chart
TV_COLORS = {"call": "#f23645", "put": "#089981", "flip": "#2962ff", "strike": "#787b86"}
_tv_lock = threading.Lock()


def _osa(script, *args, timeout=20):
    try:
        r = subprocess.run(["osascript", "-e", script, *[str(a) for a in args]], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise QuoteError(f"Chrome didn't answer within {timeout} s (a dialog may be open in a tab)")
    if r.returncode:
        err = r.stderr.strip()
        if "Allow JavaScript from Apple Events" in err:
            raise QuoteError("Chrome blocks this until you turn on View > Developer > Allow JavaScript from Apple Events (one time)")
        raise QuoteError("Chrome: " + (err.split("execution error:")[-1].strip() or "AppleScript failed"))
    return r.stdout.strip()


_TV_TABS = """tell application "Google Chrome"
set out to ""
repeat with w in windows
repeat with t in tabs of w
if URL of t contains "tradingview.com/chart" then set out to out & (id of w) & "|" & (id of t) & "|" & (URL of t) & linefeed
end repeat
end repeat
return out
end tell"""
_TV_EXEC = """on run argv
tell application "Google Chrome" to return execute (tab id ((item 2 of argv) as integer) of window id ((item 1 of argv) as integer)) javascript (item 3 of argv)
end run"""
_TV_OPEN = """on run argv
tell application "Google Chrome"
if (count windows) = 0 then make new window
tell window 1 to set t to make new tab with properties {URL:(item 1 of argv)}
return ((id of window 1) as text) & "|" & ((id of t) as text)
end tell
end run"""
_TV_SETURL = """on run argv
tell application "Google Chrome" to set URL of (tab id ((item 2 of argv) as integer) of window id ((item 1 of argv) as integer)) to (item 3 of argv)
end run"""
_TV_SYM = "(window.TradingViewApi && TradingViewApi.activeChart && TradingViewApi.activeChart().symbol()) || ''"


def _tv_eval(tab, expr, wait=4.0):
    """Run `expr` in the TradingView page itself and return its JSON-able value.
    AppleScript's `execute javascript` runs in an isolated world that can't see TradingViewApi, and the page's
    CSP blocks injected <script> tags, so the code goes in as a javascript: URL (page world) and leaves its
    result in a data attribute that the isolated world can read back."""
    w, t = tab
    _osa(_TV_EXEC, w, t, "delete document.documentElement.dataset.cx; 'ok'")
    code = ("void((function(){var r;try{r={v:(%s)}}catch(e){r={e:String(e)}}"
            "document.documentElement.dataset.cx=JSON.stringify(r)})())" % expr)
    _osa(_TV_SETURL, w, t, "javascript:" + _urlquote(code, safe=""))
    end = time.time() + wait
    while time.time() < end:
        out = _osa(_TV_EXEC, w, t, "document.documentElement.dataset.cx || ''")
        if out and out != "missing value":
            r = json.loads(out)
            if "e" in r:
                raise QuoteError("TradingView: " + r["e"])
            return r.get("v")
        time.sleep(0.25)
    raise QuoteError("TradingView page didn't answer")


def _tv_tab(sym):
    """A Chrome tab whose TradingView chart shows `sym`, opening the Option Layout chart on it if none does."""
    tabs = [l.split("|", 2) for l in _osa(_TV_TABS).splitlines() if l.count("|") >= 2]
    # tabs whose address names the ticker first: they're the likely match, so most draws need one check
    tabs.sort(key=lambda x: not re.search(r"symbol=([A-Z_]+(%3A|:))?" + re.escape(sym) + r"(&|$)", x[2], re.I))
    for w_id, t_id, _url in tabs:
        try:
            cur = _tv_eval([w_id, t_id], _TV_SYM, wait=2) or ""
        except QuoteError as e:
            if "Allow JavaScript" in str(e):
                raise
            continue
        if cur.split(":")[-1].upper() == sym:
            return [w_id, t_id]
    tab = _osa(_TV_OPEN, TV_LAYOUT + "?symbol=" + sym).split("|")
    if len(tab) != 2:
        raise QuoteError("couldn't open a TradingView tab in Chrome")
    deadline = time.time() + 25
    while True:
        time.sleep(1)
        try:
            cur = _tv_eval(tab, _TV_SYM, wait=1.5) or ""
        except QuoteError as e:
            if "Allow JavaScript" in str(e):
                raise
            cur = ""
        if cur.split(":")[-1].upper() == sym:
            time.sleep(1.5)   # let the series finish loading before drawing
            return tab
        if time.time() > deadline:
            raise QuoteError(f"TradingView didn't load {sym} in time (logged out?)")


_TV_LABEL = re.compile(r"[^\w .·()+/%:&-]")


def tv_lines(symbol, lines, clear_prefix=None):
    """Draw labeled horizontal lines on a TradingView chart of `symbol` in Chrome.
    Each line: {price, label, kind (call/put/flip/strike/level), color (#rrggbb, for kind "level")}.
    A redraw of a GEX wall/flip replaces the old one; lines with the same label are replaced;
    `clear_prefix` first removes every line whose label starts with it (e.g. a full redraw of the VIX levels)."""
    sym = str(symbol or "").upper().replace("-", ".")
    if not re.fullmatch(r"[A-Z0-9.^=!_]{1,15}", sym):
        raise QuoteError("bad symbol")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 30:
        raise QuoteError("nothing to draw")
    out = []
    for l in lines:
        try:
            price = float(l.get("price"))
        except (TypeError, ValueError, AttributeError):
            raise QuoteError("bad price")
        if not 0 < price < 1e7:
            raise QuoteError("bad price")
        kind = l.get("kind") if l.get("kind") in TV_COLORS or l.get("kind") == "level" else "strike"
        color = l.get("color") if kind == "level" and re.fullmatch(r"#[0-9a-fA-F]{6}", str(l.get("color") or "")) else TV_COLORS.get(kind, "#787b86")
        label = _TV_LABEL.sub("", str(l.get("label") or ""))[:60] or f"{price:g}"
        prefix = {"call": "Call wall", "put": "Put wall", "flip": "Gamma flip"}.get(kind)
        out.append({"price": price, "text": label, "color": color, "prefix": prefix})
    clear = _TV_LABEL.sub("", str(clear_prefix or ""))[:40] or None
    with _tv_lock:
        tab = _tv_tab(sym)
        js = """(function(){
  const c = TradingViewApi.activeChart(), L = %s, CLEAR = %s;
  for (const s of c.getAllShapes()) {
    try { const t = (c.getShapeById(s.id).getProperties() || {}).text || "";
      if ((CLEAR && t.startsWith(CLEAR)) || L.some(l => t === l.text || (l.prefix && t.startsWith(l.prefix + " ") && t.endsWith("(GEX)")))) c.removeEntity(s.id); } catch (e) {}
  }
  const now = Math.floor(Date.now() / 1000);
  for (const l of L) c.createShape({time: now, price: l.price}, {shape: "horizontal_line", text: l.text, lock: false,
    overrides: {linecolor: l.color, textcolor: l.color, linewidth: 2, linestyle: 0, fontsize: 12, showLabel: true, horzLabelsAlign: "right"}});
  return "ok";
})()""" % (json.dumps(out), json.dumps(clear))
        _tv_eval(tab, js)
        time.sleep(0.8)
        check = ("(function(){const c=TradingViewApi.activeChart(),T=%s;const have=c.getAllShapes().map(s=>{try{return (c.getShapeById(s.id).getProperties()||{}).text}catch(e){return ''}});"
                 "return T.filter(t=>have.includes(t)).length})()") % json.dumps([o["text"] for o in out])
        n = _tv_eval(tab, check) or 0
        if n < len(out):
            raise QuoteError(f"TradingView only took {n} of {len(out)} lines; is the chart fully loaded?")
    return {"drawn": [o["text"] for o in out], "symbol": sym}


def main():
    global DB
    p = argparse.ArgumentParser(description="Option chain and trade tracker in your browser.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--db", default=tracker.DEFAULT_DB)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    DB = args.db
    lan = bool(lan_key())
    server = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Sound Investment Solutions Option Tracker running at {url}  (Ctrl+C to stop)")
    if lan:
        print("Home-network mode is on (lan_key.txt): phones on this Wi-Fi need the link with ?key=")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
