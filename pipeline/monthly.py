"""Monthly run: refresh data, compute the current signal for each bottleneck, and append it to the hash-chained log.

Scheduled for the 17th of each month (after the TW 10th deadline and the 16th decision date).
Each log entry stores the SHA-256 of the previous entry, so later edits to past calls are detectable.

Nothing is appended unless every refresh step succeeded, the data is as fresh as the calendar says it should
be, monthly's v4 memory index matches the site snapshot, and no entry exists yet for the same decision date
and model.

    python pipeline/monthly.py [--no-refresh] [--publish] [--allow-stale] [--force]

--allow-stale logs despite stale data and records "stale_override": true (plus what was stale) in the entry.
--force logs even if an entry for the same decision date and model exists, recording "forced_duplicate": true.
"""
import datetime as dt
import hashlib
import importlib.util
import json
import os
import subprocess
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
PY = sys.executable
LOG = "backtest/signal_log.jsonl"
SNAPSHOT = "web/data/snapshot.json"
PUBLISH_PATHS = [LOG, "data/processed", "data/context", "backtest", "config"]

# Months after the data month until a source is due (the decision date is the 16th of data month + lag):
# TW revenue M-1 by the 10th of M; Korea customs M-1 by about the 15th of M; US Census M-2.
SOURCE_LAG = {"tw_revenue": 1, "kr_exports": 1, "korea_mem_exports": 1, "us_imports": 2}
PARITY_TOL = 0.01


def refresh():
    """Run every fetch/build step; stop at the first failure so stale data is never logged."""
    # fetch_tw rewrites the whole CSV from its start year; old pages come from the cache, so always start at 2005
    steps = [[PY, "pipeline/fetch_tw.py", "2005"], [PY, "pipeline/fetch_market.py"],
             [PY, "pipeline/fetch_comtrade.py"], [PY, "pipeline/fetch_census.py"],
             [PY, "pipeline/fetch_korea_customs.py"], [PY, "pipeline/fetch_census_all.py"],
             [PY, "pipeline/fetch_universe_prices.py"], [PY, "pipeline/discovery.py"], [PY, "engine/build.py"]]
    for cmd in steps:
        name = " ".join(cmd[1:])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            sys.exit(f"ABORT: {name} timed out; nothing logged")
        if r.returncode != 0:
            print(f"{name} -> FAILED (exit {r.returncode})\n{r.stdout[-1000:]}\n{r.stderr[-2000:]}", flush=True)
            sys.exit(f"ABORT: refresh step {name} failed; nothing logged")
        print(name, "-> ok", flush=True)


def sanity_check():
    """Refuse to log if a refresh truncated history (the index needs years of data for its z-scores)."""
    tw = pd.read_csv("data/processed/tw_revenue.csv", usecols=["month"])
    first = pd.to_datetime(tw.month).min()
    if first > pd.Timestamp("2006-01-01"):
        sys.exit(f"ABORT: tw_revenue.csv starts {first.date()}; history was truncated, not logging")


# ---------------------------------------------------------------- model v4

def load_v4():
    """Import backtest/run_v4.py as a module (its __main__ block does not run). Paths in it are relative to ROOT."""
    spec = importlib.util.spec_from_file_location("run_v4", os.path.join(ROOT, "backtest", "run_v4.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def v4_rows(v4):
    """The live row of each v4 index, with the spec needed to turn it into a call."""
    mem, src, _ = v4.build_memory_v4()
    specs = [("memory", "v4", "ruleM", mem, "memory", None),
             ("optics", "v4-tilt", "ruleB", v4.build(v4.GROUPS["optics"]), "optics", "SOXX"),
             ("power", "v4-tilt", "ruleB", v4.build(v4.GROUPS["power"]), "power", "XLI")]
    return [(name, ver, rule, v4.live(ind), grp, bench, src) for name, ver, rule, ind, grp, bench in specs]


def current_calls(v4, rows):
    """Model v4 (backtest/PREREGISTRATION_v4.md): memory in/out; optics and power as tilts vs their benchmark."""
    calls = []
    for name, ver, rule, row, grp, bench, src in rows:
        basket = v4.GROUPS[grp]["basket"] + ([bench] if bench else [])
        px = v4.PX[basket].ffill().iloc[-1]
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


# ---------------------------------------------------------------- model v6

def v6_calls(snap):
    """Model v6 (backtest/PREREGISTRATION_v6_universe.md), read from the snapshot engine/build.py just wrote.
    Memory is skipped: v6 keeps the v4 Rule M, which is already logged above. Monitoring themes log NO CALL."""
    px = pd.read_csv("data/processed/universe_prices.csv.gz", index_col=0, parse_dates=True).ffill().iloc[-1]
    calls = []
    for t in snap["themes"]:
        if t["slug"] == "memory":
            continue
        tick = [b["ticker"] for b in t["basket"]] + [t["benchmark"]]
        calls.append({"bottleneck": t["slug"], "model": "v6", "rule": "tilt" if t["status"] == "validated" else "monitoring",
                      "state": t["call"] or "NO CALL", "data_month": t.get("data_month"), "decision_date": t.get("decision_date"),
                      "C": t["C"], "M": t["M"], "benchmark": t["benchmark"],
                      "basket_prices": {k: round(float(px[k]), 4) for k in tick if k in px and pd.notna(px[k])}})
    return calls


# ---------------------------------------------------------------- gates

def expected_month(today, lag):
    """Latest data month whose decision date (16th of data month + lag) is on or before `today`."""
    t = pd.Timestamp(today).normalize() - pd.Timedelta(days=15)
    return t.to_period("M").to_timestamp() - pd.DateOffset(months=lag)


def stale_report(snap, rows, today=None):
    """Everything older than the calendar says it should be. Empty list = fresh."""
    today = pd.Timestamp(today if today is not None else pd.Timestamp.today())
    out = []
    for t in snap["themes"]:
        comps = t.get("components") or []
        if not comps:  # basket-only theme: no physical series to date
            continue
        lag = max(SOURCE_LAG.get(c["key"], 1) for c in comps)
        want = expected_month(today, lag)
        dm = t.get("data_month")
        if dm is None or pd.Timestamp(dm) < want:
            out.append(f"v6 {t['slug']}: data_month {dm}, expected {want.date()}")
        for c in comps:
            w = expected_month(today, SOURCE_LAG.get(c["key"], 1))
            if c.get("latest_month") is None or pd.Timestamp(c["latest_month"]) < w:
                out.append(f"v6 {t['slug']}: {c['key']} latest {c.get('latest_month')}, expected {w.date()}")
    want = expected_month(today, 1)  # every v4 index is dated on the 16th of M+1
    for name, ver, rule, row, grp, bench, src in rows:
        if row.name < want:
            out.append(f"{ver} {name}: data_month {row.name.date()}, expected {want.date()}")
        for col in ("z_tw_revenue", "z_korea_mem_exports", "yoy_tw_revenue"):
            if col in row.index and pd.isna(row[col]):
                out.append(f"{ver} {name}: {col.split('_', 1)[1]} missing for {row.name.date()}")
    return out


def v4_warnings(rows):
    """Components that are not freshness-gated (FRED PPI timing is not pre-registered), reported only."""
    return [f"{ver} {name}: {col[4:]} missing for {row.name.date()}"
            for name, ver, rule, row, grp, bench, src in rows
            for col in row.index if col.startswith("yoy_PCU") and pd.isna(row[col])]


def parity_problems(mem_row, snap, tol=PARITY_TOL):
    """monthly's v4 memory index must equal the one engine/build.py published (snapshot rounds to 2 dp)."""
    t = next((t for t in snap["themes"] if t["slug"] == "memory"), None)
    if t is None:
        return ["snapshot has no memory theme"]
    out = []
    if t.get("data_month") != str(mem_row.name.date()):
        out.append(f"memory data_month: monthly {mem_row.name.date()} vs snapshot {t.get('data_month')}")
    for k in ("C", "M"):
        if t.get(k) is None or abs(float(mem_row[k]) - float(t[k])) > tol:
            out.append(f"memory {k}: monthly {float(mem_row[k]):.3f} vs snapshot {t.get(k)}")
    return out


def read_log(log=None):
    log = log or LOG
    if not os.path.exists(log):
        return []
    return [json.loads(l) for l in open(log) if l.strip()]


def duplicates(calls, log=None):
    """(model, decision_date) pairs in `calls` that an earlier entry already logged."""
    seen = {(c.get("model"), c.get("decision_date")) for e in read_log(log) for c in e.get("calls", [])}
    return sorted({(c["model"], c["decision_date"]) for c in calls
                   if c.get("decision_date") and (c["model"], c["decision_date"]) in seen})


# ---------------------------------------------------------------- log + publish

def append(calls, extra=None, log=None):
    log = log or LOG
    prev = "0" * 64
    if os.path.exists(log):
        lines = [l for l in open(log) if l.strip()]
        if lines:
            prev = json.loads(lines[-1])["hash"]
    entry = {"logged_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "prev": prev,
             "calls": calls, "note": "Model output, not investment advice."}
    entry.update(extra or {})
    body = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    entry["hash"] = hashlib.sha256(body.encode()).hexdigest()
    with open(log, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def publish(entry):
    """Commit the new log entry (and refreshed public data) and push; the push time is the public timestamp.
    Commits only PUBLISH_PATHS, whatever else is staged. Returns True if the push succeeded."""
    paths = [p for p in PUBLISH_PATHS if os.path.exists(p)]
    subprocess.run(["git", "add", "--", *paths], check=True)
    msg = f"Monthly signals {entry['logged_at'][:10]}: " + ", ".join(
        f"{c['bottleneck']} {c['state']}" for c in entry["calls"]) + f"\n\nentry hash {entry['hash']}"
    r = subprocess.run(["git", "commit", "-m", msg, "--", *paths], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"commit FAILED {r.stdout[-500:]} {r.stderr[-500:]}", flush=True)
        return False
    r = subprocess.run(["git", "push"], capture_output=True, text=True)
    print("push", "ok" if r.returncode == 0 else f"FAILED {r.stderr[-500:]}", flush=True)
    return r.returncode == 0


def main(argv):
    if "--no-refresh" not in argv:
        refresh()
    sanity_check()
    snap = json.load(open(SNAPSHOT))
    v4 = load_v4()
    rows = v4_rows(v4)

    bad = parity_problems(rows[0][3], snap)
    if bad:
        sys.exit("ABORT: v4 memory index differs from the snapshot engine/build.py wrote; nothing logged\n  "
                 + "\n  ".join(bad))

    calls = current_calls(v4, rows) + v6_calls(snap)
    extra = {}

    dup = duplicates(calls)
    if dup:
        msg = "entries already exist for " + ", ".join(f"{m} decision {d}" for m, d in dup)
        if "--force" not in argv:
            sys.exit(f"ABORT: {msg}; nothing logged (use --force to log again)")
        print(f"WARNING: {msg}; logging anyway (--force)", flush=True)
        extra["forced_duplicate"] = True

    for w in v4_warnings(rows):
        print("WARNING:", w, flush=True)
    stale = stale_report(snap, rows)
    if stale:
        if "--allow-stale" not in argv:
            sys.exit("ABORT: data is older than expected; nothing logged (use --allow-stale to override)\n  "
                     + "\n  ".join(stale))
        print("WARNING: logging stale data (--allow-stale):\n  " + "\n  ".join(stale), flush=True)
        extra.update(stale_override=True, stale=stale)

    e = append(calls, extra)
    for c in e["calls"]:
        print(f"{c['bottleneck']:7s} {c['state']:3s} (data {c['data_month']}, decided {c['decision_date']}, C={c['C']}, M={c['M']})")
    print("hash", e["hash"])
    if "--publish" in argv:
        pushed = publish(e)
        subprocess.run([PY, "engine/build.py"], check=True)  # pick up the new log entry
        subprocess.run([PY, "engine/dispatch.py", "--digest"], check=True)
        if not pushed:
            sys.exit("ERROR: entry logged locally but the publish commit/push failed")


if __name__ == "__main__":
    main(sys.argv[1:])
