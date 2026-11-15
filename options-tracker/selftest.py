#!/usr/bin/env python3
"""Offline self-test for the option tracker (stdlib only, temp db, no network).

    python3 selftest.py

Never touches trades.json: every test uses a temporary --db file.
"""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import options_data as od  # noqa: E402
import tracker  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)))


def close(a, b, tol=1e-3):
    return abs(a - b) <= tol


def cli(db, *args):
    """Run the tracker CLI in-process; return (stdout, exit_ok)."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            tracker.main(["--db", db] + [str(a) for a in args])
        return buf.getvalue(), True
    except (SystemExit, tracker.TradeError) as e:
        return buf.getvalue() + repr(e), False


def load(db):
    with open(db) as f:
        return json.load(f)


# ---------------------------------------------------------------- Black-Scholes
def test_black_scholes():
    S = K = 100.0
    T, iv, r = 1.0, 0.20, 0.04
    c = od.bs_price("call", S, K, T, iv, r)
    p = od.bs_price("put", S, K, T, iv, r)
    check("bs call 9.925", close(c, 9.925, 0.001), c)
    g = od.greeks("call", S, K, T, iv, r)
    check("bs call delta 0.6179", close(g["delta"], 0.6179, 1e-4), g)
    check("bs gamma 0.0191", close(g["gamma"], 0.0191, 1e-4), g)
    check("bs theta/day -0.0161", close(g["theta"], -0.0161, 1e-4), g)
    check("bs vega 0.3814", close(g["vega"], 0.3814, 1e-4), g)
    check("put-call parity", close(c - p, S - K * pow(2.718281828459045, -r * T), 1e-6), (c, p))
    check("greeks None on junk IV", od.greeks("call", 100, 100, 1, 0.0) is None)
    check("bs expired = intrinsic", od.bs_price("put", 90, 100, 0, 0.2) == 10.0)


# ---------------------------------------------------------------- CLI
def test_cli():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.json")
    # short put 100 @ 2.00 x1, no fees
    out, ok = cli(db, "add", "XYZ", "--type", "put", "--side", "sell", "--strike", 100, "--exp", "2026-03-20",
                  "--premium", 2.0, "--contracts", 1, "--fees", 0, "--date", "2026-02-01")
    check("cli add", ok, out)
    check("db.bak not yet (first save)", not os.path.exists(db + ".bak"))
    out, ok = cli(db, "add", "XYZ", "--type", "call", "--side", "sell", "--strike", 110, "--exp", "2026-03-20",
                  "--premium", 1.0, "--fees", 0, "--date", "2026-02-01")
    check("cli add #2", ok, out)
    check("trades.json.bak written", os.path.exists(db + ".bak"))
    out, ok = cli(db, "close", 1, "--premium", 0.5, "--fees", 0, "--date", "2026-02-10")
    t = load(db)[0]
    check("close status/realized", t["status"] == "closed" and close(t["realized"], 150.0), t)
    out, ok = cli(db, "close", 2, "--expired", "--date", "2026-03-20")
    t = load(db)[1]
    check("expired realized +100", t["status"] == "expired" and close(t["realized"], 100.0), t)
    out, ok = cli(db, "close", 1, "--premium", 1)
    check("double close rejected", not ok)
    # assigned short call (long-less): intrinsic exit price required
    cli(db, "add", "ABC", "--type", "call", "--side", "sell", "--strike", 50, "--exp", "2026-03-20",
        "--premium", 1.0, "--fees", 0, "--date", "2026-02-01")
    out, ok = cli(db, "close", 3, "--assigned")
    check("assigned without intrinsic rejected", not ok)
    out, ok = cli(db, "close", 3, "--assigned", "--premium", 4.0, "--fees", 0, "--date", "2026-03-20")
    t = load(db)[2]
    check("assigned realized -300", t["status"] == "assigned" and close(t["realized"], -300.0), t)
    # assigned short put brings shares
    cli(db, "add", "PUT", "--type", "put", "--side", "sell", "--strike", 20, "--exp", "2026-03-20",
        "--premium", 0.5, "--fees", 0, "--date", "2026-02-01")
    out, ok = cli(db, "close", 4, "--assigned", "--date", "2026-03-20")
    tr = load(db)
    check("assigned put keeps premium", tr[3]["status"] == "assigned" and close(tr[3]["realized"], 50.0), tr[3])
    check("assigned put adds shares", any(x.get("type") == "stock" and x["ticker"] == "PUT" for x in tr))
    out, ok = cli(db, "report")
    check("report runs", ok and "Open legs" in out, out)
    out, ok = cli(db, "premium")
    check("premium runs", ok and "Cash-secured puts" in out, out)
    out, ok = cli(db, "list", "--status", "all")
    check("list runs", ok, out)
    out, ok = cli(db, "delete", 3)
    check("delete without --yes keeps trade", (not ok) and any(x["id"] == 3 for x in load(db)), out)
    out, ok = cli(db, "delete", 3, "--yes")
    check("delete --yes removes", ok and not any(x["id"] == 3 for x in load(db)), out)
    out, ok = cli(db, "delete", 99, "--yes")
    check("delete unknown id fails cleanly", not ok)
    out, ok = cli(db, "add", "XYZ", "--side", "sell", "--strike", -1, "--exp", "2026-03-20", "--premium", 1)
    check("bad strike rejected", not ok)
    out, ok = cli(db, "add", "XYZ", "--side", "sell", "--strike", 5, "--exp", "bad", "--premium", 1)
    check("bad date rejected", not ok)

    # group deletes as a whole
    trades = []
    tracker.add_position(trades, "GRP", [
        {"type": "put", "side": "sell", "strike": 100.0, "exp": "2026-03-20", "premium": 2.0, "contracts": 1},
        {"type": "put", "side": "buy", "strike": 95.0, "exp": "2026-03-20", "premium": 1.0, "contracts": 1}],
        "Bull put spread", fees=0.0, opened="2026-02-01")
    check("group legs share id", trades[0]["group"] and trades[0]["group"] == trades[1]["group"])
    gdb = os.path.join(d, "g.json")
    tracker.save(gdb, trades)
    out, ok = cli(gdb, "delete", trades[1]["id"], "--yes")
    check("group deletes as a whole", ok and load(gdb) == [], out)


# ---------------------------------------------------------------- premium classification
def prem_kind(legs, extra=()):
    trades = []
    tracker.add_position(trades, "T", legs, "s", fees=0.0, opened="2026-02-01")
    return tracker.premium_kind(trades)


def opt(type_, side, strike, prem, n=1):
    return {"type": type_, "side": side, "strike": float(strike), "exp": "2026-03-20", "premium": prem, "contracts": n}


def test_premium():
    stock = {"type": "stock", "side": "buy", "strike": None, "premium": 100.0, "contracts": 1}
    pk = prem_kind
    check("premium: cash-secured put", pk([opt("put", "sell", 100, 2)]) == "Cash-secured puts", pk([opt("put", "sell", 100, 2)]))
    check("premium: covered call", pk([stock, opt("call", "sell", 110, 1)]) == "Covered calls")
    check("premium: short call", pk([opt("call", "sell", 110, 1)]) == "Short calls")
    check("premium: credit spread", pk([opt("put", "sell", 100, 2), opt("put", "buy", 95, 1)]) == "Credit spreads")
    check("premium: short strangle", pk([opt("put", "sell", 90, 1), opt("call", "sell", 110, 1)]) is not None
          and pk([opt("put", "sell", 90, 1), opt("call", "sell", 110, 1)]) != "Credit spreads",
          pk([opt("put", "sell", 90, 1), opt("call", "sell", 110, 1)]))
    check("premium: long put excluded", pk([opt("put", "buy", 100, 2)]) is None)
    check("premium: debit spread excluded", pk([opt("call", "buy", 100, 3), opt("call", "sell", 105, 1)]) is None)
    sstock = {"type": "stock", "side": "sell", "strike": None, "premium": 100.0, "contracts": 1}
    check("premium: covered put", pk([sstock, opt("put", "sell", 90, 1)]) == "Covered puts", pk([sstock, opt("put", "sell", 90, 1)]))

    # numbers: one short put 100 @ 2.00 bought back at 0.50 -> collected 200, kept 150
    trades = []
    tracker.add_position(trades, "N", [opt("put", "sell", 100, 2.0)], fees=0.0, opened="2026-02-01")
    tracker.close_trade(trades, trades[0]["id"], 0.5, fees=0.0, closed="2026-02-10")
    rep = tracker.premium_report(trades)
    tot = rep["total"]
    check("premium numbers", close(tot["collected"], 200) and close(tot["kept"], 150) and tot["wins"] == 1, tot)
    check("premium months", rep["by_month"][0]["month"] == "2026-02" and close(rep["by_month"][0]["kept"], 150), rep["by_month"])


# ---------------------------------------------------------------- server
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(port, path, method="GET", headers=None, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_server():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "s.json")
    port = free_port()
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "app.py"), "--db", db, "--port", str(port),
                             "--no-browser"], cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.2)
        st, body = http(port, "/")
        check("server GET / 200", st == 200 and b"<html" in body.lower(), st)
        st, _ = http(port, "/", headers={"Host": "evil.example.com"})
        check("foreign Host -> 403", st == 403, st)
        st, _ = http(port, "/api/trades", "POST", {"Origin": "http://evil.example.com", "Content-Type": "application/json"}, b"{}")
        check("foreign Origin POST -> 403", st == 403, st)
        h = {"Content-Type": "application/json"}
        for name, b in (("bad JSON", b"{nope"), ("empty object", b"{}"), ("list body", b"[1]"),
                        ("string strike", b'{"ticker":"X","type":"put","side":"sell","strike":"abc","exp":"2026-03-20","premium":1}'),
                        ("bad legs", b'{"ticker":"X","legs":[1]}')):
            st, out = http(port, "/api/trades" if name != "bad legs" else "/api/positions", "POST", h, b)
            try:
                js = json.loads(out)
            except ValueError:
                js = None
            check("bad input (%s) -> JSON 400" % name, st == 400 and isinstance(js, dict) and "error" in js, (st, out[:80]))
        good = json.dumps({"ticker": "xyz<b>", "type": "put", "side": "sell", "strike": 100, "exp": "2099-01-15",
                           "premium": 2, "contracts": 1, "fees": 0, "notes": "<script>x</script>"}).encode()
        st, out = http(port, "/api/trades", "POST", h, good)
        check("valid add -> 200 or JSON 400", st in (200, 400) and isinstance(json.loads(out), (dict, list)), (st, out[:80]))
        st, _ = http(port, "/api/nope", "POST", h, b"{}")
        check("unknown POST path -> 404", st == 404, st)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def main():
    for fn in (test_black_scholes, test_cli, test_premium, test_server):
        try:
            fn()
        except Exception as e:  # a crash is a failure, not a silent skip
            import traceback
            traceback.print_exc()
            check(fn.__name__ + " crashed", False, repr(e))
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
