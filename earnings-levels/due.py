"""Which earnings reactions are ready to mark on TradingView.

Tracks the NEXT report for every stock on the option tracker's earnings watchlist
(options-tracker/earnings_watch.json, or its defaults) and lists the ones whose reaction session has
closed but whose earnings high/low hasn't been drawn yet. Only reports found on or after the first run
are tracked; past earnings are never marked.

  due.py                       # print due reactions as JSON (empty list when nothing is due)
  due.py --done TICKER DATE    # record that TICKER's report on DATE has been marked

The reaction session is the report day for a before-open report, and the next trading day for an
after-close report. Unknown report times are treated as after-close, and the marking prompt checks the
volume spike to confirm. State lives in state.json next to this file (gitignored).
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
TRACKER = os.path.join(os.path.dirname(HERE), "options-tracker")
sys.path.insert(0, TRACKER)
import earnings  # noqa: E402  (the tracker's Yahoo earnings lookup and watchlist)

STATE = os.path.join(HERE, "state.json")
ET = ZoneInfo("America/New_York")
# NYSE full-day closures (same list as the tracker's market-status dot); extend for 2028.
HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
            "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}


def trading_day_after(d):
    d = date.fromisoformat(d)
    while True:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in HOLIDAYS:
            return d.isoformat()


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"since": date.today().isoformat(), "reports": {}}


def save(s):
    with open(STATE, "w") as f:
        json.dump(s, f, indent=2)


def refresh(s):
    """Record each watchlist stock's next report (and update its date/time if Yahoo changes it)."""
    for sym in earnings.watchlist():
        try:
            r = earnings._fetch(sym)
        except Exception as e:
            print(f"warn: {sym}: {e}", file=sys.stderr)
            continue
        if not r.get("date") or r["date"] < s["since"]:
            continue
        # a moved date replaces the pending entry for that ticker rather than adding a second one
        for k, v in list(s["reports"].items()):
            if v["ticker"] == sym and not v.get("done") and v["date"] > date.today().isoformat() and v["date"] != r["date"]:
                del s["reports"][k]
        key = f"{sym}|{r['date']}"
        old = s["reports"].get(key, {})
        if old.get("done"):
            continue
        session = r["session"]
        reaction = r["date"] if session == "bmo" else trading_day_after(r["date"])
        s["reports"][key] = {"ticker": sym, "date": r["date"], "session": session, "estimated": r["estimated"],
                             "reaction": reaction, "done": False}


def due(s):
    now = datetime.now(ET)
    today = now.date().isoformat()
    closed = now.hour * 60 + now.minute >= 16 * 60 + 15   # give the close 15 minutes to settle
    return [v for v in s["reports"].values()
            if not v["done"] and (v["reaction"] < today or v["reaction"] == today and closed)]


def main():
    s = load()
    if len(sys.argv) == 4 and sys.argv[1] == "--done":
        key = f"{sys.argv[2].upper()}|{sys.argv[3]}"
        if key in s["reports"]:
            s["reports"][key]["done"] = True
            s["reports"][key]["marked"] = datetime.now(ET).isoformat(timespec="minutes")
            save(s)
            print(f"marked {key}")
        else:
            print(f"unknown {key}", file=sys.stderr)
            sys.exit(1)
        return
    refresh(s)
    save(s)
    print(json.dumps(due(s)))


if __name__ == "__main__":
    main()
