"""Open check on the daily chart (same 50-day SMA TradingView plots on 1D): is each major index accumulating above its 50-day moving average or distributing below it?
Prints one text-ready summary. Run by run.sh at the 6:30 AM PT open, Monday to Friday."""
import datetime as dt, sys
import yfinance as yf

NAMES = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "DIA": "Dow", "IWM": "Russell 2000"}
# NYSE full-day closures; the job stays quiet on these.
HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
            "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
today = dt.date.today()
if today.isoformat() in HOLIDAYS or today.weekday() > 4:
    sys.exit(3)

d = yf.download(list(NAMES), period="9mo", interval="1d", auto_adjust=False, progress=False)
close, vol = d["Close"], d["Volume"]
past = close.index.date < today          # 50-day and distribution counts use completed sessions only
close, vol = close[past].dropna(), vol[past].dropna()

lines, score = [], 0
for t, name in NAMES.items():
    m = yf.download(t, period="1d", interval="1m", prepost=True, auto_adjust=False, progress=False)["Close"].dropna()
    px = float(m.iloc[-1].item() if hasattr(m.iloc[-1], "item") else m.iloc[-1]) if len(m) else float(close[t].iloc[-1])
    ma = close[t].rolling(50).mean()
    ma50, ma50_wk = float(ma.iloc[-1]), float(ma.iloc[-6])
    pct, rising = (px / ma50 - 1) * 100, ma50 > ma50_wk
    # O'Neil-style count over the last 25 sessions: down/up 0.2%+ on higher volume than the day before.
    chg, vup = close[t].pct_change().tail(25), (vol[t].diff() > 0).tail(25)
    dist, acc = int(((chg <= -0.002) & vup).sum()), int(((chg >= 0.002) & vup).sum())
    if px > ma50 and rising:
        verdict = "ACCUMULATING"; score += 1
    elif px < ma50 and not rising:
        verdict = "DISTRIBUTING"; score -= 1
    else:
        verdict = "MIXED (%s 50-day, 50-day %s)" % ("above" if px > ma50 else "below", "rising" if rising else "falling")
    lines.append("%s %s: %.2f, %+.1f%% vs 50-day %.2f. %s. 25-day: %d dist / %d acc days" % (t, name, px, pct, ma50, verdict, dist, acc))

overall = {4: "all four ACCUMULATING above the 50-day", -4: "all four DISTRIBUTING below the 50-day"}.get(
    score, "mixed: %d accumulating, %d distributing" % (sum("ACCUM" in l for l in lines), sum("DISTRIB" in l for l in lines)))
print("50-day open check, daily chart, %s: %s.\n%s" % (today.strftime("%a %b %d"), overall, "\n".join(lines)))
