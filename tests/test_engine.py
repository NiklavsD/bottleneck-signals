"""Tests for engine/build.py (frozen v6 status, split-safe track record) and engine/dispatch.py (pending ledger).

No network: notify.send is replaced by fakes, databases and logs live in tmp_path.
"""
import hashlib
import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def build():
    return _load("leadtime_build", "engine/build.py")


@pytest.fixture(scope="module")
def dispatch_mod():
    return _load("leadtime_dispatch", "engine/dispatch.py")


# ---------- #3 frozen v6 validation ----------

def test_frozen_file_matches_hash_and_publication(build):
    frozen = build.load_frozen()
    validated = sorted(s for s, f in frozen.items() if f["status"] == "validated")
    assert validated == sorted(["lithography-fab-tools", "memory", "substrates-pcb", "connectors-cables",
                                "transformers", "switchgear-grid-equipment", "precision-motion-actuators"])
    sw = frozen["switchgear-grid-equipment"]["oos"]
    assert (sw["tilt_sharpe"], sw["always_sharpe"], sw["tilt_ann_pct"], sw["pass"]) == (0.02, -0.02, 0.3, True)
    assert frozen["photonics-optics"]["status"] == "monitoring"


def test_frozen_file_tamper_is_refused(build, tmp_path):
    for ext in (".json", ".sha256"):
        shutil.copy(ROOT / f"backtest/v6_frozen_results{ext}", tmp_path / f"v6{ext}")
    p = tmp_path / "v6.json"
    p.write_text(p.read_text().replace('"tilt_sharpe": 0.02', '"tilt_sharpe": 0.03'))
    with pytest.raises(SystemExit):
        build.load_frozen(str(p))


def test_v6_status_reads_frozen_and_drift_is_display_only(build):
    frozen = build.load_frozen()
    # switchgear passed by 0.02 vs -0.02; a recompute that now fails must not demote it
    status, oos, reason, drift = build.v6_status("switchgear-grid-equipment", frozen,
                                                 {"tilt_sharpe": -0.01, "always_sharpe": 0.0, "pass": False})
    assert status == "validated" and reason is None
    assert oos["tilt_sharpe"] == 0.02 and oos["pass"] is True
    assert drift["frozen_pass"] is True and drift["recomputed_pass"] is False
    # photonics failed by 0.34 vs 0.35; a recompute that now passes must not promote it
    status, oos, reason, drift = build.v6_status("photonics-optics", frozen, {"pass": True})
    assert status == "monitoring" and oos["pass"] is False and drift["recomputed_pass"] is True
    # agreement -> no drift
    assert build.v6_status("substrates-pcb", frozen, {"pass": True})[3] is None
    # a theme added after v6
    status, oos, reason, drift = build.v6_status("brand-new-theme", frozen, {"pass": True})
    assert status == "monitoring" and oos is None and build.NOT_PREREGISTERED in reason and drift is None


def test_theme_block_ignores_recomputed_oos(build, monkeypatch):
    frozen = build.load_frozen()
    theme = next(t for t in build.THEMES if t["slug"] == "switchgear-grid-equipment")
    fake = {"from": "2008-04-16", "to": "2017-02-28", "tilt_sharpe": -0.5, "always_sharpe": 0.5,
            "tilt_ann_pct": -1.0, "always_ann_pct": 1.0, "pass": False}
    monkeypatch.setattr(build, "tilt_test", lambda *a, **k: dict(fake))
    build.DRIFT.clear()
    out = build.theme_block(theme, frozen)
    assert out["status"] == "validated" and out["call"] in ("OVERWEIGHT", "UNDERWEIGHT")
    assert out["test"]["oos"]["tilt_sharpe"] == 0.02
    assert "descriptive" in out["test"]["since_2016"]["label"]
    assert [d["slug"] for d in build.DRIFT] == ["switchgear-grid-equipment"]

    build.DRIFT.clear()
    new = dict(theme, slug="switchgear-copy")
    out = build.theme_block(new, frozen)
    assert out["status"] == "monitoring" and out["call"] is None
    assert build.NOT_PREREGISTERED in out["reason"] and build.DRIFT == []
    build.DRIFT.clear()


# ---------- #4 split-safe track record ----------

def _px():
    days = pd.bdate_range("2026-09-01", "2026-09-30")
    # SPLT: 2:1 split on 15 Sep. Raw closes 100 before, 50 after, ending at 55; the adjusted series halves
    # every pre-split close, so it reads 50 ... 50 ... 55.
    splt = pd.Series(50.0, index=days)
    splt.iloc[-1] = 55.0
    ben = pd.Series(100.0, index=days)
    ben.iloc[-1] = 101.0
    new = pd.Series(float("nan"), index=days)
    new[days >= "2026-09-20"] = 20.0
    new.iloc[-1] = 22.0
    return pd.DataFrame({"SPLT": splt, "BEN": ben, "NEW": new})


def test_track_record_is_split_safe_and_reports_missing(build, tmp_path):
    px = _px()
    call = {"bottleneck": "x", "model": "v6", "state": "OVERWEIGHT", "C": 1.0, "M": 0.1, "benchmark": "BEN",
            "basket_prices": {"SPLT": 100.0, "GONE": 10.0, "BEN": 100.0}}  # stored SPLT is the raw pre-split close
    r = build.call_returns(call, "2026-09-10T16:00:00+00:00", px)
    assert r["basket_since_pct"] == 10.0  # not 55/100 - 1 = -45%
    assert r["bench_since_pct"] == 1.0 and r["excess_pct"] == 9.0 and r["call_return_pct"] == 9.0
    assert r["missing"] == ["GONE"] and r["stored_price_fallback"] == []

    under = build.call_returns(dict(call, state="UNDERWEIGHT"), "2026-09-10T16:00:00+00:00", px)
    assert under["call_return_pct"] == -9.0

    timing = {"bottleneck": "m", "model": "v4", "state": "OUT", "C": 1.0, "M": -0.1,
              "basket_prices": {"SPLT": 100.0}}
    t = build.call_returns(timing, "2026-09-10T16:00:00+00:00", px)
    assert t["bench"] is None and t["excess_pct"] is None and t["call_return_pct"] == -10.0

    # NEW has no close before the call: falls back to the stored price and is flagged
    fb = build.call_returns(dict(call, basket_prices={"NEW": 20.0, "BEN": 100.0}), "2026-09-10T16:00:00+00:00", px)
    assert fb["basket_since_pct"] == 10.0 and fb["stored_price_fallback"] == ["NEW"]

    # end-to-end through the log reader, with a valid hash chain
    entry = {"logged_at": "2026-09-10T16:00:00+00:00", "prev": "0" * 64, "calls": [call], "note": "t"}
    entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    log = tmp_path / "signal_log.jsonl"
    log.write_text(json.dumps(entry, sort_keys=True) + "\n")
    tr = build.track_record(str(log), px)
    row = tr["rows"][0]
    assert tr["chain_ok"] and row["basket_since_pct"] == 10.0 and row["bench"] == "BEN"
    assert {"basket_since_pct", "bench", "bench_since_pct", "excess_pct", "call_return_pct", "missing"} <= set(row)


def test_entry_close_uses_last_close_before_log_day(build):
    s = pd.Series([1.0, 2.0, 3.0], index=pd.to_datetime(["2026-09-08", "2026-09-09", "2026-09-10"]))
    assert build.entry_close(s, "2026-09-10T02:10:00+00:00") == 2.0  # the 10 Sep close didn't exist yet


# ---------- #5 dispatch ledger ----------

class Crash(BaseException):
    """Simulates the process dying mid-send (not caught by dispatch)."""


@pytest.fixture
def env(dispatch_mod, tmp_path, monkeypatch):
    d = dispatch_mod
    dbp = tmp_path / "leadtime.db"
    monkeypatch.setattr(d.db, "DB_PATH", str(dbp))
    snap = {"generated_at": "2026-10-02T00:00:00+00:00",
            "themes": [{"slug": "memory", "name": "Memory", "status": "validated", "call": "OUT",
                        "decision_date": "2026-09-16", "C": 5.0, "M": -0.2}],
            "discovery": {"items": [], "latest_month": "2026-08-01"}}
    (tmp_path / "snapshot.json").write_text(json.dumps(snap))
    state = tmp_path / "dispatch_state.json"
    state.write_text(json.dumps({"calls": {"memory": "IN"}, "flags_published_through": "2026-09-30"}))
    monkeypatch.setattr(d, "SNAPSHOT", tmp_path / "snapshot.json")
    monkeypatch.setattr(d, "STATE", state)
    d.db.init()
    con = sqlite3.connect(dbp)
    con.execute("INSERT INTO users(id, email, pw_hash, created_at) VALUES (1, 'a@example.com', 'x', 0)")
    for cid in (1, 2, 3):
        con.execute("INSERT INTO channels(id, user_id, kind, target, created_at) VALUES (?, 1, 'webhook', '{}', 0)",
                    (cid,))
    con.execute("INSERT INTO deliveries(channel_id, event_key, sent_at, ok, error) VALUES (1, 'old:event', 1, 1, NULL)")
    con.commit()
    con.close()
    return d, dbp, state


def _ledger(dbp):
    con = sqlite3.connect(dbp)
    try:
        return {(c, k): ok for c, k, ok in con.execute("SELECT channel_id, event_key, ok FROM deliveries")}
    finally:
        con.close()


def test_crash_leaves_pending_row_and_rerun_does_not_double_send(env, monkeypatch, capsys):
    d, dbp, state = env
    state_before = state.read_text()
    sent = []

    def crashing_send(kind, target, msg):
        if len(sent) == 1:
            sent.append("crash")
            raise Crash()
        sent.append("ok")
        return _ok_result(d)

    monkeypatch.setattr(d.notify, "send", crashing_send)
    with pytest.raises(Crash):
        d.main([])
    key = "call:memory:OUT:2026-09-16"
    led = _ledger(dbp)
    assert led[(1, key)] == 1 and led[(2, key)] is None and (3, key) not in led
    assert led[(1, "old:event")] == 1  # rows survive the ok-nullable migration
    assert state.read_text() == state_before  # crash before the state write

    calls = []

    def good_send(kind, target, msg):
        calls.append(msg.title)
        return _ok_result(d)

    monkeypatch.setattr(d.notify, "send", good_send)
    d.main([])
    out = capsys.readouterr().out
    assert len(calls) == 1  # only channel 3; channel 1 already sent, channel 2 pending
    assert "pending from previous run, skipped" in out
    led = _ledger(dbp)
    assert led[(3, key)] == 1 and led[(2, key)] is None
    con = sqlite3.connect(dbp)
    notnull = {r[1]: r[3] for r in con.execute("PRAGMA table_info(deliveries)")}
    con.close()
    assert notnull["ok"] == 0


def test_send_exception_is_recorded_not_resent(env, monkeypatch):
    d, dbp, state = env
    n = []

    def boom(kind, target, msg):
        n.append(1)
        raise RuntimeError("socket died")

    monkeypatch.setattr(d.notify, "send", boom)
    d.main([])
    assert len(n) == 3
    assert all(v == 0 for (c, k), v in _ledger(dbp).items() if k.startswith("call:"))
    state.write_text(json.dumps({"calls": {"memory": "IN"}, "flags_published_through": "2026-09-30"}))
    d.main([])
    assert len(n) == 3


def test_dispatch_does_not_block_concurrent_writer(env, monkeypatch):
    d, dbp, _ = env
    wrote = []

    def send_and_write(kind, target, msg):
        other = sqlite3.connect(dbp, timeout=0.2)
        try:
            other.execute("INSERT INTO login_attempts(ip, at) VALUES ('1.2.3.4', 1)")
            other.commit()
            wrote.append(True)
        finally:
            other.close()
        return _ok_result(d)

    monkeypatch.setattr(d.notify, "send", send_and_write)
    d.main([])
    assert wrote == [True, True, True]


def test_unverified_channels_are_skipped(env, monkeypatch):
    d, dbp, _ = env
    con = sqlite3.connect(dbp)
    if "verified" not in {r[1] for r in con.execute("PRAGMA table_info(channels)")}:  # older web schema
        con.execute("ALTER TABLE channels ADD COLUMN verified INTEGER NOT NULL DEFAULT 1")
    con.execute("UPDATE channels SET verified=0 WHERE id=2")
    con.commit()
    con.close()
    got = []
    monkeypatch.setattr(d.notify, "send", lambda kind, target, msg: got.append(1) or _ok_result(d))
    d.main([])
    assert len(got) == 2
    assert (2, "call:memory:OUT:2026-09-16") not in _ledger(dbp)


def test_channel_active_without_verified_column(dispatch_mod):
    assert dispatch_mod.channel_active({"id": 1, "kind": "webhook"})
    assert dispatch_mod.channel_active({"id": 1, "verified": 1})
    assert not dispatch_mod.channel_active({"id": 1, "verified": 0})


def test_dry_run_writes_nothing(env, monkeypatch, capsys):
    d, dbp, state = env
    before, state_before = _ledger(dbp), state.read_text()
    monkeypatch.setattr(d.notify, "send", lambda *a: pytest.fail("dry run must not send"))
    d.main(["--dry-run"])
    assert "[dry]" in capsys.readouterr().out
    assert _ledger(dbp) == before and state.read_text() == state_before


def _ok_result(d):
    return d.notify.Result(ok=True)
