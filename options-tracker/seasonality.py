"""Market seasonality: how a ticker has done in each calendar month, year by year.

Built the same way as TradingView's "Seasonals" view: each month's return is the change from the prior
month's last close to that month's last close (unadjusted closes, so dividends are not added back).
Prices come from Yahoo daily history. Nothing here is a forecast; it only describes past years.
"""
import datetime as dt
import statistics

from options_data import QuoteError, _cached, _yf, yahoo_symbol

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MAX_YEARS = 40
CACHE_TTL = 6 * 3600


def _symbol(ticker):
    t = (ticker or "").upper().strip()
    if not t:
        raise QuoteError("Enter a ticker")
    return t if ("^" in t or "=" in t) else yahoo_symbol(t)   # ^GSPC, NQ=F and the like pass straight through


def _month_closes(sym):
    """{(year, month): last close of that month} plus the date of the newest bar."""
    yf = _yf()
    try:
        h = yf.Ticker(sym).history(period="max", interval="1d", auto_adjust=False)
    except Exception as e:
        raise QuoteError(f"{sym}: no price history ({e})")
    last, newest = {}, None
    for d, c in zip(h.index, h["Close"]):
        if c == c and c > 0:
            last[(d.year, d.month)] = float(c)
            newest = d
    if len(last) < 14:
        raise QuoteError(f"{sym}: not enough price history for seasonality")
    return last, newest.strftime("%Y-%m-%d")


def _stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"avg": None, "median": None, "win": None, "n": 0, "best": None, "worst": None}
    return {"avg": round(statistics.fmean(vals), 2), "median": round(statistics.median(vals), 2),
            "win": round(100 * sum(v > 0 for v in vals) / len(vals)), "n": len(vals),
            "best": round(max(vals), 2), "worst": round(min(vals), 2)}


def seasonality(ticker, years=10):
    sym = _symbol(ticker)
    try:
        years = max(3, min(MAX_YEARS, int(years)))
    except (TypeError, ValueError):
        years = 10
    closes, asof = _cached(("season", sym), lambda: _month_closes(sym), ttl=CACHE_TTL)
    today = dt.date.today()
    first_year = min(y for y, _ in closes)

    def ret(y, m):
        py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
        a, b = closes.get((py, pm)), closes.get((y, m))
        return round((b / a - 1) * 100, 2) if a and b else None

    rows = []
    for y in range(today.year, max(first_year, today.year - years) - 1, -1):
        months = [ret(y, m) for m in range(1, 13)]
        a, b = closes.get((y - 1, 12)), next((closes[(y, m)] for m in range(12, 0, -1) if (y, m) in closes), None)
        rows.append({"year": y, "months": months, "total": round((b / a - 1) * 100, 2) if a and b else None,
                     "partial": y == today.year})
    rows = [r for r in rows if any(v is not None for v in r["months"])]
    # Averages use completed months only: this year's unfinished month would skew its column.
    done = lambda r, i: r["months"][i] if not (r["partial"] and i + 1 >= today.month) else None
    by_month = [_stats([done(r, i) for r in rows]) for i in range(12)]
    full = [r["total"] for r in rows if not r["partial"]]
    ranked = sorted((i for i in range(12) if by_month[i]["avg"] is not None), key=lambda i: by_month[i]["avg"])
    return {"ticker": ticker.upper().strip(), "symbol": sym, "asof": asof, "years": years,
            "first_year": rows[-1]["year"] if rows else None, "months": MONTHS, "rows": rows,
            "by_month": by_month, "year_stats": _stats(full), "this_month": today.month,
            "next_month": today.month % 12 + 1,
            "best_month": ranked[-1] + 1 if ranked else None, "worst_month": ranked[0] + 1 if ranked else None}
