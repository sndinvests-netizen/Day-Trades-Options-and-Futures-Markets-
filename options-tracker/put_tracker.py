#!/usr/bin/env python3
"""Put option tracker: log bought and sold puts, sorted into Hop / Skip / Leap.

Categories are set from days to expiration (DTE) on the day the trade is opened:
  Hop   0-90 days
  Skip  91-360 days   (about six months)
  Leap  over 360 days

Live prices come from Yahoo Finance via yfinance (free, may be delayed ~15 min).

Usage examples:
  python put_tracker.py add SPY --side sell --strike 550 --exp 2026-12-18 --premium 6.40
  python put_tracker.py add AAPL --side buy --strike 200 --exp 2027-12-17 --premium 14.10 --contracts 2
  python put_tracker.py list --live
  python put_tracker.py close 3 --premium 1.05
  python put_tracker.py close 4 --expired
  python put_tracker.py close 5 --assigned
  python put_tracker.py report
"""
import argparse
import json
import math
import os
import sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "trades.json")
MULTIPLIER = 100
RISK_FREE_RATE = 0.04
ASSIGNMENT_WARN_PCT = 0.02  # warn when a short put's stock is within 2% of the strike


# ---------- storage ----------

def load(db):
    if not os.path.exists(db):
        return []
    with open(db) as f:
        return json.load(f)


def save(db, trades):
    with open(db, "w") as f:
        json.dump(trades, f, indent=2)


# ---------- math ----------

def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def dte(exp, on=None):
    return (parse_date(exp) - (on or date.today())).days


def category(days):
    if days <= 90:
        return "Hop"
    if days <= 360:
        return "Skip"
    return "Leap"


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def put_delta(spot, strike, days, iv, r=RISK_FREE_RATE):
    """Black-Scholes delta of a long put (negative number)."""
    if not spot or not iv or days <= 0:
        return None
    t = days / 365
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * t) / (iv * math.sqrt(t))
    return norm_cdf(d1) - 1


def sign(trade):
    """+1 for a bought put (profits when price rises), -1 for a sold put."""
    return 1 if trade["side"] == "buy" else -1


def pnl(trade, exit_price, exit_fees=0.0):
    gross = sign(trade) * (exit_price - trade["premium"]) * MULTIPLIER * trade["contracts"]
    return gross - trade["fees"] - exit_fees


def stats(trade):
    """Static numbers that don't need live data."""
    n, k, p = trade["contracts"], trade["strike"], trade["premium"]
    out = {"breakeven": k - p}
    if trade["side"] == "buy":
        out["max_loss"] = p * MULTIPLIER * n
        out["max_profit"] = (k - p) * MULTIPLIER * n
    else:
        out["max_profit"] = p * MULTIPLIER * n
        out["max_loss"] = (k - p) * MULTIPLIER * n
        out["collateral"] = k * MULTIPLIER * n
        days = max(trade["dte_at_open"], 1)
        out["annualized"] = (p / k) * (365 / days)
    return out


# ---------- live data ----------

class QuoteError(Exception):
    pass


def yahoo_quote(ticker, exp, strike):
    """Return dict(spot, bid, ask, last, mark, iv) for one put contract."""
    try:
        import yfinance as yf
    except ImportError:
        raise QuoteError("yfinance not installed: run  pip install yfinance")
    t = yf.Ticker(ticker)
    try:
        if exp not in t.options:
            raise QuoteError(f"{ticker} has no {exp} expiration listed")
        puts = t.option_chain(exp).puts
        spot = float(t.fast_info["last_price"])
    except QuoteError:
        raise
    except Exception as e:
        raise QuoteError(f"{ticker}: {e}")
    row = puts[abs(puts["strike"] - strike) < 1e-6]
    if row.empty:
        raise QuoteError(f"{ticker} {exp} {strike}P not found in chain")
    row = row.iloc[0]
    bid, ask, last = float(row["bid"] or 0), float(row["ask"] or 0), float(row["lastPrice"] or 0)
    mark = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    return {"spot": spot, "bid": bid, "ask": ask, "last": last, "mark": mark,
            "iv": float(row["impliedVolatility"] or 0)}


QUOTE = yahoo_quote  # swap for a broker API (Schwab, Tastytrade, Tradier) later


# ---------- commands ----------

def cmd_add(args, trades):
    opened = parse_date(args.date) if args.date else date.today()
    parse_date(args.exp)
    days = dte(args.exp, opened)
    if days < 0:
        sys.exit("Expiration is before the open date.")
    trade = {
        "id": max([t["id"] for t in trades], default=0) + 1,
        "ticker": args.ticker.upper(),
        "side": args.side,
        "strike": args.strike,
        "exp": args.exp,
        "contracts": args.contracts,
        "premium": args.premium,
        "fees": args.fees,
        "opened": opened.isoformat(),
        "dte_at_open": days,
        "category": category(days),
        "status": "open",
        "notes": args.notes or "",
    }
    trades.append(trade)
    verb = "Bought" if args.side == "buy" else "Sold"
    print(f"#{trade['id']} {verb} {args.contracts} {trade['ticker']} {args.exp} {args.strike:g}P "
          f"@ {args.premium:.2f}  [{trade['category']}, {days} DTE]")


def cmd_close(args, trades):
    t = next((t for t in trades if t["id"] == args.id), None)
    if not t:
        sys.exit(f"No trade #{args.id}")
    if t["status"] != "open":
        sys.exit(f"Trade #{args.id} is already {t['status']}")
    if args.expired:
        price, status = 0.0, "expired"
    elif args.assigned:
        # Put exercised at the strike: closed here at its intrinsic value at expiration.
        if args.premium is None:
            sys.exit("--assigned needs --premium set to the put's intrinsic value "
                     "(strike minus stock price at assignment)")
        price, status = args.premium, "assigned"
    elif args.premium is not None:
        price, status = args.premium, "closed"
    else:
        sys.exit("Give --premium PRICE, --expired, or --assigned --premium INTRINSIC")
    t.update(status=status, exit_premium=price, exit_fees=args.fees,
             closed=(args.date or date.today().isoformat()),
             realized=round(pnl(t, price, args.fees), 2))
    print(f"#{t['id']} {status}: realized P&L {money(t['realized'], True)}")


def money(x, cents=False):
    if x is None:
        return "-"
    s = f"${abs(x):,.2f}" if cents else f"${abs(x):,.0f}"
    return "-" + s if x < 0 else s


def cmd_list(args, trades):
    rows = [t for t in trades
            if (args.status == "all") or (args.status == "open") == (t["status"] == "open")]
    if args.category:
        rows = [t for t in rows if t["category"].lower() == args.category.lower()]
    if not rows:
        print("No trades.")
        return
    for cat in ("Hop", "Skip", "Leap"):
        group = [t for t in rows if t["category"] == cat]
        if not group:
            continue
        print(f"\n=== {cat.upper()} ===")
        for t in group:
            print(describe(t, live=args.live and t["status"] == "open"))


def describe(t, live=False):
    s = stats(t)
    side = "LONG " if t["side"] == "buy" else "SHORT"
    head = (f"#{t['id']:<3} {side} {t['contracts']}x {t['ticker']:<5} {t['strike']:g}P {t['exp']}  "
            f"entry {t['premium']:.2f}  BE {s['breakeven']:.2f}")
    lines = [head]
    if t["status"] != "open":
        lines.append(f"      {t['status']} {t['closed']} @ {t['exit_premium']:.2f}  "
                     f"realized {money(t['realized'], True)}")
        return "\n".join(lines)
    days = dte(t["exp"])
    info = f"      {days} DTE  max profit {money(s['max_profit'])}  max loss {money(s['max_loss'])}"
    if t["side"] == "sell":
        info += f"  collateral {money(s['collateral'])}  annualized {s['annualized']:.1%}"
    lines.append(info)
    if days <= 7:
        lines.append("      ! expires within a week")
    if live:
        try:
            q = QUOTE(t["ticker"], t["exp"], t["strike"])
        except QuoteError as e:
            lines.append(f"      (no live quote: {e})")
            return "\n".join(lines)
        delta = put_delta(q["spot"], t["strike"], days, q["iv"])
        pos_delta = None if delta is None else delta * sign(t) * MULTIPLIER * t["contracts"]
        upnl = pnl(t, q["mark"])
        cost = t["premium"] * MULTIPLIER * t["contracts"]
        pct = upnl / cost if cost else 0
        lines.append(f"      stock {q['spot']:.2f}  bid/ask {q['bid']:.2f}/{q['ask']:.2f}  "
                     f"mark {q['mark']:.2f}  IV {q['iv']:.0%}"
                     + (f"  delta {delta:+.2f} (position {pos_delta:+.0f} sh)" if delta is not None else ""))
        lines.append(f"      unrealized P&L {money(upnl, True)} ({pct:+.0%})")
        if t["side"] == "sell":
            if q["spot"] < t["strike"]:
                lines.append("      ! IN THE MONEY: assignment risk")
            elif q["spot"] < t["strike"] * (1 + ASSIGNMENT_WARN_PCT):
                lines.append("      ! stock within 2% of strike")
    return "\n".join(lines)


def cmd_report(args, trades):
    closed = [t for t in trades if t["status"] != "open"]
    open_ = [t for t in trades if t["status"] == "open"]
    print(f"Open: {len(open_)}   Closed: {len(closed)}")
    short_collateral = sum(stats(t)["collateral"] for t in open_ if t["side"] == "sell")
    long_cost = sum(stats(t)["max_loss"] for t in open_ if t["side"] == "buy")
    print(f"Cash securing short puts: {money(short_collateral)}   Paid for open long puts: {money(long_cost)}")
    print(f"\n{'Group':<16}{'Trades':>7}{'Wins':>6}{'Win %':>7}{'Realized P&L':>15}")
    groups = [(c, lambda t, c=c: t["category"] == c) for c in ("Hop", "Skip", "Leap")]
    groups += [("Bought puts", lambda t: t["side"] == "buy"), ("Sold puts", lambda t: t["side"] == "sell")]
    for tk in sorted({t["ticker"] for t in closed}):
        groups.append((tk, lambda t, tk=tk: t["ticker"] == tk))
    groups.append(("TOTAL", lambda t: True))
    for name, f in groups:
        g = [t for t in closed if f(t)]
        if not g:
            continue
        wins = sum(1 for t in g if t["realized"] > 0)
        total = sum(t["realized"] for t in g)
        print(f"{name:<16}{len(g):>7}{wins:>6}{wins / len(g):>7.0%}{money(total, True):>15}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Track bought and sold puts by Hop / Skip / Leap.")
    p.add_argument("--db", default=DEFAULT_DB, help="trades file (default: trades.json next to this script)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="log a new put trade")
    a.add_argument("ticker")
    a.add_argument("--side", choices=["buy", "sell"], required=True, help="buy = long put, sell = short put")
    a.add_argument("--strike", type=float, required=True)
    a.add_argument("--exp", required=True, help="expiration YYYY-MM-DD")
    a.add_argument("--premium", type=float, required=True, help="price per share, e.g. 3.20")
    a.add_argument("--contracts", type=int, default=1)
    a.add_argument("--fees", type=float, default=0.0, help="total opening fees in dollars")
    a.add_argument("--date", help="open date YYYY-MM-DD (default today)")
    a.add_argument("--notes")

    c = sub.add_parser("close", help="close, expire, or record assignment of a trade")
    c.add_argument("id", type=int)
    c.add_argument("--premium", type=float, help="exit price per share")
    c.add_argument("--expired", action="store_true", help="expired worthless")
    c.add_argument("--assigned", action="store_true",
                   help="exercised/assigned; --premium = intrinsic value (strike - stock price)")
    c.add_argument("--fees", type=float, default=0.0)
    c.add_argument("--date", help="close date YYYY-MM-DD (default today)")

    l = sub.add_parser("list", help="show trades")
    l.add_argument("--status", choices=["open", "closed", "all"], default="open")
    l.add_argument("--category", choices=["hop", "skip", "leap"])
    l.add_argument("--live", action="store_true", help="pull live prices, P&L, delta, assignment risk")

    sub.add_parser("report", help="win rate and realized P&L by Hop/Skip/Leap, side, and ticker")

    args = p.parse_args(argv)
    trades = load(args.db)
    {"add": cmd_add, "close": cmd_close, "list": cmd_list, "report": cmd_report}[args.cmd](args, trades)
    if args.cmd in ("add", "close"):
        save(args.db, trades)


if __name__ == "__main__":
    main()
