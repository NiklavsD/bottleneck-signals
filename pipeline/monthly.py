"""Monthly run: refresh data, compute the current signal for each bottleneck, and append it to the hash-chained log.

Scheduled for the 17th of each month (after the TW 10th deadline and the 16th decision date).
Each log entry stores the SHA-256 of the previous entry, so later edits to past calls are detectable.
"""
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
PY = sys.executable
LOG = "backtest/signal_log.jsonl"


def refresh():
    # fetch_tw rewrites the whole CSV from its start year; old pages come from the cache, so always start at 2005
    steps = [[PY, "pipeline/fetch_tw.py", "2005"], [PY, "pipeline/fetch_market.py"],
             [PY, "pipeline/fetch_comtrade.py"], [PY, "pipeline/fetch_census.py"],
             [PY, "pipeline/fetch_korea_customs.py"]]
    for cmd in steps:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        print(" ".join(cmd[1:]), "->", "ok" if r.returncode == 0 else f"FAILED\n{r.stderr[-2000:]}", flush=True)


def sanity_check():
    """Refuse to log if a refresh truncated history (the index needs years of data for its z-scores)."""
    tw = pd.read_csv("data/processed/tw_revenue.csv", usecols=["month"])
    first = pd.to_datetime(tw.month).min()
    if first > pd.Timestamp("2006-01-01"):
        sys.exit(f"ABORT: tw_revenue.csv starts {first.date()}; history was truncated, not logging")


def current_calls():
    """Model v4 (backtest/PREREGISTRATION_v4.md): memory in/out; optics and power as tilts vs their benchmark."""
    ns = {}
    exec(open("backtest/run_v4.py").read().split("\nif __name__ == ")[0], ns)
    mem, src, _ = ns["build_memory_v4"]()
    specs = [("memory", "v4", "ruleM", mem, "memory", None),
             ("optics", "v4-tilt", "ruleB", ns["build"](ns["GROUPS"]["optics"]), "optics", "SOXX"),
             ("power", "v4-tilt", "ruleB", ns["build"](ns["GROUPS"]["power"]), "power", "XLI")]
    calls = []
    for name, ver, rule, ind, grp, bench in specs:
        row = ns["live"](ind)
        basket = ns["GROUPS"][grp]["basket"] + ([bench] if bench else [])
        px = ns["PX"][basket].ffill().iloc[-1]
        if bench:
            state = "OVERWEIGHT" if row[rule] else "UNDERWEIGHT"
        else:
            state = "IN" if row[rule] else "OUT"
        call = {"bottleneck": name, "model": ver, "rule": rule, "state": state,
                "data_month": str(row.name.date()), "decision_date": str(row.decision.date()),
                "C": round(float(row.C), 3), "M": round(float(row.M), 3),
                "basket_prices": {k: round(float(v), 4) for k, v in px.items() if pd.notna(v)}}
        if bench:
            call["benchmark"] = bench
        if name == "memory":
            call["korea_source"] = src
        calls.append(call)
    return calls


def append(calls):
    prev = "0" * 64
    if os.path.exists(LOG):
        lines = [l for l in open(LOG) if l.strip()]
        if lines:
            prev = json.loads(lines[-1])["hash"]
    entry = {"logged_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "prev": prev,
             "calls": calls, "note": "Model output, not investment advice."}
    body = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    entry["hash"] = hashlib.sha256(body.encode()).hexdigest()
    with open(LOG, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def publish(entry):
    """Commit the new log entry (and refreshed public data) and push; the push time is the public timestamp."""
    paths = [LOG, "data/processed", "backtest"]
    subprocess.run(["git", "add", *paths], check=True)
    msg = f"Monthly signals {entry['logged_at'][:10]}: " + ", ".join(
        f"{c['bottleneck']} {c['state']}" for c in entry["calls"]) + f"\n\nentry hash {entry['hash']}"
    if subprocess.run(["git", "commit", "-m", msg], capture_output=True).returncode == 0:
        r = subprocess.run(["git", "push"], capture_output=True, text=True)
        print("push", "ok" if r.returncode == 0 else f"FAILED {r.stderr[-500:]}")


if __name__ == "__main__":
    if "--no-refresh" not in sys.argv:
        refresh()
    sanity_check()
    e = append(current_calls())
    for c in e["calls"]:
        print(f"{c['bottleneck']:7s} {c['state']:3s} (data {c['data_month']}, decided {c['decision_date']}, C={c['C']}, M={c['M']})")
    print("hash", e["hash"])
    if "--publish" in sys.argv:
        publish(e)
