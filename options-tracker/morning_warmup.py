"""Morning warm-up: run the tracker's heavy scans one at a time before the open.

Run by launchd on weekdays at 5:30 AM PT (com.soundinvestmentsolutions.optiontracker-warmup).
Starts the tracker if it isn't running, then refreshes each scan in turn and waits for it to
finish before starting the next, so they never hit Yahoo at the same time. Scans stay fresh for
4 hours (6 for Smart Money), so during the session the tabs open on these results and only
the lookups you type go out to Yahoo / Tradier.

  python3 morning_warmup.py            # run every step
  python3 morning_warmup.py --dry-run  # list the steps
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "http://127.0.0.1:8765"

# (label, path that starts the scan, path that reports progress, done(json) -> bool, max minutes)
LEAPS_VIEWS = [("watch", "leap"), ("market", "leap"), ("watch", "otm20"), ("market", "otm20")]


def _leaps(mode, term):
    q = f"mode={mode}&term={term}"
    return (f"LEAPS {mode} {term}", f"/api/leaps?{q}&refresh=1", f"/api/leaps?{q}",
            lambda d: not d["job"]["running"], 30)


STEPS = [
    # Screener first: Sell Puts and the LEAPS market lists are built from it. Done when the scan
    # and its open-interest lookups (once a day is enough; exchanges update OI overnight) finish.
    ("Screener + open interest", "/api/screener?refresh=1", "/api/screener",
     lambda d: not d["job"]["running"] and not d["job"].get("oi_running"), 30),
    ("Sell Puts", "/api/putsell?refresh=1", "/api/putsell", lambda d: not d["job"]["running"], 45),
    *[_leaps(m, t) for m, t in LEAPS_VIEWS],
    ("Smart Money", "/api/smartmoney?refresh=1", "/api/smartmoney", lambda d: not d["job"]["running"], 20),
]


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def get(path, timeout=60):
    with urllib.request.urlopen(URL + path, timeout=timeout) as f:
        return json.load(f)


def up():
    try:
        urllib.request.urlopen(URL + "/", timeout=3).close()
        return True
    except OSError:
        return False


def start_tracker():
    if up():
        return True
    log("starting tracker")
    subprocess.Popen(["/bin/bash", os.path.join(HERE, "run.sh"), "--no-browser"], cwd=HERE,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(120):   # first run may install packages
        time.sleep(1)
        if up():
            return True
    return False


def run(step):
    label, start, poll, done, minutes = step
    t0 = time.time()
    try:
        get(start)
    except OSError as e:
        log(f"{label}: could not start ({e})")
        return False
    time.sleep(5)
    while time.time() - t0 < minutes * 60:
        try:
            d = get(poll)
            if done(d):
                job = d.get("job") or {}
                rows = len(d.get("rows") or [])
                failed = len(d.get("failed") or {}) if isinstance(d.get("failed"), dict) else d.get("failed")
                log(f"{label}: done in {(time.time() - t0) / 60:.1f} min, {rows} rows, failed {failed or 0}"
                    + (f", error: {job['error']}" if job.get("error") else ""))
                return not job.get("error")
        except (OSError, ValueError, KeyError) as e:
            log(f"{label}: poll failed ({e}), retrying")
        time.sleep(10)
    log(f"{label}: still running after {minutes} min, moving on")
    return False


def main():
    if "--dry-run" in sys.argv:
        for s in STEPS:
            print(f"{s[0]:28s} {s[1]}")
        return 0
    log("morning warm-up")
    if not start_tracker():
        log("tracker did not start; try ./run.sh by hand to see the error")
        return 1
    ok = [run(s) for s in STEPS]
    log(f"warm-up finished: {sum(ok)}/{len(ok)} scans OK")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
