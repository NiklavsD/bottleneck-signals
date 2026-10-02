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
import uuid
import math
from pathlib import Path
from contextlib import contextmanager

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PY = sys.executable
LOG = "backtest/signal_log.jsonl"
PRICE_MAX_AGE_DAYS = 7


@contextmanager
def monthly_lock(root=None):
    """One refresh/log/publication transaction at a time, on Windows and Linux."""
    path = Path(root or ROOT) / "data/monthly.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("ABORT: another monthly run holds the publication lock") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def checked_run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, cwd=ROOT)
    if r.returncode:
        raise RuntimeError(f"ABORT: {' '.join(cmd[1:])} failed ({r.returncode}): {r.stderr[-2000:]}")
    print(" ".join(cmd[1:]), "-> ok", flush=True)
    return r


def build_command(run_id, output):
    return [PY, "engine/build.py", "--run-id", run_id, "--output", str(output), "--include-pending"]


def pending_publication():
    path = ROOT / "data/monthly_pending.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_publication_state(state):
    path = ROOT / "data/monthly_pending.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, path)


def entry_publication_state(entry):
    write_publication_state({"phase": "entry", "hash": entry["hash"], "period": entry["period"]})


def call_fingerprint(rows):
    return [{**{k: v for k, v in call.items() if k != "basket_prices"},
             "basket_tickers": sorted(call.get("basket_prices", {}))} for call in rows]


def refresh(run_id, output):
    # fetch_tw rewrites the whole CSV from its start year; old pages come from the cache, so always start at 2005
    steps = [[PY, "pipeline/fetch_tw.py", "2005"], [PY, "pipeline/fetch_market.py"],
             [PY, "pipeline/fetch_comtrade.py"], [PY, "pipeline/fetch_census.py"],
             [PY, "pipeline/fetch_korea_customs.py"], [PY, "pipeline/fetch_census_all.py"],
             [PY, "pipeline/fetch_universe_prices.py"], [PY, "pipeline/discovery.py"],
             build_command(run_id, output)]
    for cmd in steps:
        checked_run(cmd)


def sanity_check(now=None):
    """Reject truncated history or physical/price data older than the monthly decision needs."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today = pd.Timestamp(now.date())
    decision_month = today.replace(day=1) - pd.offsets.MonthBegin(1) if today.day < 16 else today.replace(day=1)
    tw = pd.read_csv(ROOT / "data/processed/tw_revenue.csv", usecols=["month"])
    first = pd.to_datetime(tw.month).min()
    if pd.isna(first) or first > pd.Timestamp("2006-01-01"):
        raise RuntimeError(f"ABORT: tw_revenue.csv history is empty or truncated ({first})")
    for filename, lag in (("tw_revenue.csv", 1), ("korea_customs_exports.csv", 1), ("us_imports_all.csv", 2)):
        months = pd.to_datetime(pd.read_csv(ROOT / "data/processed" / filename, usecols=["month"]).month)
        latest = months.max()
        needed = decision_month - pd.offsets.MonthBegin(lag)
        if pd.isna(latest) or latest < needed or latest >= today.replace(day=1):
            raise RuntimeError(f"ABORT: {filename} covers {latest}; need completed month {needed.date()}")
    for filename in ("prices.csv", "universe_prices.csv.gz"):
        frame = pd.read_csv(ROOT / "data/processed" / filename, index_col=0, parse_dates=True)
        actual = frame.dropna(how="all")
        if actual.empty or actual.index.max() < today - pd.Timedelta(days=PRICE_MAX_AGE_DAYS) or actual.index.max() > today:
            raise RuntimeError(f"ABORT: {filename} has no recent, nonfuture price observations")


def read_snapshot(run_id, started_at, now=None, path=None):
    """Only the snapshot generated for this invocation may supply the logged v6 calls."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with open(path or ROOT / "web/data/snapshot.json", encoding="utf-8") as handle:
        snap = json.load(handle)
    generated = dt.datetime.fromisoformat(snap["generated_at"])
    if generated.tzinfo is None or snap.get("run_id") != run_id:
        raise RuntimeError("ABORT: snapshot is not from this monthly run")
    if not started_at.replace(microsecond=0) <= generated <= now:
        raise RuntimeError("ABORT: snapshot timestamp is stale or in the future")
    return snap


def basket_prices(frame, tickers, now=None):
    """Reject missing or stale constituent prices rather than indefinitely forward-filling them."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = pd.Timestamp(now.date()) - pd.Timedelta(days=PRICE_MAX_AGE_DAYS)
    prices = {}
    for ticker in tickers:
        series = frame[ticker].dropna() if ticker in frame else pd.Series(dtype=float)
        if series.empty or series.index[-1] < cutoff or series.index[-1] > pd.Timestamp(now.date()):
            raise RuntimeError(f"ABORT: missing, stale or future price for {ticker}")
        price = float(series.iloc[-1])
        if not math.isfinite(price) or price <= 0:
            raise RuntimeError(f"ABORT: nonpositive or nonfinite price for {ticker}")
        prices[ticker] = round(price, 4)
    return prices


def current_calls(now=None):
    """Model v4 (backtest/PREREGISTRATION_v4.md): memory in/out; optics and power as tilts vs their benchmark."""
    from backtest import run_v4 as model
    model.load_data()
    mem, src, _ = model.build_memory_v4()
    specs = [("memory", "v4", "ruleM", mem, "memory", None),
             ("optics", "v4-tilt", "ruleB", model.build(model.GROUPS["optics"]), "optics", "SOXX"),
             ("power", "v4-tilt", "ruleB", model.build(model.GROUPS["power"]), "power", "XLI")]
    calls = []
    for name, ver, rule, ind, grp, bench in specs:
        row = model.live(ind)
        if name == "memory" and (not math.isfinite(float(row.M)) or not math.isfinite(float(row.C))):
            raise RuntimeError("ABORT: memory pressure or like-for-like momentum is unavailable")
        basket = model.GROUPS[grp]["basket"] + ([bench] if bench else [])
        px = basket_prices(model.PX, basket, now)
        if bench:
            state = "OVERWEIGHT" if row[rule] else "UNDERWEIGHT"
        else:
            state = "IN" if row[rule] else "OUT"
        call = {"bottleneck": name, "model": ver, "rule": rule, "state": state,
                "data_month": str(row.name.date()), "decision_date": str(row.decision.date()),
                "C": round(float(row.C), 3), "M": round(float(row.M), 3) if math.isfinite(float(row.M)) else None,
                "basket_prices": px}
        if bench:
            call["benchmark"] = bench
        if name == "memory":
            call["korea_source"] = src
        calls.append(call)
    return calls


def v6_calls(snap, now=None):
    """Model v6 (backtest/PREREGISTRATION_v6_universe.md), read from the snapshot engine/build.py just wrote.
    Memory is skipped: v6 keeps the v4 Rule M, which is already logged above. Monitoring themes log NO CALL."""
    px = pd.read_csv(ROOT / "data/processed/universe_prices.csv.gz", index_col=0, parse_dates=True)
    calls = []
    for t in snap["themes"]:
        if t["slug"] == "memory":
            continue
        tick = [b["ticker"] for b in t["basket"]] + [t["benchmark"]]
        calls.append({"bottleneck": t["slug"], "model": "v6", "rule": "tilt" if t["status"] == "validated" else "monitoring",
                      "state": t["call"] or "NO CALL", "data_month": t.get("data_month"), "decision_date": t.get("decision_date"),
                      "source_components": [component["key"] for component in t.get("components", [])],
                      "C": t["C"], "M": t["M"], "benchmark": t["benchmark"],
                      "basket_prices": basket_prices(px, tick, now)})
    return calls


def validate_calls(calls, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    today = pd.Timestamp(now.date())
    month = today.replace(day=1) - pd.offsets.MonthBegin(1) if today.day < 16 else today.replace(day=1)
    latest_decision = month + pd.Timedelta(days=15)
    for call in calls:
        if call["state"] == "NO CALL":
            continue
        decision = pd.Timestamp(call.get("decision_date"))
        if pd.isna(decision) or decision < latest_decision or decision > today:
            raise RuntimeError(f"ABORT: {call['bottleneck']} call uses a stale or future decision ({decision})")


def validate_source_freshness(calls, now):
    from pipeline._freshness import validate_active_sources
    validate_active_sources(ROOT, calls, now)


def verified_entries():
    """Read a complete valid chain before appending or recovering publication state."""
    prev = "0" * 64
    entries = []
    if os.path.exists(LOG):
        with open(LOG) as handle:
            entries = [json.loads(l) for l in handle if l.strip()]
        for old in entries:
            body = {k: v for k, v in old.items() if k != "hash"}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if old.get("prev") != prev or old.get("hash") != digest:
                raise RuntimeError("ABORT: existing signal log chain is broken")
            prev = old["hash"]
    return entries


def recover_orphan_marker():
    pending = pending_publication()
    if not pending:
        return None
    entries = verified_entries()
    if pending.get("phase") == "refresh":
        # A crash after append/fsync but before the marker transition must reuse that entry.
        if entries and entries[-1]["hash"] != pending.get("initial_head") and entries[-1].get("period") == pending["period"]:
            entry_publication_state(entries[-1])
            return pending_publication()
    elif not any(entry["hash"] == pending.get("hash") for entry in entries):
        # Keep builders blocked: a legacy pre-append marker cannot establish input consistency.
        write_publication_state({"phase": "refresh", "period": pending["period"],
                                 "initial_head": entries[-1]["hash"] if entries else "0" * 64})
        return pending_publication()
    return pending


def complete_publication(entry):
    pending = pending_publication()
    if pending and pending.get("hash") == entry["hash"]:
        (ROOT / "data/monthly_pending.json").unlink()


def append(calls, period=None):
    entries = verified_entries()
    prev = entries[-1]["hash"] if entries else "0" * 64
    if period is not None:
        matching = [entry for entry in entries if entry.get("period") == period]
        if matching:
            old = matching[-1]
            if call_fingerprint(old["calls"]) != call_fingerprint(calls):
                raise RuntimeError("ABORT: this period already has different recorded calls; existing entry was preserved")
            entry_publication_state(old)
            return old
    entry = {"logged_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "prev": prev,
             "calls": calls, "note": "Model output, not investment advice."}
    if period is not None:
        entry["period"] = period
    body = json.dumps(entry, sort_keys=True, separators=(",", ":"), allow_nan=False)
    entry["hash"] = hashlib.sha256(body.encode()).hexdigest()
    if period is not None:
        pending = pending_publication()
        if pending and (pending.get("phase") != "refresh" or pending["period"] != period):
            raise RuntimeError("ABORT: a previous entry still awaits publication; no new entry was appended")
        if not pending:
            write_publication_state({"phase": "refresh", "period": period, "initial_head": prev})
    with open(LOG, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    if period is not None:
        entry_publication_state(entry)
    return entry


def publish(entry):
    """Commit the new log entry (and refreshed public data) and push; the push time is the public timestamp."""
    paths = [LOG, "data/processed", "backtest", "config"]
    subprocess.run(["git", "add", *paths], check=True)
    msg = f"Monthly signals {entry['logged_at'][:10]}: " + ", ".join(
        f"{c['bottleneck']} {c['state']}" for c in entry["calls"]) + f"\n\nentry hash {entry['hash']}"
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
    if staged.returncode == 1:
        subprocess.run(["git", "commit", "-m", msg], capture_output=True, text=True, check=True)
    elif staged.returncode != 0:
        raise RuntimeError("ABORT: cannot inspect staged publication changes")
    subprocess.run(["git", "push"], capture_output=True, text=True, check=True)
    print("push ok")


def run_monthly(args):
    os.chdir(ROOT)
    run_id = str(uuid.uuid4())
    started_at = dt.datetime.now(dt.timezone.utc)
    month = pd.Timestamp(started_at.date()).replace(day=1)
    if started_at.day < 16:
        month -= pd.offsets.MonthBegin(1)
    pending = recover_orphan_marker()
    if pending and (pending.get("phase", "entry") == "entry" or pending.get("entry_hash")) and pending["period"] != month.strftime("%Y-%m"):
        raise RuntimeError(f"ABORT: period {pending['period']} still awaits publication; resolve it before refreshing a new period")
    if pending and pending.get("phase") == "refresh" and "--no-refresh" in args:
        raise RuntimeError("ABORT: the interrupted refresh must be rerun completely; --no-refresh cannot resume it")
    entries = verified_entries()
    state = {"phase": "refresh", "period": month.strftime("%Y-%m"), "run_id": run_id,
             "initial_head": entries[-1]["hash"] if entries else "0" * 64}
    if pending and (pending.get("hash") or pending.get("entry_hash")):
        state["entry_hash"] = pending.get("hash") or pending["entry_hash"]
    write_publication_state(state)
    candidate = ROOT / "web/data" / f"snapshot.monthly-{run_id}.json"
    try:
        if "--no-refresh" not in args:
            refresh(run_id, candidate)
        else:
            checked_run(build_command(run_id, candidate))
        now = dt.datetime.now(dt.timezone.utc)
        sanity_check(now)
        snap = read_snapshot(run_id, started_at, now, candidate)
        calls = current_calls(now) + v6_calls(snap, now)
        validate_calls(calls, now)
        validate_source_freshness(calls, now)
        month = pd.Timestamp(now.date()).replace(day=1)
        if now.day < 16:
            month -= pd.offsets.MonthBegin(1)
        e = append(calls, period=month.strftime("%Y-%m"))
        for c in e["calls"]:
            print(f"{c['bottleneck']:7s} {c['state']:3s} (data {c['data_month']}, decided {c['decision_date']}, C={c['C']}, M={c['M']})")
        print("hash", e["hash"])
        # Build the updated track record in staging. A build or push failure leaves the public snapshot intact.
        checked_run(build_command(run_id, candidate))
        read_snapshot(run_id, started_at, path=candidate)
        if "--publish" in args:
            publish(e)
        if "--publish" in args:
            os.replace(candidate, ROOT / "web/data/snapshot.json")
            complete_publication(e)
            checked_run([PY, "engine/dispatch.py", "--digest"])
        return e
    finally:
        if candidate.exists():
            candidate.unlink()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    with monthly_lock():
        return run_monthly(args)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
