"""Smart Money tab: what Congress, company insiders and big option traders are buying.

- Congress: House periodic transaction reports (PTRs) from the House Clerk. The yearly index
  (https://disclosures-clerk.house.gov/public_disc/financial-pdfs/<year>FD.zip) lists every filing;
  each PTR is a PDF at /public_disc/ptr-pdfs/<year>/<DocID>.pdf. Parsed PDFs are cached in
  smartmoney_ptr/ so each one is downloaded once. Members have up to 45 days to report a trade, and
  amounts are ranges. The Senate's site needs a terms click-through first, so it isn't pulled yet.
- Insiders: open-market purchases from Yahoo (yfinance insider_transactions) for the tickers in
  smart_watch.json. Yahoo misses some filings (e.g. indirect holdings); SEC Form 4 is the full source.
- Options flow: the latest open-scan files written by ~/.claude/scripts/uoa_scan.py.

    python smartmoney.py          # rebuild and print a summary
    python smartmoney.py alert    # rebuild and text any new alert-worthy item (run each morning)

A screen of public filings, not advice.
"""
import glob
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "smartmoney_cache.json")
PTR_DIR = os.path.join(HERE, "smartmoney_ptr")
WATCH_FILE = os.path.join(HERE, "smart_watch.json")
ALERTS_FILE = os.path.join(HERE, "smart_alerts.json")
ALERTED_FILE = os.path.join(HERE, "smart_alerted.json")
UOA_DIR = os.path.expanduser("~/Documents/Claude-deliverables/uoa")
HOUSE = "https://disclosures-clerk.house.gov/public_disc"
UA = "OptionsTracker personal-research"
DAYS = 60              # congress filings filed in the last 60 days
INSIDER_DAYS = 183     # insider buys in the last 6 months
CLUSTER_DAYS = 30
FRESH_SECONDS = 6 * 3600
PHONE = "+17142139798"
PAUSE_FILE = os.path.expanduser("~/.claude/imessage_paused")
DEFAULT_ALERTS = {"congress_watch": True, "cluster": True, "insider_1m": True, "ceo_cfo": False}
STOCK_TYPES = {"ST", "OP"}   # stocks and stock options; bonds, CDs and funds are left out

_lock = threading.Lock()
_job = {"running": False, "phase": "", "done": 0, "total": 0, "started": 0, "finished": 0, "error": None}
_data = None
_loaded = False


# ---------- small helpers ----------

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def watchlist():
    return [str(t).strip().upper() for t in _load(WATCH_FILE, []) if str(t).strip()]


def alert_settings():
    s = dict(DEFAULT_ALERTS)
    s.update({k: bool(v) for k, v in _load(ALERTS_FILE, {}).items() if k in DEFAULT_ALERTS})
    return s


def save_alert_settings(body):
    s = alert_settings()
    s.update({k: bool(body[k]) for k in DEFAULT_ALERTS if k in body})
    _save(ALERTS_FILE, s)
    return s


def _mdy(s):
    return datetime.strptime(s, "%m/%d/%Y").date()


def _progress(phase, done=None, total=None):
    with _lock:
        _job["phase"] = phase
        if done is not None:
            _job["done"] = done
        if total is not None:
            _job["total"] = total


# ---------- Congress (House) ----------

def _house_index(year):
    """PTR filings for one year from the House Clerk's zip index (re-downloaded at most every 6 h)."""
    os.makedirs(PTR_DIR, exist_ok=True)
    path = os.path.join(PTR_DIR, f"{year}FD.xml")
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > FRESH_SECONDS:
        z = zipfile.ZipFile(io.BytesIO(_get(f"{HOUSE}/financial-pdfs/{year}FD.zip", 60)))
        with open(path, "wb") as f:
            f.write(z.read(f"{year}FD.xml"))
    out = []
    for m in ET.parse(path).getroot():
        if m.findtext("FilingType") != "P":
            continue
        name = " ".join(x for x in (m.findtext("First"), m.findtext("Last")) if x).strip()
        out.append({"doc": m.findtext("DocID"), "year": year, "filed": _mdy(m.findtext("FilingDate")).isoformat(),
                    "member": re.sub(r"\b(\w+) \1\b", r"\1", re.sub(r"\s+", " ", name)), "dist": m.findtext("StateDst") or ""})
    return out


ROW = re.compile(
    r"\(([A-Z][A-Z0-9.\-]{0,7})\)\s*\[([A-Z]{2})\]\s*"
    r"(P|S \(partial\)|S|E)\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+"
    r"(\$[\d,]+(?:\.\d+)?(?:\s*-\s*\$[\d,]+)?|Over \$[\d,]+)")
OWNER = {"SP": "Spouse", "JT": "Joint", "DC": "Child"}


def _money(s):
    """'$1,001' -> '$1K', '$5,000,000' -> '$5M', '$710.52' -> '$711'."""
    v = float(s.replace("$", "").replace(",", ""))
    if v >= 1e6:
        return f"${round(v / 1e6):,}M"
    if v >= 1e3:
        return f"${round(v / 1e3):,}K"
    return f"${v:,.0f}"


def _amount(s):
    if s.startswith("Over"):
        return "Over " + _money(s[5:].strip())
    return "–".join(_money(p.strip()) for p in s.split("-"))


def _parse_ptr(text):
    """Transactions in one PTR's text. Each row reads: [owner] asset (TICKER) [type] P|S date date $amount."""
    text = re.sub(r"\s+", " ", text.replace("\x00", ""))
    rows = []
    for m in ROW.finditer(text):
        # text since the previous row ended ("...: New"): the owner code (SP/JT/DC) sits just before
        # the asset name; no code means the member's own account
        tail = text[max(0, m.start() - 200):m.start()].rsplit(": New", 1)[-1]
        own = re.findall(r"(?:^|\s)(SP|JT|DC)\s+(?=[A-Z0-9])", tail.rsplit("$200?", 1)[-1])   # skip the header (it has "Washington, DC")
        kind = m.group(3)
        rows.append({"ticker": m.group(1).replace(".", "-"), "asset_type": m.group(2),
                     "side": "Buy" if kind == "P" else "Exchange" if kind == "E" else "Sell",
                     "partial": "partial" in kind, "traded": _mdy(m.group(4)).isoformat(),
                     "notified": _mdy(m.group(5)).isoformat(), "amount": _amount(m.group(6)),
                     "owner": OWNER[own[-1]] if own else "Self"})
    return rows


def _ptr(f):
    """One filing's transactions, from cache or by downloading and reading its PDF."""
    path = os.path.join(PTR_DIR, f"{f['doc']}.json")
    cached = _load(path, None)
    if cached is not None:
        return cached
    import pypdf
    url = f"{HOUSE}/ptr-pdfs/{f['year']}/{f['doc']}.pdf"
    try:
        pdf = pypdf.PdfReader(io.BytesIO(_get(url)))
        text = " ".join(p.extract_text() or "" for p in pdf.pages)
        rows = _parse_ptr(text)
        res = {"rows": rows, "scanned": not text.strip(), "url": url}
    except Exception as e:   # network hiccup: don't cache, try again next build
        return {"rows": [], "error": str(e), "url": url}
    _save(path, res)
    return res


def congress():
    today = date.today()
    filings = _house_index(today.year)
    if today.month <= 2:
        filings += _house_index(today.year - 1)
    cut = (today - timedelta(days=DAYS)).isoformat()
    filings = sorted((f for f in filings if f["filed"] >= cut), key=lambda f: f["filed"], reverse=True)
    trades, paper = [], []
    _progress("Reading House filings", 0, len(filings))
    for i, f in enumerate(filings):
        p = _ptr(f)
        if p.get("scanned"):
            paper.append({"member": f["member"], "dist": f["dist"], "filed": f["filed"], "url": p["url"]})
        for r in p["rows"]:
            if r["asset_type"] not in STOCK_TYPES:
                continue
            late = (date.fromisoformat(f["filed"]) - date.fromisoformat(r["traded"])).days
            trades.append(dict(r, member=f["member"], dist=f["dist"], filed=f["filed"], late=late,
                               url=p["url"], chamber="House"))
        _progress("Reading House filings", i + 1)
    return trades, paper, len(filings)


def clusters(trades):
    cut = (date.today() - timedelta(days=CLUSTER_DAYS + 45)).isoformat()   # trade dates, allowing the filing lag
    by = {}
    for t in trades:
        if t["side"] == "Buy" and t["traded"] >= cut:
            by.setdefault(t["ticker"], []).append(t)
    out = []
    for tk, ts in by.items():
        members = sorted({t["member"] for t in ts})
        if len(members) >= 2 or len(ts) >= 3:
            ts.sort(key=lambda t: t["traded"])
            first, last = ts[0]["traded"], ts[-1]["traded"]
            if (date.fromisoformat(last) - date.fromisoformat(first)).days <= CLUSTER_DAYS or len(members) == 1:
                out.append({"ticker": tk, "members": members, "buys": len(ts), "first": first, "last": last})
    out.sort(key=lambda c: (-len(c["members"]), -c["buys"]))
    return out


# ---------- Insiders ----------

PRICE = re.compile(r"at price ([\d.]+)(?:\s*-\s*([\d.]+))?")


def insiders(tickers):
    import yfinance as yf
    cut = date.today() - timedelta(days=INSIDER_DAYS)
    out, failed = [], {}
    _progress("Reading insider buys", 0, len(tickers))
    for i, sym in enumerate(tickers):
        if i:
            time.sleep(0.6)   # go easy on Yahoo
        try:
            df = yf.Ticker(sym).insider_transactions
        except Exception as e:
            failed[sym] = str(e)[:120]
            continue
        if df is None or df.empty:
            _progress("Reading insider buys", i + 1)
            continue
        for r in df.to_dict("records"):
            text = str(r.get("Text") or "")
            if not text.lower().startswith("purchase"):
                continue
            sd = r.get("Start Date")
            d = sd.date() if hasattr(sd, "date") else None
            if d is None or d < cut:
                continue
            m = PRICE.search(text)
            px = float(m.group(1)) if m else None
            pos = str(r.get("Position") or "")
            sh = int(r.get("Shares") or 0)
            val = r.get("Value")
            val = float(val) if val == val and val else (px or 0) * sh
            out.append({"ticker": sym, "date": d.isoformat(), "name": str(r.get("Insider") or "").title(),
                        "role": pos, "shares": sh, "price": px, "value": val,
                        "officer": bool(re.search(r"chief executive|chief financial|\bceo\b|\bcfo\b", pos, re.I))})
        _progress("Reading insider buys", i + 1)
    out.sort(key=lambda x: x["date"], reverse=True)
    return out, failed



# ---------- Insiders from SEC Form 4 (complete; needs a contact email in sec_contact.txt) ----------

SEC_CONTACT_FILE = os.path.join(HERE, "sec_contact.txt")
_sec_last = [0.0]


def sec_contact():
    try:
        with open(SEC_CONTACT_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _sec(url, as_json=False):
    """SEC asks automated tools to name a contact and stay under 10 requests a second."""
    wait = 0.15 - (time.time() - _sec_last[0])
    if wait > 0:
        time.sleep(wait)
    _sec_last[0] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": f"OptionsTracker {sec_contact()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        b = r.read()
    return json.loads(b) if as_json else b


def _ciks():
    path = os.path.join(PTR_DIR, "sec_tickers.json")
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 7 * 86400:
        os.makedirs(PTR_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(_sec("https://www.sec.gov/files/company_tickers.json"))
    return {v["ticker"].upper().replace(".", "-"): int(v["cik_str"]) for v in _load(path, {}).values()}


def _x(node, path):
    v = node.find(path)
    return (v.text or "").strip() if v is not None and v.text else ""


def _form4(cik, acc, doc):
    """Open-market purchases (code P) in one Form 4, cached by accession number."""
    path = os.path.join(PTR_DIR, f"f4_{acc}.json")
    cached = _load(path, None)
    if cached is not None:
        return cached
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc.split('/')[-1]}"
    root = ET.fromstring(_sec(url))
    own = root.find("reportingOwner")
    rel = own.find("reportingOwnerRelationship") if own is not None else None
    flag = lambda k: rel is not None and _x(rel, k) in ("1", "true")
    title = _x(rel, "officerTitle") if rel is not None else ""
    role = title or ("Director" if flag("isDirector") else "10% owner" if flag("isTenPercentOwner") else "")
    buys = []
    for t in root.iter("nonDerivativeTransaction"):
        if _x(t, "transactionCoding/transactionCode") != "P":
            continue
        try:
            sh = float(_x(t, "transactionAmounts/transactionShares/value") or 0)
            px = float(_x(t, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        buys.append({"date": _x(t, "transactionDate/value")[:10], "shares": sh, "price": px})
    res = {"name": _x(own, "reportingOwnerId/rptOwnerName").title() if own is not None else "",
           "role": role, "buys": buys, "url": url}
    _save(path, res)
    return res


def sec_insiders(tickers):
    cut = (date.today() - timedelta(days=INSIDER_DAYS)).isoformat()
    ciks = _ciks()
    out, failed = [], {}
    _progress("Reading SEC insider filings", 0, len(tickers))
    for i, sym in enumerate(tickers):
        cik = ciks.get(sym)
        if not cik:
            failed[sym] = "no SEC filer (foreign companies don't file Form 4)"
            _progress("Reading SEC insider filings", i + 1)
            continue
        try:
            r = _sec(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", True)["filings"]["recent"]
            for k, form in enumerate(r["form"]):
                if form not in ("4", "4/A") or r["filingDate"][k] < cut:
                    continue
                f = _form4(cik, r["accessionNumber"][k], r["primaryDocument"][k])
                if not f["buys"]:
                    continue
                sh = sum(b["shares"] for b in f["buys"])
                val = sum(b["shares"] * b["price"] for b in f["buys"])
                out.append({"ticker": sym, "date": min(b["date"] for b in f["buys"]), "name": f["name"],
                            "role": f["role"], "shares": int(sh), "price": round(val / sh, 2) if sh else None,
                            "value": val, "url": f["url"],
                            "officer": bool(re.search(r"chief executive|chief financial|\bceo\b|\bcfo\b", f["role"], re.I))})
        except Exception as e:
            failed[sym] = f"{type(e).__name__}: {e}"[:120]
        _progress("Reading SEC insider filings", i + 1)
    seen, uniq = set(), []
    for x in out:   # an amended Form 4 (4/A) repeats the original's buys
        k = (x["ticker"], x["name"], x["date"], x["shares"])
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    out = uniq
    out.sort(key=lambda x: x["date"], reverse=True)
    return out, failed

# ---------- Options flow (from the unusual-activity scanner) ----------

def flow(days=3):
    cut = (date.today() - timedelta(days=days)).isoformat()
    out = []
    for path in sorted(glob.glob(os.path.join(UOA_DIR, "*", "*-open.json"))):
        day = os.path.basename(path)[:10]
        if day < cut:
            continue
        d = _load(path, {})
        o = d.get("options") or {}
        out.append({"ticker": d.get("ticker"), "day": day, "as_of": d.get("as_of"), "flags": d.get("flags", []),
                    "lean": o.get("lean"), "summary": o.get("summary"), "prints": (o.get("prints") or [])[:4]})
    out.sort(key=lambda x: (x["day"], bool(x["flags"])), reverse=True)
    latest = {}
    for x in out:   # newest scan per ticker
        latest.setdefault(x["ticker"], x)
    return list(latest.values())


# ---------- hits, prices, build ----------

def _prices(tickers):
    import yfinance as yf
    px = {}
    for sym in tickers:
        try:
            px[sym] = round(float(yf.Ticker(sym).fast_info["last_price"]), 2)
        except Exception:
            pass
    return px


def _d(iso):
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}"


def hits(trades, ins, clus, watch, settings=None):
    """Items worth a text, newest first. Each has a stable id so a text goes out once."""
    s = settings or alert_settings()
    w = set(watch)
    out = []
    if s["congress_watch"]:
        for t in trades:
            if t["ticker"] in w and t["side"] == "Buy":
                out.append({"id": f"c:{t['url'].rsplit('/', 1)[-1]}:{t['ticker']}:{t['traded']}:{t['side']}",
                            "kind": "congress", "ticker": t["ticker"], "side": t["side"], "date": t["filed"],
                            "who": f"Rep. {t['member']} ({t['dist']})",
                            "what": f"Congress · traded {_d(t['traded'])} · filed {_d(t['filed'])}",
                            "detail": f"{t['amount']} ({t['owner'].lower()})"})
    for x in ins:
        big = s["insider_1m"] and x["ticker"] in w and x["value"] >= 1_000_000
        boss = s["ceo_cfo"] and x["officer"]
        if big or boss:
            out.append({"id": f"i:{x['ticker']}:{x['name']}:{x['date']}:{x['shares']}", "kind": "insider",
                        "ticker": x["ticker"], "side": "Buy", "date": x["date"],
                        "who": f"{x['name']}{', ' + x['role'] if x['role'] else ''}",
                        "what": f"Insider · {_d(x['date'])} · {x['shares']:,} shares" + (f" at ${x['price']:,.2f}" if x["price"] else ""),
                        "detail": f"${x['value'] / 1e6:,.1f}M" if x["value"] >= 1e6 else f"${x['value'] / 1e3:,.0f}K"})
    if s["cluster"]:
        for c in clus:
            if len(c["members"]) >= 2:
                out.append({"id": f"k:{c['ticker']}:{','.join(c['members'])}", "kind": "cluster", "ticker": c["ticker"],
                            "side": "Buy", "date": c["last"], "who": f"{len(c['members'])} members of Congress",
                            "what": f"Cluster · {c['buys']} buys {_d(c['first'])} to {_d(c['last'])}",
                            "detail": ", ".join(c["members"])})
    out.sort(key=lambda h: h["date"], reverse=True)
    return out


def build():
    watch = watchlist()
    trades, paper, nfilings = congress()
    ins, failed = sec_insiders(watch) if sec_contact() else insiders(watch)
    ins_source = "SEC Form 4" if sec_contact() else "Yahoo"
    clus = clusters(trades)
    hit = hits(trades, ins, clus, watch)
    _progress("Prices")
    px = _prices(sorted({h["ticker"] for h in hit} | {x["ticker"] for x in ins}))
    return {"built_at": time.time(), "watch": watch, "congress": trades, "paper": paper, "filings": nfilings,
            "clusters": clus, "insiders": ins, "insider_failed": failed, "insider_source": ins_source, "hits": hit, "prices": px, "flow": flow()}


def _run():
    global _data
    try:
        d = build()
        _save(CACHE_FILE, d)
        with _lock:
            _data = d
            _job.update(running=False, finished=time.time(), phase="Done")
    except Exception as e:
        with _lock:
            _job.update(running=False, finished=time.time(), error=f"{type(e).__name__}: {e}")


def feed(refresh=False):
    """Cached Smart Money data; rebuilds in the background when stale or asked. Poll while job.running."""
    global _data, _loaded
    with _lock:
        if not _loaded:
            _loaded = True
            _data = _load(CACHE_FILE, None)
        data = _data
        stale = data is None or time.time() - data["built_at"] > FRESH_SECONDS
        recent_fail = _job["error"] and time.time() - _job["finished"] < 600
        start = (refresh or (stale and not recent_fail)) and not _job["running"]
        if start:
            _job.update(running=True, started=time.time(), error=None, done=0, total=0, phase="Starting")
        job = dict(_job)
    if start:
        threading.Thread(target=_run, daemon=True).start()
    base = {"job": job, "stale": stale, "alerts": alert_settings()}
    if data is None:
        return dict(base, status="loading")
    d = dict(data)
    d["flow"] = flow()   # cheap: always fresh
    d["hits"] = hits(d["congress"], d["insiders"], d["clusters"], d["watch"])
    return dict(base, status="ok", **d)


# ---------- morning texts ----------

def _text(msg):
    if os.path.exists(PAUSE_FILE):
        return False
    script = ('on run argv\ntell application "Messages"\nset svc to 1st account whose service type = iMessage\n'
              'send (item 1 of argv) to participant (item 2 of argv) of svc\nend tell\nend run')
    subprocess.run(["osascript", "-e", script, msg, PHONE], check=True, timeout=30)
    return True


def alert():
    d = build()
    _save(CACHE_FILE, d)
    sent = set(_load(ALERTED_FILE, []))
    first_run = not os.path.exists(ALERTED_FILE)
    new = [h for h in d["hits"] if h["id"] not in sent]
    if new and not first_run:   # the first run only records what's already there
        lines = [f"{h['ticker']} {h['side']}: {h['who']} · {h['what']} · {h['detail']}" for h in new[:6]]
        more = f"\n(+{len(new) - 6} more in the Smart Money tab)" if len(new) > 6 else ""
        if not _text("Smart Money: new filings on your list\n" + "\n".join(lines) + more + "\nFilings, not advice."):
            return print("texts paused; will retry next run")
    _save(ALERTED_FILE, sorted(sent | {h["id"] for h in d["hits"]}))
    print(f"{len(new)} new item(s){' (first run: recorded, no text)' if first_run else ''}")


if __name__ == "__main__":
    if sys.argv[1:] == ["alert"]:
        alert()
    else:
        d = build()
        _save(CACHE_FILE, d)
        print(f"{d['filings']} House filings, {len(d['congress'])} stock trades, {len(d['paper'])} paper filings, "
              f"{len(d['clusters'])} clusters, {len(d['insiders'])} insider buys, {len(d['hits'])} hits")
        for h in d["hits"][:10]:
            print(" ", h["ticker"], h["side"], h["who"], "|", h["what"], "|", h["detail"])
