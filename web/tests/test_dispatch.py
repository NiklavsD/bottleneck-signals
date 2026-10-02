from concurrent.futures import ThreadPoolExecutor
import copy
import importlib.util
import json
from pathlib import Path
import threading
import sys

import pytest

from app import db
import notify

SPEC = importlib.util.spec_from_file_location("leadtime_dispatch", Path(__file__).resolve().parents[2] / "engine" / "dispatch.py")
dispatch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch)


def snapshot(date="2026-10-16", call="OUT", flags=None):
    return {"generated_at": date + "T12:00:00+00:00",
            "themes": [{"slug": "memory", "name": "Memory", "status": "validated",
                        "call": call, "C": 2.5, "M": -0.5, "decision_date": date}],
            "discovery": {"latest_month": date[:7] + "-01", "items": flags or []}}


def flag():
    return {"published": "2026-10-16", "month": "2026-10-01", "kind": "company",
            "name": "Example", "ticker": "EXAMPLE", "themes": ["memory"], "yoy3": 20.0, "z": 4.0}


@pytest.fixture
def isolated_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "accounts.sqlite"))
    monkeypatch.setattr(dispatch, "STATE", tmp_path / "state.json")
    clock = [1000]
    monkeypatch.setattr(db, "now", lambda: clock[0])
    return clock


def channel():
    db.init()
    with db.tx() as con:
        uid = con.execute("INSERT INTO users(email,pw_hash,created_at) VALUES ('test@example.test','unused',0)").lastrowid
        cid = con.execute("INSERT INTO channels(user_id,kind,target,created_at) VALUES (?,'webhook',?,0)",
                          (uid, json.dumps({"url": "https://hooks.example.test"}))).lastrowid
        return dict(con.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone())


def queue_count():
    with db.tx() as con:
        return con.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0]


def test_retry_preserves_payload_after_cursor_and_snapshot_advance(isolated_dispatch, monkeypatch):
    channel()
    dispatch.ingest(snapshot())
    changed = snapshot("2026-10-17", "IN", [flag()])
    assert dispatch.ingest(changed)["queued"] == 2
    attempted = []
    def fail(kind, target, msg):
        attempted.append(msg)
        return notify.Result(False, "Delivery timed out.")
    monkeypatch.setattr(notify, "send", fail)
    assert dispatch.work_pending()["fail"] == 2
    original = attempted[:]
    assert queue_count() == 2
    newer = snapshot("2026-11-17", "IN")
    newer["themes"][0]["C"] = 99.0
    assert dispatch.ingest(newer)["queued"] == 0
    assert dispatch.work_pending()["fail"] == 0
    isolated_dispatch[0] += dispatch.RETRY_SECONDS
    monkeypatch.setattr(notify, "send", lambda kind, target, msg: (attempted.append(msg) or notify.Result(True)))
    assert dispatch.work_pending()["ok"] == 2
    assert attempted[2:] == original
    assert queue_count() == 0
    assert dispatch.work_pending()["ok"] == 0
    with db.tx() as con:
        assert {r[0] for r in con.execute("SELECT ok FROM deliveries")} == {1}


def test_failed_ingestion_does_not_advance_cursor_or_file(isolated_dispatch, monkeypatch):
    channel()
    dispatch.ingest(snapshot())
    before = dispatch.STATE.read_bytes()
    enqueue = dispatch._enqueue
    def crash_after_enqueue(*args):
        enqueue(*args)
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(dispatch, "_enqueue", crash_after_enqueue)
    with pytest.raises(RuntimeError):
        dispatch.ingest(snapshot("2026-10-17", "IN"))
    assert dispatch.STATE.read_bytes() == before
    assert queue_count() == 0
    with db.tx() as con:
        state, _ = dispatch._load_state(con)
        assert state["calls"]["memory"] == "OUT"


def test_atomic_claim_and_abandoned_lease_recovery(isolated_dispatch):
    channel()
    dispatch.ingest(snapshot())
    dispatch.ingest(snapshot("2026-10-17", "IN"))
    barrier = threading.Barrier(2)
    def claim():
        barrier.wait()
        return dispatch.claim_next()
    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = list(workers.map(lambda _: claim(), range(2)))
    first = next(job for job in jobs if job is not None)
    assert sum(job is not None for job in jobs) == 1
    isolated_dispatch[0] += dispatch.LEASE_SECONDS
    recovered = dispatch.claim_next()
    assert recovered["id"] == first["id"] and recovered["attempts"] == 2
    assert recovered["claim_token"] != first["claim_token"]
    assert dispatch.finish_job(first, notify.Result(True)) == "obsolete"
    assert queue_count() == 1
    assert dispatch.finish_job(recovered, notify.Result(True)) == "ok"


def test_two_workers_send_once_and_network_holds_no_db_lock(isolated_dispatch, monkeypatch):
    channel()
    dispatch.ingest(snapshot())
    dispatch.ingest(snapshot("2026-10-17", "IN"))
    entered, release = threading.Event(), threading.Event()
    sends = []
    def slow_send(kind, target, msg):
        # A separate write succeeds while the provider operation is in progress.
        with db.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO dispatch_metadata(key,value) VALUES ('probe','ok')")
        sends.append(msg)
        entered.set()
        assert release.wait(5)
        return notify.Result(True)
    monkeypatch.setattr(notify, "send", slow_send)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(dispatch.work_pending)
        assert entered.wait(5)
        second = workers.submit(dispatch.work_pending)
        assert second.result(timeout=5)["ok"] == 0
        release.set()
        assert first.result(timeout=5)["ok"] == 1
    assert len(sends) == 1


def test_dry_run_creates_nothing_without_database(isolated_dispatch, monkeypatch):
    monkeypatch.setattr(notify, "send", lambda *args: pytest.fail("dry run sent a message"))
    dispatch.dispatch_snapshot(snapshot(), digest=True, dry_run=True)
    assert not Path(db.DB_PATH).exists()
    assert not dispatch.STATE.exists()


def test_dry_run_leaves_existing_queue_cursor_and_database_unchanged(isolated_dispatch, monkeypatch):
    channel()
    dispatch.ingest(snapshot())
    dispatch.ingest(snapshot("2026-10-17", "IN"))
    before = Path(db.DB_PATH).read_bytes(), dispatch.STATE.read_bytes()
    monkeypatch.setattr(notify, "send", lambda *args: pytest.fail("dry run sent a message"))
    dispatch.dispatch_snapshot(snapshot("2026-11-17", "OUT"), digest=True, dry_run=True)
    assert before == (Path(db.DB_PATH).read_bytes(), dispatch.STATE.read_bytes())
    with db.tx() as con:
        row = con.execute("SELECT attempts,claim_token FROM delivery_queue").fetchone()
        assert tuple(row) == (0, None)


def test_legacy_failure_without_payload_is_not_invented_or_replayed(isolated_dispatch, monkeypatch, capsys):
    ch = channel()
    dispatch.ingest(snapshot())
    with db.tx() as con:
        con.execute("INSERT INTO deliveries(channel_id,event_key,sent_at,ok,error) VALUES (?,'old-event',0,0,'timeout')",
                    (ch["id"],))
    monkeypatch.setattr(notify, "send", lambda *args: pytest.fail("invented historical message"))
    dispatch.dispatch_snapshot(snapshot())
    assert "no stored payload" in capsys.readouterr().out
    assert queue_count() == 0


def test_successful_ledger_deduplicates_but_failure_can_queue_real_payload(isolated_dispatch):
    ch = channel()
    msg = notify.Message("Call", "Saved body")
    with db.tx() as con:
        con.execute("INSERT INTO deliveries(channel_id,event_key,sent_at,ok) VALUES (?,'event',0,0)", (ch["id"],))
        assert dispatch._enqueue(con, ch, "event", msg, db.now())
    job = dispatch.claim_next()
    dispatch.finish_job(job, notify.Result(True))
    with db.tx() as con:
        assert not dispatch._enqueue(con, ch, "event", msg, db.now())
    assert queue_count() == 0


def test_database_cursor_survives_missing_or_stale_state_mirror(isolated_dispatch):
    channel()
    dispatch.ingest(snapshot())
    dispatch.ingest(snapshot("2026-10-17", "IN"))
    dispatch.STATE.unlink()
    assert dispatch.ingest(snapshot("2026-10-16", "OUT"))["queued"] == 0
    assert json.loads(dispatch.STATE.read_text())["calls"]["memory"] == "IN"
    assert queue_count() == 1


def test_concurrent_ingest_queues_each_event_once(isolated_dispatch):
    channel()
    dispatch.ingest(snapshot())
    changed = snapshot("2026-10-17", "IN", [flag()])
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(dispatch.ingest, [changed, copy.deepcopy(changed)]))
    assert sum(item["queued"] for item in results) == 2
    assert queue_count() == 2
    with db.tx() as con:
        state, _ = dispatch._load_state(con)
    assert json.loads(dispatch.STATE.read_text()) == state


def test_retry_backoff_is_bounded():
    assert dispatch.retry_delay(1) == 60
    assert dispatch.retry_delay(2) == 120
    assert dispatch.retry_delay(10000) == dispatch.MAX_RETRY_SECONDS


def test_tilt_call_without_momentum_still_queues_and_delivers(isolated_dispatch, monkeypatch):
    channel()
    baseline = snapshot(call="UNDERWEIGHT")
    baseline["themes"][0].update(slug="optics", name="Optics")
    dispatch.ingest(baseline)
    changed = copy.deepcopy(baseline)
    changed["generated_at"] = "2026-10-17T12:00:00+00:00"
    changed["themes"][0]["call"] = "OVERWEIGHT"
    changed["themes"][0]["M"] = None
    assert dispatch.ingest(changed)["queued"] == 1
    sent = []
    monkeypatch.setattr(notify, "send", lambda kind, target, msg: (sent.append(msg) or notify.Result(True)))
    assert dispatch.work_pending()["ok"] == 1
    assert "momentum unavailable" in sent[0].body


@pytest.mark.parametrize("corrupt", [False, True])
def test_retry_only_cli_never_loads_snapshot(isolated_dispatch, monkeypatch, tmp_path, corrupt):
    channel()
    dispatch.ingest(snapshot())
    dispatch.ingest(snapshot("2026-10-17", "IN"))
    missing = tmp_path / "missing-snapshot.json"
    if corrupt:
        missing.write_text("not valid JSON")
    monkeypatch.setattr(dispatch, "SNAPSHOT", missing)
    monkeypatch.setattr(sys, "argv", ["dispatch.py", "--retry-only"])
    sent = []
    monkeypatch.setattr(notify, "send", lambda kind, target, msg: (sent.append(msg) or notify.Result(True)))
    dispatch.main()
    assert len(sent) == 1 and queue_count() == 0


def test_retry_only_dry_run_creates_nothing(isolated_dispatch, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["dispatch.py", "--retry-only", "--dry-run"])
    monkeypatch.setattr(notify, "send", lambda *args: pytest.fail("dry-run sent"))
    dispatch.main()
    assert not Path(db.DB_PATH).exists() and not dispatch.STATE.exists()
