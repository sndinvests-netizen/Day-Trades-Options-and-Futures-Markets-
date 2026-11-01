#!/usr/bin/env python3
"""Offline self-test for the option tracker: stdlib + repo modules + a temp db. Run: python3 selftest.py

Never touches trades.json. Network is not used (the server tests hit only local, non-quote routes).
"""
import contextlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import options_data as od  # noqa: E402
import tracker  # noqa: E402


def cli(db, *argv):
    """Run the tracker CLI in-process; returns (exit message or None, stdout)."""
    out, err = io.StringIO(), None
    with contextlib.redirect_stdout(out):
        try:
            tracker.main(["--db", db] + [str(a) for a in argv])
        except SystemExit as e:
            err = str(e.code) if e.code not in (None, 0) else None
    return err, out.getvalue()


class BlackScholes(unittest.TestCase):
    def test_textbook(self):
        g = od.greeks("call", 100, 100, 1.0, 0.20, 0.04)
        self.assertAlmostEqual(od.bs_price("call", 100, 100, 1.0, 0.20, 0.04), 9.925, 3)
        self.assertAlmostEqual(g["delta"], 0.6179, 4)
        self.assertAlmostEqual(g["gamma"], 0.0191, 4)
        self.assertAlmostEqual(g["theta"], -0.0161, 4)
        self.assertAlmostEqual(g["vega"], 0.3814, 4)

    def test_put_call_parity(self):
        c = od.bs_price("call", 100, 100, 1.0, 0.20, 0.04)
        p = od.bs_price("put", 100, 100, 1.0, 0.20, 0.04)
        self.assertAlmostEqual(c - p, 100 - 100 * 2.718281828459045 ** -0.04, 6)

    def test_edge_cases(self):
        self.assertIsNone(od.greeks("call", 100, 100, 1.0, 0.0))
        self.assertIsNone(od.greeks("call", 0, 100, 1.0, 0.2))
        self.assertEqual(od.bs_price("put", 90, 100, 0, 0.2), 10)
        self.assertIsNone(od.implied_vol("call", 100, 100, 1.0, 0))
        iv = od.implied_vol("call", 100, 100, 1.0, od.bs_price("call", 100, 100, 1.0, 0.3))
        self.assertAlmostEqual(iv, 0.3, 3)


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "t.json")

    def tearDown(self):
        self.dir.cleanup()

    def trades(self):
        return tracker.load(self.db)

    def test_close_expire_assign_delete_report(self):
        # short put 2 contracts @1.50 -> buy back @0.50: (1.50-0.50)*100*2 = +200
        cli(self.db, "add", "SPY", "--side", "sell", "--strike", 400, "--exp", "2030-01-18",
            "--premium", 1.5, "--contracts", 2, "--fees", 0, "--date", "2029-12-01")
        err, out = cli(self.db, "close", 1, "--premium", 0.5, "--fees", 0)
        self.assertIsNone(err)
        self.assertIn("200.00", out)
        # expired worthless: 1.00 * 100 = +100
        cli(self.db, "add", "QQQ", "--side", "sell", "--strike", 300, "--exp", "2030-01-18",
            "--premium", 1.0, "--fees", 0, "--date", "2029-12-01")
        cli(self.db, "close", 2, "--expired")
        self.assertEqual(self.trades()[1]["realized"], 100.0)
        self.assertEqual(self.trades()[1]["status"], "expired")
        # assigned short put: keeps premium (+250), shares lot appears
        cli(self.db, "add", "IWM", "--side", "sell", "--strike", 200, "--exp", "2030-01-18",
            "--premium", 2.5, "--fees", 0, "--date", "2029-12-01")
        cli(self.db, "close", 3, "--assigned")
        t = self.trades()
        self.assertEqual(t[2]["status"], "assigned")
        self.assertEqual(t[2]["realized"], 250.0)
        self.assertTrue(any(x["type"] == "stock" and x["ticker"] == "IWM" for x in t))
        # bought call losing: (0.2-1.0)*100 = -80
        cli(self.db, "add", "DIA", "--type", "call", "--side", "buy", "--strike", 350, "--exp", "2030-01-18",
            "--premium", 1.0, "--fees", 0, "--date", "2029-12-01")
        did = max(x["id"] for x in self.trades())
        cli(self.db, "close", did, "--premium", 0.2, "--fees", 0)
        err, out = cli(self.db, "report")
        self.assertIsNone(err)
        self.assertIn("TOTAL", out)
        total = [r for r in tracker.report_groups(self.trades()) if r[0] == "TOTAL"][0]
        self.assertEqual((total[1], total[2], round(total[3], 2)), (4, 3, 470.0))
        # errors
        err, _ = cli(self.db, "close", 1, "--premium", 1)
        self.assertIn("already", err)
        err, _ = cli(self.db, "close", 999, "--expired")
        self.assertIn("No trade", err)
        err, _ = cli(self.db, "add", "SPY", "--side", "buy", "--strike", 0, "--exp", "2030-01-18", "--premium", 1)
        self.assertIsNotNone(err)
        err, _ = cli(self.db, "add", "SPY", "--side", "buy", "--strike", 5, "--exp", "bad", "--premium", 1)
        self.assertIsNotNone(err)

    def test_delete_and_backup(self):
        cli(self.db, "add", "SPY", "--side", "sell", "--strike", 400, "--exp", "2030-01-18",
            "--premium", 1.5, "--fees", 0, "--date", "2029-12-01")
        cli(self.db, "add", "QQQ", "--side", "sell", "--strike", 300, "--exp", "2030-01-18",
            "--premium", 1.5, "--fees", 0, "--date", "2029-12-01")
        self.assertTrue(os.path.exists(self.db + ".bak"))
        err, _ = cli(self.db, "delete", 1)
        self.assertIn("--yes", err)
        self.assertEqual(len(self.trades()), 2)
        cli(self.db, "delete", 1, "--yes")
        self.assertEqual([t["ticker"] for t in self.trades()], ["QQQ"])
        with open(self.db + ".bak") as f:
            self.assertEqual(len(json.load(f)), 2)

    def test_group_delete(self):
        trades = []
        tracker.add_position(trades, "SPY", [
            {"type": "put", "side": "sell", "strike": 400, "exp": "2030-01-18", "premium": 2.0, "contracts": 1},
            {"type": "put", "side": "buy", "strike": 390, "exp": "2030-01-18", "premium": 1.0, "contracts": 1}],
            "Bull put spread", fees=0, opened="2029-12-01")
        tracker.save(self.db, trades)
        cli(self.db, "delete", 2, "--yes")
        self.assertEqual(self.trades(), [])

    def pos(self, ticker, legs):
        trades = []
        tracker.add_position(trades, ticker, legs, "x", fees=0, opened="2029-12-01")
        return trades

    def leg(self, type, side, strike=100, prem=1.0, n=1, exp="2030-01-18"):
        return {"type": type, "side": side, "strike": strike, "exp": exp, "premium": prem, "contracts": n}

    def test_premium_classification(self):
        L = self.leg
        cases = [
            ("Cash-secured puts", [L("put", "sell", prem=2)]),
            ("Short calls", [L("call", "sell", prem=2)]),
            ("Covered calls", [L("call", "sell", prem=2), {"type": "stock", "side": "buy", "premium": 95, "contracts": 1}]),
            ("Covered puts", [L("put", "sell", prem=2), {"type": "stock", "side": "sell", "premium": 95, "contracts": 1}]),
            ("Credit spreads", [L("put", "sell", 100, 2), L("put", "buy", 90, 1)]),
            ("Short straddles & strangles", [L("put", "sell", 90, 1), L("call", "sell", 110, 1)]),
            (None, [L("put", "buy", prem=2)]),
            (None, [L("call", "buy", 100, 2), L("call", "sell", 110, 1)]),  # debit spread
        ]
        for want, legs in cases:
            trades = self.pos("SPY", legs)
            self.assertEqual(tracker.premium_kind(trades), want, want)

    def test_premium_numbers(self):
        # CSP 2 contracts @1.50 closed @0.50: collected 300, kept 200
        cli(self.db, "add", "SPY", "--side", "sell", "--strike", 400, "--exp", "2030-01-18",
            "--premium", 1.5, "--contracts", 2, "--fees", 0, "--date", "2029-12-01")
        cli(self.db, "close", 1, "--premium", 0.5, "--fees", 0, "--date", "2029-12-20")
        cli(self.db, "add", "QQQ", "--side", "sell", "--strike", 300, "--exp", "2030-01-18",
            "--premium", 1.0, "--fees", 0, "--date", "2029-12-05")
        rep = tracker.premium_report(self.trades())
        self.assertEqual(rep["total"]["collected"], 400.0)
        self.assertEqual(rep["total"]["kept"], 200.0)
        self.assertEqual(rep["total"]["open_credit"], 100.0)
        self.assertEqual(rep["total"]["wins"], 1)
        err, out = cli(self.db, "premium")
        self.assertIsNone(err)
        self.assertIn("Cash-secured puts", out)


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.port = 8797
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "app.py"), "--db", os.path.join(cls.dir.name, "t.json"),
             "--port", str(cls.port), "--no-browser"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                cls.req("GET", "/")
                break
            except OSError:
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()
        cls.dir.cleanup()

    @classmethod
    def req(cls, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, data, h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw

    def test_index(self):
        status, raw = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", raw.lower())

    def test_foreign_host(self):
        self.assertEqual(self.req("GET", "/", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/positions", headers={"Host": "evil.example:8797"})[0], 403)

    def test_foreign_origin(self):
        s, _ = self.req("POST", "/api/trades", {}, {"Origin": "http://evil.example"})
        self.assertEqual(s, 403)

    def test_bad_input_is_json_error(self):
        origin = {"Origin": "http://127.0.0.1:%d" % self.port}
        bad = [("/api/trades", {"ticker": "SPY", "type": "put", "side": "sell", "strike": "abc"}),
               ("/api/trades", {}),
               ("/api/trades/close", {"id": 99}),
               ("/api/trades/delete", {"id": "x"}),
               ("/api/positions", {"ticker": "SPY", "legs": []}),
               ("/api/positions", {"ticker": "SPY", "legs": [{}]}),
               ("/api/positions", {"ticker": "SPY", "legs": "oops"}),
               ("/api/vix/levels", {"levels": [{"price": "x"}]}),
               ("/api/vix/levels", {"levels": "bad"}),
               ("/api/fees", {"legs": [{"type": "put", "action": "sell", "price": "x", "contracts": 1}]}),
               ("/api/trades", [1, 2]),
               ("/api/trades", b"{not json"),
               ("/api/nothing", {})]
        for path, body in bad:
            s, raw = self.req("POST", path, body, origin)
            self.assertIn(s, (400, 404), (path, body, s, raw))
            self.assertIn("error", json.loads(raw), (path, body))
        s, raw = self.req("GET", "/api/chain")
        self.assertEqual(s, 400)
        self.assertIn("error", json.loads(raw))

    def test_positions_empty(self):
        s, raw = self.req("GET", "/api/positions")
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(raw)["open"], [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
