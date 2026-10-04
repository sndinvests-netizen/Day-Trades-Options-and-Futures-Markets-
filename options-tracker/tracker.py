#!/usr/bin/env python3
"""Option tracker: log bought and sold puts and calls, sorted into Hop / Skip / Leap.

Categories are set from days to expiration (DTE) on the day the trade is opened:
  Hop   0-90 days
  Skip  91-360 days   (about six months)
  Leap  over 360 days

Live prices come from Yahoo Finance via yfinance (free, may be delayed ~15 min).
For a browser view of any ticker's chain plus these positions, run app.py.

Usage examples:
  python3 tracker.py add SPY --side sell --strike 550 --exp 2026-12-18 --premium 6.40
  python3 tracker.py add AAPL --type call --side buy --strike 250 --exp 2027-12-17 --premium 14.10 --contracts 2
  python3 tracker.py list --live
  python3 tracker.py close 3 --premium 1.05
  python3 tracker.py close 4 --expired
  python3 tracker.py close 5 --assigned --premium 8
  python3 tracker.py report
"""
import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime

from options_data import QuoteError, greeks, quote, spot_price, years_to_expiry

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "trades.json")
MULTIPLIER = 100
ASSIGNMENT_WARN_PCT = 0.02  # warn when a short option's stock is within 2% of the strike


# ---------- storage ----------

def load(db):
    if not os.path.exists(db):
        return []
    with open(db) as f:
        return json.load(f)


def save(db, trades):
    # Keep the previous version as trades.json.bak, so one bad edit or delete can be undone.
    if os.path.exists(db):
        shutil.copyfile(db, db + ".bak")
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


def kind(trade):
    """'put', 'call' or 'stock'. Trades logged before calls were supported are puts.

    Stock legs only come from multi-leg positions saved in the Builder (covered
    call, collar); their contracts count is lots of 100 shares and they have no exp.
    """
    return trade.get("type", "put")


def label(trade):
    if kind(trade) == "stock":
        return f"{trade['contracts'] * MULTIPLIER} sh"
    return f"{trade['strike']:g}{kind(trade)[0].upper()}"


def sign(trade):
    """+1 for a bought option (profits when its price rises), -1 for a sold one."""
    return 1 if trade["side"] == "buy" else -1


def pnl(trade, exit_price, exit_fees=0.0):
    gross = sign(trade) * (exit_price - trade["premium"]) * MULTIPLIER * trade["contracts"]
    return gross - trade["fees"] - exit_fees


def stats(trade):
    """Static numbers that don't need live data. None means unlimited."""
    n, k, p = trade["contracts"], trade["strike"], trade["premium"]
    if kind(trade) == "stock":
        value = p * MULTIPLIER * n
        if trade["side"] == "buy":
            return {"breakeven": p, "max_loss": value, "max_profit": None}
        return {"breakeven": p, "max_profit": value, "max_loss": None}
    call = kind(trade) == "call"
    out = {"breakeven": k + p if call else k - p}
    if trade["side"] == "buy":
        out["max_loss"] = p * MULTIPLIER * n
        out["max_profit"] = None if call else (k - p) * MULTIPLIER * n
    else:
        out["max_profit"] = p * MULTIPLIER * n
        out["max_loss"] = None if call else (k - p) * MULTIPLIER * n
        if not call:
            out["collateral"] = k * MULTIPLIER * n
        days = max(trade["dte_at_open"], 1)
        out["annualized"] = (p / k) * (365 / days)
    return out


def live(trade):
    """Live quote, delta, unrealized P&L and warnings for an open trade."""
    cost = trade["premium"] * MULTIPLIER * trade["contracts"]
    if kind(trade) == "stock":
        s = spot_price(trade["ticker"])
        upnl = pnl(trade, s)
        return {"spot": s, "bid": s, "ask": s, "last": s, "mark": s, "iv": 0.0, "volume": 0, "oi": 0,
                "delta": 1.0, "position_delta": sign(trade) * MULTIPLIER * trade["contracts"],
                "unrealized": upnl, "unrealized_pct": upnl / cost if cost else 0, "warning": None}
    q = quote(trade["ticker"], trade["exp"], trade["strike"], kind(trade))
    g = greeks(kind(trade), q["spot"], trade["strike"], years_to_expiry(trade["exp"]), q["iv"])
    delta = g["delta"] if g else None
    upnl = pnl(trade, q["mark"])
    warning = None
    if trade["side"] == "sell":
        k, s = trade["strike"], q["spot"]
        itm = s > k if kind(trade) == "call" else s < k
        near = s > k * (1 - ASSIGNMENT_WARN_PCT) if kind(trade) == "call" else s < k * (1 + ASSIGNMENT_WARN_PCT)
        if itm:
            warning = "IN THE MONEY: assignment risk"
        elif near:
            warning = "stock within 2% of strike"
    return dict(q, delta=delta,
                position_delta=None if delta is None else delta * sign(trade) * MULTIPLIER * trade["contracts"],
                unrealized=upnl, unrealized_pct=upnl / cost if cost else 0, warning=warning)


# ---------- commands ----------

class TradeError(Exception):
    pass


def add_trade(trades, ticker, type, side, strike, exp, premium, contracts=1, fees=0.0,
              opened=None, notes=""):
    """Append a new single-option trade and return it. Raises TradeError on bad input."""
    leg = {"type": type, "side": side, "strike": strike, "exp": exp, "premium": premium, "contracts": contracts}
    return add_position(trades, ticker, [leg], fees=fees, opened=opened, notes=notes)[0]


def add_position(trades, ticker, legs, strategy="", fees=0.0, opened=None, notes=""):
    """Append one position (one or more legs) and return its leg records.

    Multi-leg positions share a "group" id and "strategy" name, and are grouped as
    Hop / Skip / Leap by their nearest option expiration. Fees go on the first leg.
    """
    if not legs:
        raise TradeError("Add at least one leg")
    try:
        opened = parse_date(opened) if opened else date.today()
    except ValueError:
        raise TradeError("dates must be YYYY-MM-DD")
    for leg in legs:
        if leg.get("type") not in ("put", "call", "stock") or leg.get("side") not in ("buy", "sell"):
            raise TradeError("each leg needs type put/call/stock and side buy/sell")
        if leg["premium"] is None or leg["premium"] < 0 or not leg.get("contracts") or leg["contracts"] < 1:
            raise TradeError("each leg needs a price of 0 or more and at least 1 contract")
        if leg["type"] != "stock":
            if not leg.get("strike") or leg["strike"] <= 0:
                raise TradeError("strike must be above 0")
            try:
                leg["dte"] = dte(leg["exp"], opened)
            except (TypeError, ValueError):
                raise TradeError("dates must be YYYY-MM-DD")
            if leg["dte"] < 0:
                raise TradeError("Expiration is before the open date.")
    option_days = [leg["dte"] for leg in legs if leg["type"] != "stock"]
    if not option_days:
        raise TradeError("A position needs at least one option leg")
    cat = category(min(option_days))
    group = None
    if len(legs) > 1:
        group = max([t.get("group") or 0 for t in trades], default=0) + 1
    next_id = max([t["id"] for t in trades], default=0) + 1
    out = []
    for i, leg in enumerate(legs):
        stock = leg["type"] == "stock"
        trade = {
            "id": next_id + i,
            "ticker": ticker.upper().strip(),
            "type": leg["type"],
            "side": leg["side"],
            "strike": 0.0 if stock else leg["strike"],
            "exp": None if stock else leg["exp"],
            "contracts": leg["contracts"],
            "premium": leg["premium"],
            "fees": fees if i == 0 else 0.0,
            "opened": opened.isoformat(),
            "dte_at_open": min(option_days) if stock else leg["dte"],
            "category": cat,
            "status": "open",
            "notes": notes or "",
        }
        if group:
            trade.update(group=group, strategy=strategy or "Multi-leg")
        out.append(trade)
    trades.extend(out)
    return out


def delete_position(trades, trade_id):
    """Remove a trade logged by mistake, open or closed, and return what was removed.

    A leg of a multi-leg position takes the whole position with it, so a spread is
    never left half-deleted.
    """
    t = next((t for t in trades if t["id"] == trade_id), None)
    if t is None:
        raise TradeError(f"No trade #{trade_id}")
    removed = [x for x in trades if x.get("group") == t["group"]] if t.get("group") else [t]
    ids = {x["id"] for x in removed}
    trades[:] = [x for x in trades if x["id"] not in ids]
    return removed


def close_position(trades, legs, fees=0.0, closed=None):
    """Close several legs at once: legs = [{"id": .., "premium": exit price}].

    An option leg closed at 0 is recorded as expired. Fees go on the first leg.
    """
    if not legs:
        raise TradeError("No legs to close")
    out = []
    for i, leg in enumerate(legs):
        t = next((t for t in trades if t["id"] == leg.get("id")), None)
        if t is None:
            raise TradeError(f"No trade #{leg.get('id')}")
        price = leg.get("premium")
        if price is None or price < 0:
            raise TradeError(f"Exit price needed for #{t['id']}")
        expired = price == 0 and kind(t) != "stock"
        out.append(close_trade(trades, t["id"], None if expired else price, expired=expired,
                               fees=fees if i == 0 else 0.0, closed=closed))
    return out


def close_trade(trades, trade_id, premium=None, expired=False, assigned=False, fees=0.0, closed=None):
    """Close, expire or assign an open trade and return it. Raises TradeError on bad input."""
    t = next((t for t in trades if t["id"] == trade_id), None)
    if not t:
        raise TradeError(f"No trade #{trade_id}")
    if t["status"] != "open":
        raise TradeError(f"Trade #{trade_id} is already {t['status']}")
    if expired:
        price, status = 0.0, "expired"
    elif assigned:
        # Exercised at the strike: closed here at its intrinsic value at expiration.
        if premium is None:
            raise TradeError("Assigned needs the option's intrinsic value as the exit price "
                             "(put: strike minus stock price; call: stock price minus strike)")
        price, status = premium, "assigned"
    elif premium is not None:
        price, status = premium, "closed"
    else:
        raise TradeError("Give an exit price, expired, or assigned with intrinsic value")
    t.update(status=status, exit_premium=price, exit_fees=fees,
             closed=(closed or date.today().isoformat()),
             realized=round(pnl(t, price, fees), 2))
    return t


def cmd_add(args, trades):
    try:
        trade = add_trade(trades, args.ticker, args.type, args.side, args.strike, args.exp,
                          args.premium, args.contracts, args.fees, args.date, args.notes)
    except TradeError as e:
        sys.exit(str(e))
    verb = "Bought" if args.side == "buy" else "Sold"
    print(f"#{trade['id']} {verb} {args.contracts} {trade['ticker']} {args.exp} {label(trade)} "
          f"@ {args.premium:.2f}  [{trade['category']}, {trade['dte_at_open']} DTE]")


def cmd_delete(args, trades):
    t = next((t for t in trades if t["id"] == args.id), None)
    if t is None:
        sys.exit(f"No trade #{args.id}")
    legs = [x for x in trades if x.get("group") == t["group"]] if t.get("group") else [t]
    for x in legs:
        print(f"  #{x['id']} {x['side']} {x['contracts']} {x['ticker']} {label(x)} {x['exp'] or ''} @ {x['premium']:.2f} ({x['status']})")
    if not args.yes:
        sys.exit(f"Run again with --yes to delete {'these ' + str(len(legs)) + ' legs' if len(legs) > 1 else 'this trade'}.")
    delete_position(trades, args.id)
    print(f"Deleted {len(legs)} leg(s). The previous file is saved as trades.json.bak.")


def cmd_close(args, trades):
    try:
        t = close_trade(trades, args.id, args.premium, args.expired, args.assigned, args.fees, args.date)
    except TradeError as e:
        sys.exit(str(e))
    print(f"#{t['id']} {t['status']}: realized P&L {money(t['realized'], True)}")


def money(x, cents=False):
    if x is None:
        return "unlimited"
    s = f"${abs(x):,.2f}" if cents else f"${abs(x):,.0f}"
    return "-" + s if x < 0 else s


def cmd_list(args, trades):
    rows = [t for t in trades
            if (args.status == "all") or (args.status == "open") == (t["status"] == "open")]
    if args.category:
        rows = [t for t in rows if t["category"].lower() == args.category.lower()]
    if args.type:
        rows = [t for t in rows if kind(t) == args.type]
    if not rows:
        print("No trades.")
        return
    for cat in ("Hop", "Skip", "Leap"):
        group = [t for t in rows if t["category"] == cat]
        if not group:
            continue
        print(f"\n=== {cat.upper()} ===")
        for t in group:
            print(describe(t, show_live=args.live and t["status"] == "open"))


def describe(t, show_live=False):
    s = stats(t)
    side = "LONG " if t["side"] == "buy" else "SHORT"
    head = (f"#{t['id']:<3} {side} {t['contracts']}x {t['ticker']:<5} {label(t)} {t['exp'] or ''}  "
            f"entry {t['premium']:.2f}  BE {s['breakeven']:.2f}")
    if t.get("group"):
        head += f"  [{t['strategy']}, position {t['group']}]"
    lines = [head]
    if t["status"] != "open":
        lines.append(f"      {t['status']} {t['closed']} @ {t['exit_premium']:.2f}  "
                     f"realized {money(t['realized'], True)}")
        return "\n".join(lines)
    days = dte(t["exp"]) if t["exp"] else None
    info = f"      {'-' if days is None else days} DTE  max profit {money(s['max_profit'])}  max loss {money(s['max_loss'])}"
    if "collateral" in s:
        info += f"  collateral {money(s['collateral'])}"
    if "annualized" in s:
        info += f"  annualized {s['annualized']:.1%}"
    lines.append(info)
    if days is not None and days < 0:
        lines.append("      ! past expiration: close it as expired or assigned")
    elif days is not None and days <= 7:
        lines.append("      ! expires within a week")
    if show_live:
        try:
            q = live(t)
        except QuoteError as e:
            lines.append(f"      (no live quote: {e})")
            return "\n".join(lines)
        lines.append(f"      stock {q['spot']:.2f}  bid/ask {q['bid']:.2f}/{q['ask']:.2f}  "
                     f"mark {q['mark']:.2f}  IV {q['iv']:.0%}"
                     + (f"  delta {q['delta']:+.2f} (position {q['position_delta']:+.0f} sh)"
                        if q["delta"] is not None else ""))
        lines.append(f"      unrealized P&L {money(q['unrealized'], True)} ({q['unrealized_pct']:+.0%})")
        if q["warning"]:
            lines.append(f"      ! {q['warning']}")
    return "\n".join(lines)


def positions_of(trades):
    """Group legs into positions: a multi-leg group, or a single trade on its own."""
    out = {}
    for t in trades:
        out.setdefault(("g", t["group"]) if t.get("group") else ("t", t["id"]), []).append(t)
    return list(out.values())


def position_kind(legs):
    if len(legs) > 1:
        return "Multi-leg"
    t = legs[0]
    return f"{'Bought' if t['side'] == 'buy' else 'Sold'} {kind(t)}s"


def report_groups(trades):
    """(name, positions, wins, realized) rows for the report table.

    Counts whole positions: a spread is one win or loss, once all its legs are closed.
    """
    closed = [p for p in positions_of(trades) if all(t["status"] != "open" for t in p)]
    groups = [(c, lambda p, c=c: p[0]["category"] == c) for c in ("Hop", "Skip", "Leap")]
    for name in ("Bought puts", "Sold puts", "Bought calls", "Sold calls", "Multi-leg"):
        groups.append((name, lambda p, name=name: position_kind(p) == name))
    for tk in sorted({p[0]["ticker"] for p in closed}):
        groups.append((tk, lambda p, tk=tk: p[0]["ticker"] == tk))
    groups.append(("TOTAL", lambda p: True))
    out = []
    for name, f in groups:
        g = [sum(t["realized"] for t in p) for p in closed if f(p)]
        if g:
            out.append((name, len(g), sum(1 for r in g if r > 0), sum(g)))
    return out


def open_totals(trades):
    """(cash securing single short puts, paid for open long options)."""
    open_ = [t for t in trades if t["status"] == "open"]
    collateral = sum(stats(t).get("collateral", 0) for t in open_
                     if t["side"] == "sell" and kind(t) == "put" and not t.get("group"))
    long_cost = sum(stats(t)["max_loss"] for t in open_ if t["side"] == "buy" and kind(t) != "stock")
    return collateral, long_cost


def cmd_report(args, trades):
    closed = [t for t in trades if t["status"] != "open"]
    open_ = [t for t in trades if t["status"] == "open"]
    print(f"Open legs: {len(open_)}   Closed legs: {len(closed)}")
    short_collateral, long_cost = open_totals(trades)
    print(f"Cash securing short puts: {money(short_collateral)}   Paid for open long options: {money(long_cost)}")
    print(f"\n{'Group':<16}{'Positions':>10}{'Wins':>6}{'Win %':>7}{'Realized P&L':>15}")
    for name, n, wins, total in report_groups(trades):
        print(f"{name:<16}{n:>10}{wins:>6}{wins / n:>7.0%}{money(total, True):>15}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Track bought and sold options by Hop / Skip / Leap.")
    p.add_argument("--db", default=DEFAULT_DB, help="trades file (default: trades.json next to this script)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="log a new trade")
    a.add_argument("ticker")
    a.add_argument("--type", choices=["put", "call"], default="put", help="default put")
    a.add_argument("--side", choices=["buy", "sell"], required=True, help="buy = long, sell = short")
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
                   help="exercised/assigned; --premium = intrinsic value at assignment")
    c.add_argument("--fees", type=float, default=0.0)
    c.add_argument("--date", help="close date YYYY-MM-DD (default today)")

    l = sub.add_parser("list", help="show trades")
    l.add_argument("--status", choices=["open", "closed", "all"], default="open")
    l.add_argument("--category", choices=["hop", "skip", "leap"])
    l.add_argument("--type", choices=["put", "call"])
    l.add_argument("--live", action="store_true", help="pull live prices, P&L, delta, assignment risk")

    sub.add_parser("report", help="win rate and realized P&L by Hop/Skip/Leap, side, and ticker")

    d = sub.add_parser("delete", help="delete a trade logged by mistake (a multi-leg position goes as a whole)")
    d.add_argument("id", type=int)
    d.add_argument("--yes", action="store_true", help="actually delete (without it, just shows what would go)")

    args = p.parse_args(argv)
    trades = load(args.db)
    {"add": cmd_add, "close": cmd_close, "delete": cmd_delete, "list": cmd_list,
     "report": cmd_report}[args.cmd](args, trades)
    if args.cmd in ("add", "close", "delete"):
        save(args.db, trades)


if __name__ == "__main__":
    main()
