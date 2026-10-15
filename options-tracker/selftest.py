#!/usr/bin/env python3
"""Offline self-test for the option tracker. Stdlib only; uses a temporary trades file.

  python3 selftest.py

Never touches trades.json. Exits non-zero if any check fails.
"""
import contextlib
import io
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import options_data as od  # noqa: E402
import tracker  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {name} {detail}")


def near(name, got, want, tol=1e-6):
    check(name, got is not None and abs(got - want) <= tol, f"got {got}, want {want}")


def cli(db, *argv):
    """Run tracker.main on a temp db; return (stdout, exit message or None)."""
    out = io.StringIO()
    err = None
    with contextlib.redirect_stdout(out):
        try:
            tracker.main(["--db", db] + [str(a) for a in argv])
        except SystemExit as e:
            err = str(e.code)
    return out.getvalue(), err


# ---------- Black-Scholes ----------

def test_black_scholes():
    S = K = 100.0
    c = od.bs_price("call", S, K, 1.0, 0.20, 0.04)
    p = od.bs_price("put", S, K, 1.0, 0.20, 0.04)
    near("bs call", c, 9.925, 5e-4)
    near("put-call parity", c - p, S - K * math.exp(-0.04), 1e-9)
    g = od.greeks("call", S, K, 1.0, 0.20, 0.04)
    near("call delta", g["delta"], 0.6179, 5e-5)
    near("gamma", g["gamma"], 0.0191, 5e-5)
    near("theta/day", g["theta"], -0.0161, 5e-5)
    near("vega", g["vega"], 0.3814, 5e-5)
    gp = od.greeks("put", S, K, 1.0, 0.20, 0.04)
    near("put delta", gp["delta"], g["delta"] - 1, 1e-9)
    check("greeks none on junk iv", od.greeks("call", S, K, 1.0, 0.0) is None)
    near("expired call intrinsic", od.bs_price("call", 110, 100, 0, 0.2), 10.0)
    near("implied vol roundtrip", od.implied_vol("call", S, K, 1.0, c, 0.04), 0.20, 1e-4)


# ---------- CLI ----------

def test_cli(tmp):
    db = os.path.join(tmp, "cli.json")
    today = date.today()
    exp = (today + timedelta(days=30)).isoformat()
    exp_far = (today + timedelta(days=200)).isoformat()
    # 1: sold put 100 @ 2.00, closed at 0.50, fees 1 + 1 -> (2-0.5)*100 - 2 = 148
    out, err = cli(db, "add", "AAA", "--side", "sell", "--strike", 100, "--exp", exp, "--premium", 2.0, "--fees", 1)
    check("add ok", err is None and "#1" in out and "Hop" in out, out + str(err))
    out, err = cli(db, "close", 1, "--premium", 0.5, "--fees", 1)
    check("close ok", err is None, str(err))
    t = tracker.load(db)[0]
    near("closed realized", t["realized"], 148.0)
    check("closed status", t["status"] == "closed")
    check("bak written", os.path.exists(db + ".bak"))
    # 2: bought call 50 @ 3.00 x2, expires worthless, fees 0 -> -600
    cli(db, "add", "BBB", "--type", "call", "--side", "buy", "--strike", 50, "--exp", exp_far, "--premium", 3,
        "--contracts", 2, "--fees", 0)
    out, err = cli(db, "close", 2, "--expired")
    t = tracker.load(db)[1]
    check("expired status", t["status"] == "expired")
    near("expired realized", t["realized"], -600.0)
    check("skip category", t["category"] == "Skip")
    # 3: sold call 60 @ 1.00, assigned at intrinsic 4 -> (1-4)*100 = -300
    cli(db, "add", "CCC", "--type", "call", "--side", "sell", "--strike", 60, "--exp", exp, "--premium", 1, "--fees", 0)
    out, err = cli(db, "close", 3, "--assigned", "--premium", 4)
    t = tracker.load(db)[2]
    check("assigned status", t["status"] == "assigned")
    near("assigned realized", t["realized"], -300.0)
    out, err = cli(db, "close", 3, "--assigned")
    check("assigned needs premium", err is not None)
    out, err = cli(db, "close", 3, "--premium", 1)
    check("already closed rejected", err is not None and "already" in err, str(err))
    out, err = cli(db, "add", "ZZZ", "--side", "sell", "--strike", -5, "--exp", exp, "--premium", 1)
    check("bad strike rejected", err is not None)
    out, err = cli(db, "add", "ZZZ", "--side", "sell", "--strike", 5, "--exp", "not-a-date", "--premium", 1)
    check("bad date rejected", err is not None)
    out, err = cli(db, "report")
    check("report runs", err is None and "TOTAL" in out, out)
    rows = {n: (c, w, r) for n, c, w, r in tracker.report_groups(tracker.load(db))}
    check("report total", rows["TOTAL"][0] == 3 and rows["TOTAL"][1] == 1, str(rows))
    near("report total pnl", rows["TOTAL"][2], 148 - 600 - 300)
    out, err = cli(db, "premium")
    check("premium runs", err is None, str(err))
    # delete: single, needs --yes
    out, err = cli(db, "delete", 1)
    check("delete needs --yes", err is not None and len(tracker.load(db)) == 3)
    out, err = cli(db, "delete", 1, "--yes")
    check("delete single", err is None and len(tracker.load(db)) == 2)
    out, err = cli(db, "delete", 99, "--yes")
    check("delete missing", err is not None)
    # group delete as a whole
    trades = tracker.load(db)
    tracker.add_position(trades, "DDD", [
        {"type": "put", "side": "sell", "strike": 90, "exp": exp, "premium": 2, "contracts": 1},
        {"type": "put", "side": "buy", "strike": 85, "exp": exp, "premium": 1, "contracts": 1}], "Bull put", fees=0)
    tracker.save(db, trades)
    gid = tracker.load(db)[-1]["id"]
    n = len(tracker.load(db))
    cli(db, "delete", gid, "--yes")
    check("group deletes whole", len(tracker.load(db)) == n - 2)


# ---------- premium classification ----------

def pos(legs, status="open", exit_prices=None, fees=0.0, closed="2026-01-31"):
    trades = []
    exp = (date.today() + timedelta(days=30)).isoformat()
    full = []
    for l in legs:
        l = dict(l)
        l.setdefault("exp", None if l["type"] == "stock" else exp)
        l.setdefault("strike", 0 if l["type"] == "stock" else 100)
        l.setdefault("contracts", 1)
        full.append(l)
    tracker.add_position(trades, "TST", full, "x", fees=fees, opened="2026-01-05")
    if status != "open":
        for i, t in enumerate(trades):
            t.update(status=status, exit_premium=(exit_prices or [0] * len(trades))[i], exit_fees=0.0,
                     closed=closed)
    return trades


def test_premium():
    def pk(legs):
        return tracker.premium_kind(pos(legs))
    put = {"type": "put", "side": "sell", "premium": 2.0}
    check("cash-secured put", pk([put]) == "Cash-secured puts")
    check("short call", pk([{"type": "call", "side": "sell", "premium": 2.0}]) == "Short calls")
    check("covered call", pk([{"type": "stock", "side": "buy", "premium": 100},
                              {"type": "call", "side": "sell", "premium": 2.0}]) == "Covered calls")
    check("covered put", pk([{"type": "stock", "side": "sell", "premium": 100},
                             {"type": "put", "side": "sell", "premium": 2.0}]) == "Covered puts")
    check("credit spread", pk([{"type": "put", "side": "sell", "strike": 100, "premium": 2.0},
                               {"type": "put", "side": "buy", "strike": 95, "premium": 1.0}]) == "Credit spreads")
    check("short strangle", pk([{"type": "put", "side": "sell", "strike": 95, "premium": 1.5},
                                {"type": "call", "side": "sell", "strike": 105, "premium": 1.5}])
          == "Short straddles & strangles")
    check("debit trade excluded", pk([{"type": "call", "side": "buy", "premium": 2.0}]) is None)
    check("debit spread excluded", pk([{"type": "put", "side": "sell", "strike": 95, "premium": 1.0},
                                       {"type": "put", "side": "buy", "strike": 100, "premium": 2.0}]) is None)
    # report numbers: sold put 2.00, bought back at 0.50, fees 1.00 -> collected 200, kept 149
    trades = pos([put], status="closed", exit_prices=[0.5], fees=1.0)
    rep = tracker.premium_report(trades)
    near("premium collected", rep["total"]["collected"], 200.0)
    near("premium kept", rep["total"]["kept"], 149.0)
    check("premium win", rep["total"]["wins"] == 1)
    check("premium month", [m["month"] for m in rep["by_month"]] == ["2026-01"], str(rep["by_month"]))
    # open position counts as open credit
    rep = tracker.premium_report(pos([put]))
    near("open credit", rep["total"]["open_credit"], 200.0)


# ---------- fees ----------

def test_fees():
    from fees import order_fees
    total, _ = order_fees("SPY", [{"type": "put", "action": "sell", "price": 2.0, "contracts": 1}])
    near("sell option fee", total, 0.05)  # SEC 0.01 + OCC 0.04
    total, _ = order_fees("SPY", [{"type": "put", "action": "buy", "price": 2.0, "contracts": 10}])
    near("buy option fee", total, 0.40)


# ---------- server ----------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(port, method, path, host=None, origin=None, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    req.add_header("Host", host or f"127.0.0.1:{port}")
    if origin:
        req.add_header("Origin", origin)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_server(tmp):
    db = os.path.join(tmp, "srv.json")
    port = free_port()
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "app.py"), "--db", db, "--port", str(port),
                             "--no-browser"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        status, body = http(port, "GET", "/")
        check("GET / 200", status == 200 and b"<html" in body.lower(), str(status))
        status, _ = http(port, "GET", "/", host="evil.example.com")
        check("foreign Host 403", status == 403, str(status))
        status, _ = http(port, "POST", "/api/trades", origin="http://evil.example.com", body={})
        check("foreign Origin 403", status == 403, str(status))
        for name, path, kw in [
            ("empty trade", "/api/trades", dict(body={})),
            ("bad number", "/api/trades", dict(body={"ticker": "X", "type": "put", "side": "sell",
                                                     "strike": "abc", "exp": "2030-01-01", "premium": 1})),
            ("bad JSON", "/api/trades", dict(raw=b"{nope")),
            ("JSON list body", "/api/trades", dict(raw=b"[1,2]")),
            ("JSON null body", "/api/positions", dict(raw=b"null")),
            ("legs not objects", "/api/positions", dict(body={"ticker": "X", "legs": [1, "a"]})),
            ("delete missing", "/api/trades/delete", dict(body={"id": 5})),
            ("close missing id", "/api/trades/close", dict(body={})),
            ("fees bad legs", "/api/fees", dict(body={"ticker": "X", "legs": ["x"]})),
        ]:
            status, body = http(port, "POST", path, **kw)
            try:
                ok = status in (400, 404) and "error" in json.loads(body)
            except ValueError:
                ok = False
            check(f"{name} -> JSON error", ok, f"{status} {body[:80]}")
        status, body = http(port, "GET", "/api/chain")
        check("chain without ticker 400", status == 400)
        status, body = http(port, "GET", "/api/positions")
        check("positions empty", status == 200 and json.loads(body)["open"] == [], str(status))
        # a good trade through the API
        exp = (date.today() + timedelta(days=30)).isoformat()
        status, body = http(port, "POST", "/api/trades", body={
            "ticker": "aaa", "type": "put", "side": "sell", "strike": 10, "exp": exp, "premium": 1, "fees": 0})
        check("API add trade", status == 200 and json.loads(body)["ticker"] == "AAA", f"{status} {body[:80]}")
        check("API trade saved to temp db", len(tracker.load(db)) == 1)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_black_scholes()
        test_cli(tmp)
        test_premium()
        test_fees()
        test_server(tmp)
    print(f"selftest: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
