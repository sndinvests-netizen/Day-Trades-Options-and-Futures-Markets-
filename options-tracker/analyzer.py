"""Chart analyzer: reads a ticker's market structure and looks for the patterns in CHART_PATTERNS.md.

For one ticker and timeframe it returns the candles, the swing highs and lows labeled HH / HL / LH / LL,
breaks of structure, support and resistance, and any pattern it can find mechanically (double and triple
tops and bottoms, head and shoulders, triangles, wedges, channels, rectangles, flags and gaps), each with
its trigger, measured target and invalidation level. A pattern is "forming" until a close through its
trigger. Rounded bases, cup and handle and V reversals need an eye on the chart and are not detected.

The three potential paths (bull, base, bear with odds) come from the stock-direction-map script when it
is installed on this computer. Everything here is a read of past price action, not a forecast or advice.
"""
import json
import os
import subprocess
import time

from options_data import QuoteError, _cached, _yf, yahoo_symbol

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "analyzer_cache")          # chart PNGs and path JSON (kept out of git)
MAP_DIR = os.path.expanduser("~/.claude/skills/stock-direction-map")
MAP_PY = os.path.join(MAP_DIR, "scripts", "map.py")
MAP_PYTHON = os.path.join(MAP_DIR, ".venv", "bin", "python")
TIMEFRAMES = {"15m": ("10d", "15m", "15-minute"), "1h": ("60d", "1h", "hourly"),
              "1d": ("1y", "1d", "daily"), "1wk": ("5y", "1wk", "weekly")}
SHOW = 150            # bars sent to the page
PIVOT_W = 3           # a swing high/low is the extreme of this many bars on each side
PATHS_TTL = 20 * 60


def _symbol(ticker):
    t = (ticker or "").upper().strip()
    if not t:
        raise QuoteError("Enter a ticker")
    return t if ("^" in t or "=" in t) else yahoo_symbol(t)


def _bars(sym, tf):
    period, interval, _ = TIMEFRAMES[tf]
    yf = _yf()

    def fetch():
        try:
            h = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=False)
        except Exception as e:
            raise QuoteError(f"{sym}: no price history ({e})")
        out = []
        for d, o, hi, lo, c, v in zip(h.index, h["Open"], h["High"], h["Low"], h["Close"], h["Volume"]):
            if c == c and c > 0 and hi == hi and lo == lo:
                o = o if o == o and o > 0 else c
                out.append({"t": d.strftime("%Y-%m-%d %H:%M") if interval in ("15m", "1h") else d.strftime("%Y-%m-%d"),
                            "o": float(o), "h": float(hi), "l": float(lo), "c": float(c), "v": float(v) if v == v else 0.0})
        if len(out) < 40:
            raise QuoteError(f"{sym}: not enough price history on this timeframe")
        return out
    return _cached(("analyzer", sym, tf), fetch, ttl=120 if interval in ("15m", "1h") else 600)


def _atr(b, n=14):
    trs = [max(x["h"] - x["l"], abs(x["h"] - p["c"]), abs(x["l"] - p["c"])) for p, x in zip(b, b[1:])]
    trs = trs[-n:]
    return sum(trs) / len(trs) if trs else 0.0


def _stoch_rsi(closes, rsi_n=14, stoch_n=14, k_n=3, d_n=3):
    """Stochastic RSI (14, 14, 3, 3), the TradingView default: %K and %D lists aligned to closes, None while warming up."""
    n = len(closes)
    rsi = [None] * n
    up = dn = 0.0
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if i <= rsi_n:
            up, dn = up + g / rsi_n, dn + l / rsi_n
        else:
            up, dn = (up * (rsi_n - 1) + g) / rsi_n, (dn * (rsi_n - 1) + l) / rsi_n
        if i >= rsi_n:
            rsi[i] = 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)
    st = [None] * n
    for i in range(n):
        win = rsi[i - stoch_n + 1:i + 1] if i >= stoch_n - 1 else []
        if len(win) == stoch_n and None not in win:
            lo, hi = min(win), max(win)
            st[i] = 50.0 if hi == lo else (rsi[i] - lo) / (hi - lo) * 100

    def sma(xs, m):
        out = [None] * n
        for i in range(n):
            win = xs[i - m + 1:i + 1] if i >= m - 1 else []
            if len(win) == m and None not in win:
                out[i] = sum(win) / m
        return out
    k = sma(st, k_n)
    return k, sma(k, d_n)


def _stoch_read(k, d):
    """Where %K sits and the latest %K / %D cross within the last 3 bars."""
    if not k or k[-1] is None or d[-1] is None:
        return None
    zone = "overbought" if k[-1] >= 80 else "oversold" if k[-1] <= 20 else "mid-range"
    cross = None
    for j in range(len(k) - 1, max(len(k) - 4, 0), -1):
        if None in (k[j], d[j], k[j - 1], d[j - 1]):
            break
        if k[j - 1] <= d[j - 1] and k[j] > d[j]:
            cross = {"dir": "up", "bars_ago": len(k) - 1 - j, "from": "oversold" if min(k[j - 1], d[j - 1]) <= 20 else "overbought" if max(k[j - 1], d[j - 1]) >= 80 else "mid-range"}
            break
        if k[j - 1] >= d[j - 1] and k[j] < d[j]:
            cross = {"dir": "down", "bars_ago": len(k) - 1 - j, "from": "overbought" if max(k[j - 1], d[j - 1]) >= 80 else "oversold" if min(k[j - 1], d[j - 1]) <= 20 else "mid-range"}
            break
    return {"k": round(k[-1], 1), "d": round(d[-1], 1), "zone": zone, "cross": cross}


def _pivots(b, w=PIVOT_W):
    """Alternating swing highs and lows: [(index, 'H'|'L', price)]."""
    raw = []
    for i in range(w, len(b) - w):
        win = b[i - w:i + w + 1]
        if b[i]["h"] == max(x["h"] for x in win):
            raw.append((i, "H", b[i]["h"]))
        if b[i]["l"] == min(x["l"] for x in win):
            raw.append((i, "L", b[i]["l"]))
    raw.sort()
    zz = []
    for p in raw:
        if zz and zz[-1][1] == p[1]:          # two highs (or lows) in a row: keep the more extreme one
            if (p[1] == "H" and p[2] >= zz[-1][2]) or (p[1] == "L" and p[2] <= zz[-1][2]):
                zz[-1] = p
        else:
            zz.append(p)
    return zz


def _label(zz):
    out, last = [], {"H": None, "L": None}
    for i, kind, price in zz:
        prev = last[kind]
        tag = kind if prev is None else (("HH" if price > prev else "LH") if kind == "H" else ("HL" if price > prev else "LL"))
        out.append({"i": i, "kind": kind, "price": round(price, 4), "label": tag})
        last[kind] = price
    return out


def _structure(b, zz, w=PIVOT_W):
    """Trend state and the breaks of structure, walking the bars in order.

    A close through the last confirmed swing high while the trend is up is a break of structure (BOS, trend
    continues); the same close while the trend is down is a change of character (CHoCH, trend may be turning).
    """
    events, state = [], None
    sh = sl = None                      # last confirmed, unbroken swing high / low: (index, price)
    k = 0
    for i in range(len(b)):
        while k < len(zz) and zz[k][0] + w <= i:      # a swing is only known w bars after it prints
            if zz[k][1] == "H":
                sh = (zz[k][0], zz[k][2])
            else:
                sl = (zz[k][0], zz[k][2])
            k += 1
        c = b[i]["c"]
        if sh and c > sh[1]:
            events.append({"i": i, "from": sh[0], "price": round(sh[1], 4), "dir": "up",
                           "type": "BOS" if state == "up" else "CHoCH"})
            state, sh = "up", None
        elif sl and c < sl[1]:
            events.append({"i": i, "from": sl[0], "price": round(sl[1], 4), "dir": "down",
                           "type": "BOS" if state == "down" else "CHoCH"})
            state, sl = "down", None
    highs = [p for p in zz if p[1] == "H"][-2:]
    lows = [p for p in zz if p[1] == "L"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        swing = "unclear"
    else:
        hh, hl = highs[1][2] > highs[0][2], lows[1][2] > lows[0][2]
        swing = ("uptrend" if hh and hl else "downtrend" if not hh and not hl
                 else "expanding range" if hh else "contracting range")
    return {"trend": swing, "state": state, "events": events,
            "swing_high": round(sh[1], 4) if sh else None, "swing_low": round(sl[1], 4) if sl else None}


def _levels(zz, atr, close, n):
    pts = sorted((p[2], p[0]) for p in zz)
    groups = []
    for price, idx in pts:
        if groups and price - groups[-1][-1][0] <= 0.6 * atr and price - groups[-1][0][0] <= 1.2 * atr:
            groups[-1].append((price, idx))
        else:
            groups.append([(price, idx)])
    out = []
    for g in groups:
        if len(g) >= 2:
            p = sum(x[0] for x in g) / len(g)
            out.append({"price": round(p, 4), "touches": len(g), "last": max(x[1] for x in g),
                        "side": "resistance" if p > close else "support"})
    out.sort(key=lambda x: (-x["touches"], abs(x["price"] - close)))
    return sorted(out[:6], key=lambda x: -x["price"])


def _fit(pts):
    """Least-squares line through [(index, price)]: (slope, intercept, worst miss)."""
    n = len(pts)
    mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    m = sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else 0.0
    c = my - m * mx
    return m, c, max(abs(p[1] - (m * p[0] + c)) for p in pts)


def _pat(name, bias, status, trigger, target, invalid, note, lines, start):
    r = lambda x: None if x is None else round(x, 4)
    return {"name": name, "bias": bias, "status": status, "trigger": r(trigger), "target": r(target),
            "invalid": r(invalid), "note": note, "lines": [[a, r(p), c, r(q)] for a, p, c, q in lines], "start": start}


def _tops(b, zz, atr, s):
    """Double / triple top (s=+1) or bottom (s=-1) and head and shoulders, from the last swings."""
    n, close = len(b), b[-1]["c"]
    v = lambda p: s * p                                     # flip prices so bottoms look like tops
    ext = [p for p in zz if p[1] == ("H" if s > 0 else "L") and p[0] >= n - 110]
    mid = [p for p in zz if p[1] == ("L" if s > 0 else "H")]
    between = lambda a, c: [p for p in mid if a[0] < p[0] < c[0]]
    before = lambda a: next((p for p in reversed(mid) if p[0] < a[0]), None)
    after = lambda i: [x["c"] for x in b[i + 1:]]
    word = ("Top", "bearish", "below", "above") if s > 0 else ("Bottom", "bullish", "above", "below")
    out = []
    # head and shoulders: three extremes, the middle one clearly beyond the other two
    if len(ext) >= 3:
        a, h, c = ext[-3:]
        t1, t2 = between(a, h), between(h, c)
        pre = before(a)
        if (t1 and t2 and v(h[2]) > v(a[2]) + 0.7 * atr and v(h[2]) > v(c[2]) + 0.7 * atr
                and abs(a[2] - c[2]) <= max(1.5 * atr, 0.04 * close) and c[0] - a[0] >= 10):
            t1 = max(t1, key=lambda p: -v(p[2]))
            t2 = max(t2, key=lambda p: -v(p[2]))
            if pre is None or v(pre[2]) < min(v(t1[2]), v(t2[2])):
                m = (t2[2] - t1[2]) / (t2[0] - t1[0])
                neck = lambda i: t1[2] + m * (i - t1[0])
                height = abs(h[2] - neck(h[0]))
                closes = after(c[0])
                broke = any(v(x) < v(neck(c[0] + 1 + j)) for j, x in enumerate(closes))
                failed = any(v(x) > v(c[2]) for x in closes)
                if not failed:
                    out.append(_pat(("Head and shoulders" if s > 0 else "Inverse head and shoulders"), word[1],
                                    "confirmed" if broke else "forming", neck(n - 1), neck(n - 1) - s * height, c[2],
                                    f"Neckline through the two {'troughs' if s > 0 else 'peaks'}; target is the head-to-neckline height.",
                                    [(t1[0], t1[2], n - 1, neck(n - 1)), (a[0], a[2], a[0], a[2]), (h[0], h[2], h[0], h[2]), (c[0], c[2], c[0], c[2])],
                                    a[0]))
                    return out
    tol = max(0.6 * atr, 0.012 * close)
    for count in (3, 2):
        if len(ext) < count:
            continue
        grp = ext[-count:]
        if max(p[2] for p in grp) - min(p[2] for p in grp) > tol or grp[-1][0] - grp[0][0] < 8:
            continue
        mids = [p for a, c in zip(grp, grp[1:]) for p in between(a, c)]
        if not mids:
            continue
        floor = max(mids, key=lambda p: -v(p[2]))             # the deepest pullback between the tests
        top = sum(p[2] for p in grp) / count
        height = abs(top - floor[2])
        pre = before(grp[0])
        if height < 2 * atr or (pre is not None and v(pre[2]) > v(floor[2])):
            continue
        closes = after(grp[-1][0])
        if any(v(x) > max(v(p[2]) for p in grp) for x in closes):
            continue                                           # new extreme beyond the tops: pattern is void
        broke = any(v(x) < v(floor[2]) for x in closes)
        out.append(_pat(("Triple " if count == 3 else "Double ") + word[0].lower(), word[1],
                        "confirmed" if broke else "forming", floor[2], floor[2] - s * height,
                        max(grp, key=lambda p: v(p[2]))[2],
                        f"{count} tests of {top:.2f}; confirmed on a close {word[2]} {floor[2]:.2f}.",
                        [(grp[0][0], top, n - 1, top), (floor[0], floor[2], n - 1, floor[2])], grp[0][0]))
        break
    return out


def _lines(b, zz, atr):
    """Triangles, wedges, channels and rectangles: lines fitted to the recent swing highs and swing lows."""
    n, close = len(b), b[-1]["c"]
    for look in (45, 70, 100):
        hs = [(p[0], p[2]) for p in zz if p[1] == "H" and p[0] >= n - look]
        ls = [(p[0], p[2]) for p in zz if p[1] == "L" and p[0] >= n - look]
        if len(hs) < 2 or len(ls) < 2 or len(hs) + len(ls) < 5:
            continue
        mh, ch, eh = _fit(hs)
        ml, cl, el = _fit(ls)
        if max(eh, el) > 0.7 * atr:
            continue
        start = min(hs[0][0], ls[0][0])
        up = lambda i: mh * i + ch
        lo = lambda i: ml * i + cl
        u, l, w0 = up(n - 1), lo(n - 1), up(start) - lo(start)
        if u - l < 0.5 * atr or w0 <= 0:
            continue
        sh, sl = mh / atr, ml / atr                       # slope in ATRs per bar
        flat = lambda x: abs(x) < 0.02
        ratio = (u - l) / w0
        conv, par = ratio < 0.75, 0.75 <= ratio <= 1.3
        if flat(sh) and sl >= 0.02 and conv:
            name, bias, trig, tgt, inv, note = "Ascending triangle", "bullish", u, u + w0, l, "Flat resistance with rising lows."
        elif flat(sl) and sh <= -0.02 and conv:
            name, bias, trig, tgt, inv, note = "Descending triangle", "bearish", l, l - w0, u, "Flat support with falling highs."
        elif sh <= -0.02 and sl >= 0.02 and conv:
            name, bias, trig, tgt, inv, note = "Symmetrical triangle", "neutral", None, None, None, "Lower highs and higher lows converging; it tends to continue the prior trend. Trade the side that breaks."
        elif sh > 0.02 and sl > 0.02 and conv and sl > sh:
            name, bias, trig, tgt, inv, note = "Rising wedge", "bearish", l, lo(start), u, "Price climbing inside converging lines; momentum fading."
        elif sh < -0.02 and sl < -0.02 and conv and sh < sl:
            name, bias, trig, tgt, inv, note = "Falling wedge", "bullish", u, up(start), l, "Price falling inside converging lines."
        elif flat(sh) and flat(sl) and par and len(hs) + len(ls) >= 5:
            name, bias, trig, tgt, inv, note = "Rectangle (trading range)", "neutral", None, None, None, "Horizontal support and resistance. Trade the breakout; target is the range height."
        elif sh > 0.02 and sl > 0.02 and par:
            name, bias, trig, tgt, inv, note = "Ascending channel", "bullish", l, None, None, "Trend continues while inside. A close below the lower line warns of a reversal."
        elif sh < -0.02 and sl < -0.02 and par:
            name, bias, trig, tgt, inv, note = "Descending channel", "bearish", u, None, None, "Trend continues while inside. A close above the upper line warns of a reversal."
        else:
            continue
        if close > u + 0.1 * atr:
            status, side = "broke out above", "up"
        elif close < l - 0.1 * atr:
            status, side = "broke down below", "down"
        else:
            status, side = "forming", None
        if bias == "neutral":
            note += f" Above {u:.2f} targets {u + w0:.2f}; below {l:.2f} targets {l - w0:.2f}."
        if "channel" in name.lower():
            status = "inside the channel" if side is None else status
        elif side:
            good = (side == "up" and bias in ("bullish", "neutral")) or (side == "down" and bias in ("bearish", "neutral"))
            status = ("confirmed, " if good else "failed, ") + status
            if bias == "neutral":
                trig, tgt, inv = (u, u + w0, l) if side == "up" else (l, l - w0, u)
        return [_pat(name, bias, status, trig, tgt, inv, note,
                     [(start, up(start), n - 1, u), (start, lo(start), n - 1, l)], start)]
    return []


def _flag(b, atr):
    n = len(b)
    best = None
    for e in range(n - 4, max(n - 17, 12), -1):                # e = last bar of the pole
        for k in range(3, 11):
            a = e - k
            for s in (1, -1):
                move = s * (b[e]["c"] - b[a]["c"])
                if move < 4 * atr or move > 0.35 * b[a]["c"]:
                    continue
                tip = b[e]["h"] if s > 0 else b[e]["l"]
                base = min(x["l"] for x in b[a:e + 1]) if s > 0 else max(x["h"] for x in b[a:e + 1])
                pole = abs(tip - base)
                cons = b[e + 1:]
                far = min(x["l"] for x in cons) if s > 0 else max(x["h"] for x in cons)     # deepest pullback
                edge = max([tip] + [x["h"] for x in cons[:-1]]) if s > 0 else min([tip] + [x["l"] for x in cons[:-1]])
                if abs(tip - far) > 0.5 * pole or s * (edge - tip) > 0.35 * pole:
                    continue
                if best is None or move / k > best[0]:
                    best = (move / k, s, a, e, tip, base, pole, far, edge)
        if best:
            break
    if not best:
        return []
    _, s, a, e, tip, base, pole, far, edge = best
    close = b[-1]["c"]
    broke = s * (close - edge) > 0
    return [_pat("Bull flag / pennant" if s > 0 else "Bear flag / pennant", "bullish" if s > 0 else "bearish",
                 "confirmed" if broke else "forming", edge, edge + s * pole, tip - s * 0.5 * pole,
                 f"Pole of {pole:.2f} over {e - a} bars, then a {n - 1 - e}-bar pause. Fails if the pause gives back more than half the pole.",
                 [(a, base, e, tip), (e, edge, n - 1, edge), (e, far, n - 1, far)], a)]


def _gaps(b, atr):
    n, out = len(b), []
    vols = [x["v"] for x in b]
    for i in range(max(1, n - 30), n):
        p, x = b[i - 1], b[i]
        s = 1 if x["l"] > p["h"] + 0.3 * atr else -1 if x["h"] < p["l"] - 0.3 * atr else 0
        if not s:
            continue
        edge = p["h"] if s > 0 else p["l"]
        filled = any((y["l"] <= edge) if s > 0 else (y["h"] >= edge) for y in b[i + 1:])
        avg = sum(vols[max(0, i - 50):i]) / max(1, len(vols[max(0, i - 50):i]))
        prior = b[max(0, i - 20):i]
        out_of_range = x["c"] > max(y["h"] for y in prior) if s > 0 else x["c"] < min(y["l"] for y in prior)
        run = s * (p["c"] - b[max(0, i - 10)]["c"])
        kind = ("breakaway" if out_of_range and avg and x["v"] >= 1.5 * avg and run < 3 * atr
                else "exhaustion" if run >= 3 * atr and filled
                else "runaway" if run >= 3 * atr else "common")
        out.append({"i": i, "dir": "up" if s > 0 else "down", "from": round(edge, 4),
                    "to": round(x["l"] if s > 0 else x["h"], 4), "filled": filled, "kind": kind, "date": x["t"]})
    return out[-3:]


def _read(struct, patterns, gaps, levels, close, atr, label, stoch=None):
    score, why = 0, []
    t = struct["trend"]
    if t == "uptrend":
        score += 2; why.append(f"{label} swings are making higher highs and higher lows")
    elif t == "downtrend":
        score -= 2; why.append(f"{label} swings are making lower highs and lower lows")
    else:
        why.append(f"{label} swings are in {'an' if t[0] in 'aeiou' else 'a'} {t}" if t != "unclear" else f"{label} swing structure is unclear")
    ev = struct["events"][-1] if struct["events"] else None
    if ev:
        score += 1 if ev["dir"] == "up" else -1
        why.append(("break of structure" if ev["type"] == "BOS" else "change of character")
                   + f" {ev['dir']} through {ev['price']:.2f}")
    for p in patterns:
        w = 2 if p["status"].startswith("confirmed") else -2 if p["status"].startswith("failed") else 1
        d = 1 if p["bias"] == "bullish" else -1 if p["bias"] == "bearish" else (
            1 if "above" in p["status"] else -1 if "below" in p["status"] else 0)
        if w == -2 and p["bias"] == "neutral":
            w = 2
        score += w * d
        if d:
            why.append(f"{p['name'].lower()} ({p['status']})")
    if stoch:
        cr = stoch["cross"]
        if cr and cr["dir"] == "up" and cr["from"] == "oversold":
            score += 1; why.append(f"Stochastic RSI crossed up out of oversold ({stoch['k']:.0f})")
        elif cr and cr["dir"] == "down" and cr["from"] == "overbought":
            score -= 1; why.append(f"Stochastic RSI crossed down out of overbought ({stoch['k']:.0f})")
        else:
            why.append(f"Stochastic RSI is {stoch['zone']} ({stoch['k']:.0f})")
    lean = "bullish" if score >= 2 else "bearish" if score <= -2 else "neutral"
    res = min((l["price"] for l in levels if l["price"] > close), default=None)
    sup = max((l["price"] for l in levels if l["price"] < close), default=None)
    return {"lean": lean, "score": score, "why": why, "next_resistance": res, "next_support": sup}


def analyze(ticker, tf="1d"):
    tf = tf if tf in TIMEFRAMES else "1d"
    sym = _symbol(ticker)
    allb = _bars(sym, tf)
    b = allb[-SHOW:]
    atr = _atr(b)
    if atr <= 0:
        raise QuoteError(f"{sym}: prices are not moving enough to analyze")
    zz = _pivots(b)
    close = b[-1]["c"]
    struct = _structure(b, zz)
    levels = _levels(zz, atr, close, len(b))
    patterns = _tops(b, zz, atr, 1) + _tops(b, zz, atr, -1) + _lines(b, zz, atr) + _flag(b, atr)
    gaps = _gaps(b, atr)
    label = TIMEFRAMES[tf][2]
    k, dd = _stoch_rsi([x["c"] for x in allb])
    k, dd = k[-len(b):], dd[-len(b):]
    stoch = _stoch_read(k, dd)
    r1 = lambda xs: [None if v is None else round(v, 1) for v in xs]
    struct_out = dict(struct, events=struct["events"][-4:])
    return {"ticker": ticker.upper().strip(), "symbol": sym, "tf": tf, "tf_label": label,
            "asof": b[-1]["t"], "close": round(close, 4), "atr": round(atr, 4),
            "bars": [{k: (round(v, 4) if k != "t" else v) for k, v in x.items()} for x in b],
            "pivots": _label(zz), "structure": struct_out, "levels": levels, "patterns": patterns, "gaps": gaps,
            "read": _read(struct, patterns, gaps, levels, close, atr, label.capitalize(), stoch),
            "stoch": {"k": r1(k), "d": r1(dd), "now": stoch},
            "paths_available": os.path.exists(MAP_PY) and os.path.exists(MAP_PYTHON)}


def _safe(sym):
    return "".join(ch if ch.isalnum() else "_" for ch in sym)


def paths(ticker):
    """Bull / base / bear paths with odds from the stock-direction-map script (daily chart)."""
    sym = _symbol(ticker)
    if not (os.path.exists(MAP_PY) and os.path.exists(MAP_PYTHON)):
        raise QuoteError("Potential paths need the stock-direction-map script, which is not installed on this computer")

    def run():
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            r = subprocess.run([MAP_PYTHON, MAP_PY, sym, "--out", CACHE_DIR, "--months", "6"],
                               capture_output=True, text=True, timeout=150)
        except subprocess.TimeoutExpired:
            raise QuoteError(f"{sym}: the path map took too long, try again")
        path = os.path.join(CACHE_DIR, sym + ".json")
        if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 300:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["no output"]
            raise QuoteError(f"{sym}: could not build the path map ({tail[0][:160]})")
        d = json.load(open(path))
        sc = d.get("scenarios", {})
        keep = lambda k: {x: sc.get(k, {}).get(x) for x in ("odds_pct", "trigger", "targets", "invalidation", "range")}
        return {"ticker": ticker.upper().strip(), "symbol": sym, "as_of": d.get("as_of"), "close": d.get("close"),
                "intraday": d.get("last_bar_intraday"), "lean": d.get("lean"),
                "score": d.get("score", {}).get("total"), "score_max": d.get("score", {}).get("max"),
                "signals": d.get("score", {}).get("signals", []),
                "bull": keep("bull"), "base": keep("base"), "bear": keep("bear"),
                "expected": d.get("expected_move_realized"), "implied": d.get("expected_move_implied"),
                "earnings": d.get("next_earnings"), "png": os.path.basename(d.get("chart_png") or "")}
    return _cached(("paths", sym), run, ttl=PATHS_TTL)


def chart_png(ticker):
    path = os.path.join(CACHE_DIR, _symbol(ticker) + ".png")
    if not os.path.exists(path):
        raise QuoteError("No chart yet: load the potential paths first")
    with open(path, "rb") as f:
        return f.read()
