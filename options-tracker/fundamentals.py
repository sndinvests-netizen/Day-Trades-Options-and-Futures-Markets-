"""Company fundamentals for one ticker: what it does, how it's valued, how profitable and how indebted it is,
its dividend, what analysts expect, and a few years of revenue, profit and cash flow.

Data is Yahoo's (through yfinance). Nothing here is a rating or advice; the "things to notice" list only
points at numbers that are unusual on their face.
"""
import datetime as dt

from options_data import QuoteError, _cached, _yf, yahoo_symbol

EXCHANGES = {"NMS": "nasdaq", "NGM": "nasdaq", "NCM": "nasdaq", "NYQ": "nyse", "ASE": "amex", "PCX": "arca", "BTS": "bats"}


def _f(x):
    try:
        x = float(x)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _date(ts):
    try:
        return dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def _years(tk):
    """Last four fiscal years: revenue, net income, operating and free cash flow."""
    rows = {}
    try:
        inc, cf = tk.income_stmt, tk.cashflow
    except Exception:
        return []

    def grab(df, names, key):
        if df is None or df.empty:
            return
        for n in names:
            if n in df.index:
                for col, v in df.loc[n].items():
                    v = _f(v)
                    if v is not None:
                        rows.setdefault(str(col)[:4], {})[key] = v
                return
    grab(inc, ["Total Revenue", "Operating Revenue"], "revenue")
    grab(inc, ["Net Income", "Net Income Common Stockholders"], "net_income")
    grab(inc, ["Diluted EPS", "Basic EPS"], "eps")
    grab(cf, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"], "op_cash")
    grab(cf, ["Free Cash Flow"], "fcf")
    grab(cf, ["Capital Expenditure"], "capex")
    for r in rows.values():       # Yahoo sometimes repeats operating cash as "free cash flow" when capex is missing
        if r.get("capex") is None and r.get("fcf") is not None and r.get("fcf") == r.get("op_cash"):
            r["fcf"] = None
    return [dict(year=y, **rows[y]) for y in sorted(rows)[-4:]]


def _notes(d):
    """Plain observations about numbers that stand out. Not a rating."""
    n = []
    v, p, h, dv, a = d["valuation"], d["profit"], d["health"], d["dividend"], d["analysts"]
    if v["pe"] and v["forward_pe"]:
        if v["forward_pe"] < v["pe"] * 0.85:
            n.append(f"Forward P/E ({v['forward_pe']:.1f}) is well below trailing P/E ({v['pe']:.1f}): analysts expect earnings to grow.")
        elif v["forward_pe"] > v["pe"] * 1.15:
            n.append(f"Forward P/E ({v['forward_pe']:.1f}) is above trailing P/E ({v['pe']:.1f}): analysts expect earnings to fall.")
    if v["pe"] is None and d["eps"] is not None and d["eps"] <= 0:
        n.append("The company lost money over the last 12 months, so there is no P/E.")
    if h["fcf"] is not None and h["fcf"] < 0:
        n.append("Free cash flow is negative: it is spending more on operations and investment than it brings in.")
    if h["debt_to_equity"] is not None and h["debt_to_equity"] > 150:
        n.append(f"Debt is {h['debt_to_equity'] / 100:.1f} times equity, which is high outside of utilities, banks and real estate.")
    if h["current_ratio"] is not None and h["current_ratio"] < 1:
        n.append(f"Current ratio is {h['current_ratio']:.2f}: short-term bills exceed short-term assets.")
    if dv["payout_pct"] is not None and dv["payout_pct"] > 80:
        n.append(f"The dividend takes {dv['payout_pct']:.0f}% of earnings, which leaves little cushion.")
    if dv["yield_pct"] and dv["avg5y_yield_pct"]:
        if dv["yield_pct"] > dv["avg5y_yield_pct"] * 1.2:
            n.append(f"Dividend yield ({dv['yield_pct']:.2f}%) is above its 5-year average ({dv['avg5y_yield_pct']:.2f}%).")
        elif dv["yield_pct"] < dv["avg5y_yield_pct"] * 0.8:
            n.append(f"Dividend yield ({dv['yield_pct']:.2f}%) is below its 5-year average ({dv['avg5y_yield_pct']:.2f}%).")
    if a["upside_pct"] is not None and a["count"]:
        n.append(f"The average analyst target ({a['target_mean']:.2f}) is {abs(a['upside_pct']):.0f}% {'above' if a['upside_pct'] >= 0 else 'below'} the price ({int(a['count'])} analysts).")
    if p["revenue_growth_pct"] is not None and p["revenue_growth_pct"] < 0:
        n.append(f"Revenue is down {abs(p['revenue_growth_pct']):.1f}% from a year ago.")
    if d["short_pct_float"] is not None and d["short_pct_float"] > 10:
        n.append(f"{d['short_pct_float']:.1f}% of the float is sold short.")
    if d["range_pos"] is not None and (d["range_pos"] >= 90 or d["range_pos"] <= 10):
        n.append(f"The price is {'near the top' if d['range_pos'] >= 90 else 'near the bottom'} of its 52-week range.")
    return n


def fundamentals(ticker):
    t = (ticker or "").upper().strip()
    if not t:
        raise QuoteError("Enter a ticker")
    sym = yahoo_symbol(t)
    yf = _yf()

    def fetch():
        tk = yf.Ticker(sym)
        try:
            i = tk.info or {}
        except Exception as e:
            raise QuoteError(f"{t}: no company data ({e})")
        kind = (i.get("quoteType") or "").upper()
        if kind in ("ETF", "MUTUALFUND"):
            raise QuoteError(f"{t} is a fund, not a company. Use the ETF Profile tab for it.")
        if kind != "EQUITY" or not (i.get("longName") or i.get("shortName")):
            raise QuoteError(f"{t}: no company fundamentals found for that ticker")
        g = lambda k: _f(i.get(k))
        pct = lambda k: None if g(k) is None else round(g(k) * 100, 2)
        price, lo, hi = g("regularMarketPrice") or g("currentPrice"), g("fiftyTwoWeekLow"), g("fiftyTwoWeekHigh")
        prev = g("previousClose")
        tmean = g("targetMeanPrice")
        nxt = None
        try:
            ed = (tk.calendar or {}).get("Earnings Date") or []
            fut = [e for e in ed if e >= dt.date.today()]
            nxt = str(fut[0]) if fut else None
        except Exception:
            pass
        d = {
            "ticker": t, "name": i.get("longName") or i.get("shortName"), "sector": i.get("sector"),
            "industry": i.get("industry"), "summary": i.get("longBusinessSummary"), "employees": g("fullTimeEmployees"),
            "website": i.get("website"), "price": price,
            "change_pct": round((price / prev - 1) * 100, 2) if price and prev else None,
            "market_cap": g("marketCap"), "enterprise_value": g("enterpriseValue"),
            "low52": lo, "high52": hi,
            "range_pos": round((price - lo) / (hi - lo) * 100) if price and lo and hi and hi > lo else None,
            "ma50": g("fiftyDayAverage"), "ma200": g("twoHundredDayAverage"), "beta": g("beta"),
            "eps": g("trailingEps"), "forward_eps": g("forwardEps"), "next_earnings": nxt,
            "short_pct_float": pct("shortPercentOfFloat"), "institutions_pct": pct("heldPercentInstitutions"),
            "insiders_pct": pct("heldPercentInsiders"),
            "valuation": {"pe": g("trailingPE"), "forward_pe": g("forwardPE"), "peg": g("trailingPegRatio"),
                          "ps": g("priceToSalesTrailing12Months"), "pb": g("priceToBook"),
                          "ev_ebitda": g("enterpriseToEbitda"), "ev_revenue": g("enterpriseToRevenue")},
            "profit": {"gross_margin_pct": pct("grossMargins"), "operating_margin_pct": pct("operatingMargins"),
                       "net_margin_pct": pct("profitMargins"), "roe_pct": pct("returnOnEquity"),
                       "roa_pct": pct("returnOnAssets"), "revenue_growth_pct": pct("revenueGrowth"),
                       "earnings_growth_pct": pct("earningsGrowth"), "revenue": g("totalRevenue"), "ebitda": g("ebitda")},
            "health": {"debt": g("totalDebt"), "cash": g("totalCash"), "debt_to_equity": g("debtToEquity"),
                       "current_ratio": g("currentRatio"), "op_cash": g("operatingCashflow"), "fcf": g("freeCashflow")},
            "dividend": {"rate": g("dividendRate"), "yield_pct": g("dividendYield"),
                         "payout_pct": pct("payoutRatio"), "avg5y_yield_pct": g("fiveYearAvgDividendYield"),
                         "ex_date": _date(i.get("exDividendDate"))},
            "analysts": {"count": g("numberOfAnalystOpinions"), "rating": i.get("recommendationKey"),
                         "score": g("recommendationMean"), "target_mean": tmean, "target_median": g("targetMedianPrice"),
                         "target_low": g("targetLowPrice"), "target_high": g("targetHighPrice"),
                         "upside_pct": round((tmean / price - 1) * 100, 1) if tmean and price else None},
            "years": _years(tk),
        }
        ex = EXCHANGES.get(i.get("exchange") or "")
        d["alphaspread"] = f"https://www.alphaspread.com/security/{ex}/{t.lower().replace('.', '-')}/summary" if ex else None
        d["notes"] = _notes(d)
        return d
    return _cached(("fundamentals", sym), fetch, ttl=30 * 60)
