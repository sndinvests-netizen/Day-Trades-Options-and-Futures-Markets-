#!/usr/bin/env python3
"""Sunday Globex open (3:00 PM Pacific) for MES and MNQ: the first hourly candle's range and any weekend gap.

Prints JSON. The gap compares the Sunday open with Friday's last price (the 2:00 PM Pacific close).
A gap is "full" when the open is outside Friday's whole high-low range.
  python3 levels.py                 # the most recent Sunday open
  python3 levels.py --date 2026-10-04
  python3 levels.py --text          # a one-line text message instead of JSON
"""
import argparse, json, sys
from datetime import date, datetime, timedelta
import yfinance as yf

SYMBOLS = {"MES": "MES=F", "MNQ": "MNQ=F"}


def last_sunday(today):
    return today - timedelta(days=(today.weekday() + 1) % 7)


def levels(name, yahoo, sunday):
    h = yf.Ticker(yahoo).history(start=sunday - timedelta(days=4), end=sunday + timedelta(days=1),
                                 interval="1h", prepost=True)
    if h.empty:
        raise RuntimeError(f"{name}: no data from Yahoo")
    h.index = h.index.tz_convert("America/Los_Angeles")
    first = h[(h.index.date == sunday) & (h.index.hour == 15)]
    if first.empty:
        raise RuntimeError(f"{name}: no 3:00 PM candle on {sunday} yet")
    bar = first.iloc[0]
    closed = datetime.now(first.index[0].tz) >= first.index[0] + timedelta(hours=1)
    friday = sunday - timedelta(days=2)
    fri = h[h.index.date == friday]
    if fri.empty:
        raise RuntimeError(f"{name}: no Friday {friday} data")
    fri_close, fri_high, fri_low = float(fri.Close.iloc[-1]), float(fri.High.max()), float(fri.Low.min())
    gap = float(bar.Open) - fri_close
    kind = "none" if abs(gap) < 0.25 else ("up" if gap > 0 else "down")
    full = (kind == "up" and bar.Open > fri_high) or (kind == "down" and bar.Open < fri_low)
    filled = (kind == "up" and bar.Low <= fri_close) or (kind == "down" and bar.High >= fri_close)
    return {
        "symbol": name, "tradingview": f"CME_MINI:{name}1!", "sunday": str(sunday), "candle_closed": bool(closed),
        "or_high": float(bar.High), "or_low": float(bar.Low), "open": float(bar.Open), "close": float(bar.Close),
        "or_size": round(float(bar.High - bar.Low), 2),
        "fri_close": fri_close, "fri_high": fri_high, "fri_low": fri_low,
        "gap": round(gap, 2), "gap_pct": round(gap / fri_close * 100, 2), "gap_kind": kind,
        "full_gap": bool(full), "gap_filled_in_first_hour": bool(filled),
        "start_ts": int(first.index[0].timestamp()), "fri_close_ts": int(fri.index[-1].timestamp()),
    }


def p(x):  # 31061.75 -> "31061.75", 7783.0 -> "7783"
    return ("%.2f" % x).rstrip("0").rstrip(".")


def as_text(out):
    parts = []
    for l in out:
        if l["gap_kind"] == "none":
            gap = "no gap"
        else:
            gap = "gap %s %s%s from Fri %s, %s" % (l["gap_kind"], "+" if l["gap"] > 0 else "-", p(abs(l["gap"])), p(l["fri_close"]),
                                                 "filled" if l["gap_filled_in_first_hour"] else "not filled")
        parts.append("%s 1st hr %s-%s (%s pts), %s." % (l["symbol"], p(l["or_low"]), p(l["or_high"]), p(l["or_size"]), gap))
    return "Sunday open: " + " ".join(parts)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="the Sunday, YYYY-MM-DD (default: most recent Sunday)")
    p.add_argument("--text", action="store_true", help="print a one-line text message instead of JSON")
    a = p.parse_args()
    sunday = date.fromisoformat(a.date) if a.date else last_sunday(date.today())
    out, errors = [], []
    for name, yahoo in SYMBOLS.items():
        try:
            out.append(levels(name, yahoo, sunday))
        except Exception as e:
            errors.append(str(e))
    print(as_text(out) if a.text else json.dumps({"levels": out, "errors": errors}, indent=1))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
