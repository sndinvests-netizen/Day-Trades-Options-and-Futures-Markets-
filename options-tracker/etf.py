"""ETF profile: what a fund is, what it costs, what it holds and how it has done.

Data is Yahoo's fund data (through yfinance). Leveraged and inverse funds are flagged from their name,
because their daily reset makes them behave differently from the stock or index they track when held.
"""
import datetime as dt
import re

from options_data import QuoteError, _cached, _yf, yahoo_symbol

SECTORS = {"technology": "Technology", "communication_services": "Communication services",
           "consumer_cyclical": "Consumer cyclical", "consumer_defensive": "Consumer defensive",
           "financial_services": "Financials", "healthcare": "Healthcare", "industrials": "Industrials",
           "energy": "Energy", "utilities": "Utilities", "basic_materials": "Materials", "realestate": "Real estate"}
ASSETS = {"stockPosition": "Stocks", "bondPosition": "Bonds", "cashPosition": "Cash",
          "preferredPosition": "Preferred", "convertiblePosition": "Convertibles",
          "otherPosition": "Other (swaps, futures and the like)"}


def _f(x):
    try:
        x = float(x)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _leverage(name):
    """(factor, direction) read from the fund's name, e.g. 'Daily TSLA Bull 2X Shares' -> (2.0, 'long')."""
    n = name or ""
    m = re.search(r"(-?\d(?:\.\d+)?)\s*[xX]\b", n)
    factor = abs(float(m.group(1))) if m else None
    inverse = bool(re.search(r"\b(bear|inverse|short)\b", n, re.I)) or bool(m and m.group(1).startswith("-"))
    ultra = re.search(r"\bultra(pro)?\b", n, re.I)
    if factor is None and ultra:
        factor = 3.0 if ultra.group(1) else 2.0
    if factor is None and inverse:
        factor = 1.0
    if factor is None or (factor == 1.0 and not inverse):
        return None
    return {"factor": factor, "direction": "inverse" if inverse else "long"}


def _returns(tk):
    try:
        h = tk.history(period="1y", interval="1d", auto_adjust=True)["Close"].dropna()
    except Exception:
        return {}
    if len(h) < 30:
        return {}
    last, out = float(h.iloc[-1]), {}
    for label, days in (("1 week", 5), ("1 month", 21), ("3 months", 63), ("6 months", 126), ("1 year", len(h) - 1)):
        if len(h) > days:
            out[label] = round((last / float(h.iloc[-1 - days]) - 1) * 100, 1)
    return out


def profile(ticker):
    t = (ticker or "").upper().strip()
    if not t:
        raise QuoteError("Enter an ETF ticker")
    sym = yahoo_symbol(t)
    yf = _yf()

    def fetch():
        tk = yf.Ticker(sym)
        try:
            info = tk.info or {}
        except Exception as e:
            raise QuoteError(f"{t}: no fund data ({e})")
        kind = (info.get("quoteType") or "").upper()
        if kind not in ("ETF", "MUTUALFUND"):
            what = {"EQUITY": "a stock", "INDEX": "an index", "FUTURE": "a futures contract"}.get(kind, "not a fund")
            raise QuoteError(f"{t} is {what}, not an ETF. Try a fund such as SPY, QQQ or TSLL.")
        holdings, sectors, assets, turnover = [], [], [], None
        try:
            fd = tk.funds_data
            th = fd.top_holdings
            if th is not None and len(th):
                for s, row in th.iterrows():
                    w = _f(row.get("Holding Percent"))
                    if w:
                        holdings.append({"symbol": str(s), "name": str(row.get("Name") or s), "pct": round(w * 100, 2)})
            sectors = sorted(({"name": SECTORS.get(k, k), "pct": round(v * 100, 1)}
                              for k, v in (fd.sector_weightings or {}).items() if _f(v)), key=lambda x: -x["pct"])
            assets = sorted(({"name": ASSETS.get(k, k), "pct": round(v * 100, 1)}
                             for k, v in (fd.asset_classes or {}).items() if _f(v)), key=lambda x: -x["pct"])
            ops = fd.fund_operations
            if ops is not None and sym in ops.columns:
                turnover = _f(ops[sym].get("Annual Holdings Turnover"))
        except Exception:
            pass
        price, nav = _f(info.get("regularMarketPrice")), _f(info.get("navPrice"))
        lo, hi = _f(info.get("fiftyTwoWeekLow")), _f(info.get("fiftyTwoWeekHigh"))
        inc = info.get("fundInceptionDate")
        pct = lambda k: None if _f(info.get(k)) is None else round(_f(info.get(k)) * 100, 2)
        name = info.get("longName") or info.get("shortName") or t
        prev = _f(info.get("previousClose"))
        return {
            "ticker": t, "name": name, "family": info.get("fundFamily"), "category": info.get("category"),
            "kind": "ETF" if kind == "ETF" else "Mutual fund", "summary": info.get("longBusinessSummary"),
            "leverage": _leverage(name), "price": price,
            "change_pct": round((price / prev - 1) * 100, 2) if price and prev else None,
            "nav": nav, "premium_pct": round((price / nav - 1) * 100, 2) if price and nav else None,
            "assets": _f(info.get("totalAssets")), "expense_pct": _f(info.get("netExpenseRatio")),
            "yield_pct": pct("yield"), "beta3y": _f(info.get("beta3Year")), "pe": _f(info.get("trailingPE")),
            "avg_volume": _f(info.get("averageVolume")), "low52": lo, "high52": hi,
            "range_pos": round((price - lo) / (hi - lo) * 100) if price and lo and hi and hi > lo else None,
            "inception": dt.datetime.utcfromtimestamp(inc).strftime("%Y-%m-%d") if isinstance(inc, (int, float)) else None,
            "ytd_pct": _f(info.get("ytdReturn")), "avg3y_pct": pct("threeYearAverageReturn"),
            "avg5y_pct": pct("fiveYearAverageReturn"), "turnover_pct": None if turnover is None else round(turnover * 100),
            "returns": _returns(tk), "holdings": holdings,
            "top10_pct": round(sum(h["pct"] for h in holdings[:10]), 1) if holdings else None,
            "sectors": sectors, "asset_mix": assets,
        }
    return _cached(("etf", sym), fetch, ttl=30 * 60)
