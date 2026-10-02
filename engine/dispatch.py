"""Queue snapshot changes and retry durable alert jobs.

The SQLite cursor and event payloads are committed together before any network
delivery. Workers claim jobs atomically, release database locks before sending,
and recover abandoned claims after a lease. Confirmed successes are deduplicated.
Delivery is at least once: a crash after provider acceptance but before recording
success can cause a repeat because these providers do not share an idempotency key.
Historical failed ledger entries without a saved payload cannot be reconstructed.

The first snapshot records a baseline without alerting about history.
    python engine/dispatch.py [--digest] [--dry-run]
    python engine/dispatch.py --retry-only
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import datetime as dt
import json
import math
import os
import secrets
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
from app import db  # noqa: E402
import notify  # noqa: E402

SNAPSHOT = ROOT / "web" / "data" / "snapshot.json"
STATE = ROOT / "data" / "dispatch_state.json"
SITE = os.environ.get("LEADTIME_URL", "https://leadtime.benbox.dev").rstrip("/")
LEASE_SECONDS = 120
RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 3600
MAX_JOBS_PER_RUN = 100
DEFAULT_PREFS = {"themes": ["*"], "ev_calls": 1, "ev_discovery": 1, "ev_digest": 1,
                 "discovery_min_z": 3.0, "discovery_themed_only": 1}


def reading(value):
    return f"{value:+.2f}" if isinstance(value, (int, float)) and math.isfinite(value) else "unavailable"


def call_events(snap, state):
    out = []
    for t in snap["themes"]:
        if t.get("status") != "validated" or not t.get("call"):
            continue
        prev = state.get("calls", {}).get(t["slug"])
        if prev and prev != t["call"]:
            out.append({"key": f"call:{t['slug']}:{t['call']}:{t.get('decision_date') or snap['generated_at'][:10]}",
                        "theme": t["slug"], "msg": notify.Message(
                            title=f"{t['name']}: {prev} → {t['call']}",
                            body=(f"The model changed its {t['name']} call from {prev} to {t['call']}. "
                                  f"Pressure {reading(t.get('C'))}, momentum {reading(t.get('M'))}. Research, not advice."),
                            url=f"{SITE}/t/{t['slug']}", severity="change")})
    return out


def new_flags(snap, state):
    seen = state.get("flags_published_through", "")
    return [i for i in snap["discovery"]["items"] if i["published"] > seen]


def flag_line(i, names):
    th = ", ".join(names.get(s, s) for s in i["themes"][:2])
    th = f" [{th}]" if th else ""
    if i["kind"] == "company":
        return f"• {i['name']} ({i['ticker']}): sales +{i['yoy3']:.0f}% YoY, z {i['z']:.1f}{th}"
    src = "US imports" if i["source"] == "us_imports" else "Korea exports"
    desc = f" {i['desc'][:40]}" if i.get("desc") else ""
    return f"• {src} HS {i['hs']}{desc}: +{i['yoy3']:.0f}% YoY, z {i['z']:.1f}{th}"


def digest_msg(snap):
    val = [t for t in snap["themes"] if t.get("status") == "validated"]
    hot = sorted((t for t in snap["themes"] if t.get("C") is not None),
                 key=lambda t: -t["C"])[:3]
    lines = [f"• {t['name']}: {t['call']}" for t in val]
    lines.append("Tightest now: " + ", ".join(f"{t['name']} ({t['C']:+.1f})" for t in hot))
    n = sum(1 for i in snap["discovery"]["items"] if i["month"] == snap["discovery"]["latest_month"])
    lines.append(f"{n} discovery flags for {snap['discovery']['latest_month'][:7]}.")
    return notify.Message(title="Leadtime monthly digest", body="\n".join(lines), url=SITE, severity="info")


def wants_theme(prefs, themes):
    return prefs["themes"] == ["*"] or bool(set(prefs["themes"]) & set(themes))


def user_events(snap, calls, flags, prefs, digest):
    names = {t["slug"]: t["name"] for t in snap["themes"]}
    todo = []
    if prefs["ev_calls"]:
        todo += [(e["key"], e["msg"], True) for e in calls if wants_theme(prefs, [e["theme"]])]
    if prefs["ev_discovery"]:
        mine = [i for i in flags if i["z"] >= prefs["discovery_min_z"]
                and (i["themes"] or not prefs["discovery_themed_only"])
                and (not i["themes"] or wants_theme(prefs, i["themes"]))]
        if mine:
            mine.sort(key=lambda i: -i["z"])
            body = "\n".join(flag_line(i, names) for i in mine[:8])
            if len(mine) > 8:
                body += f"\n…and {len(mine) - 8} more."
            through = max(i["published"] for i in mine)
            todo.append((f"disc:{through}:{len(mine)}", notify.Message(
                title=f"{len(mine)} new discovery flag{'s' if len(mine) > 1 else ''}",
                body=body + "\nLeads to research, not buy signals.", url=f"{SITE}/discovery",
                severity="flag"), False))
    if digest and prefs["ev_digest"]:
        period = snap["discovery"]["latest_month"][:7]
        todo.append((f"digest:{period}", digest_msg(snap), False))
    return todo


def _has_table(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


def _load_state(con):
    if con is not None and _has_table(con, "dispatch_metadata"):
        row = con.execute("SELECT value FROM dispatch_metadata WHERE key='state'").fetchone()
        if row:
            return json.loads(row["value"]), False
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8")), False
    return {}, True


def _is_stale(snap, state):
    if not state.get("snapshot"):
        return False
    return dt.datetime.fromisoformat(snap["generated_at"]) < dt.datetime.fromisoformat(state["snapshot"])


def _next_state(snap, state):
    return {"calls": {t["slug"]: t["call"] for t in snap["themes"] if t.get("call")},
            "flags_published_through": max([state.get("flags_published_through", "")] +
                                           [i["published"] for i in snap["discovery"]["items"]]),
            "snapshot": snap["generated_at"]}


def _enqueue(con, ch, key, msg, now):
    if con.execute("SELECT 1 FROM deliveries WHERE channel_id=? AND event_key=? AND ok=1",
                   (ch["id"], key)).fetchone():
        return False
    result = con.execute(
        """INSERT INTO delivery_queue(channel_id,event_key,payload,created_at,next_attempt_at)
           VALUES (?,?,?,?,?) ON CONFLICT(channel_id,event_key) DO NOTHING""",
        (ch["id"], key, json.dumps(asdict(msg), ensure_ascii=False), now, now),
    )
    return result.rowcount == 1


def _channel_events(con, snap, calls, flags, digest, readonly=False):
    if con is None or not _has_table(con, "users"):
        return
    for u in con.execute("SELECT id FROM users").fetchall():
        if readonly:
            row = con.execute("SELECT * FROM prefs WHERE user_id=?", (u["id"],)).fetchone()
            prefs = dict(row) if row else dict(DEFAULT_PREFS)
            if row:
                prefs["themes"] = json.loads(prefs["themes"])
        else:
            prefs = db.get_prefs(con, u["id"])
        todo = user_events(snap, calls, flags, prefs, digest)
        for ch in con.execute("SELECT * FROM channels WHERE user_id=?", (u["id"],)).fetchall():
            for key, msg, is_call in todo:
                if ch["kind"] != "sms" or is_call:
                    yield ch, key, msg


def _mirror_state():
    # Serialize mirrors with ingestion and read the latest committed cursor.
    # SQLite is authoritative if a process dies before replacing the file.
    with db.tx() as con:
        con.execute("BEGIN IMMEDIATE")
        state, _ = _load_state(con)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        filename = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE.parent,
                                             prefix=".dispatch-state-", delete=False) as handle:
                filename = handle.name
                json.dump(state, handle, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(filename, STATE)
        finally:
            if filename and os.path.exists(filename):
                os.unlink(filename)


def ingest(snap, digest=False):
    """Commit payloads and cursor together; concurrent ingestion is serialized."""
    with db.tx() as con:
        con.execute("BEGIN IMMEDIATE")
        state, first = _load_state(con)
        stale = _is_stale(snap, state)
        calls = [] if stale else call_events(snap, state)
        flags = [] if stale else new_flags(snap, state)
        queued = 0
        if not first and not stale:
            for ch, key, msg in _channel_events(con, snap, calls, flags, digest):
                queued += int(_enqueue(con, ch, key, msg, db.now()))
        if not stale:
            state = _next_state(snap, state)
        con.execute("""INSERT INTO dispatch_metadata(key,value) VALUES ('state',?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (json.dumps(state),))
    try:
        _mirror_state()
    except OSError:
        print("state mirror could not be written; committed SQLite cursor remains authoritative")
    if first:
        print("first run: recording state, no historical alerts queued")
    if stale:
        print("older snapshot ignored; pending jobs can still be retried")
    return {"calls": len(calls), "flags": len(flags), "queued": queued}


def retry_delay(attempts):
    return min(MAX_RETRY_SECONDS, RETRY_SECONDS * (2 ** min(max(attempts - 1, 0), 10)))


def claim_next(now=None):
    """Only one worker can claim a job. A crashed worker's lease can expire."""
    now = db.now() if now is None else now
    token = secrets.token_hex(24)
    with db.tx() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("""DELETE FROM delivery_queue WHERE EXISTS (
                       SELECT 1 FROM deliveries d WHERE d.channel_id=delivery_queue.channel_id
                       AND d.event_key=delivery_queue.event_key AND d.ok=1)""")
        row = con.execute("""SELECT q.*,c.kind,c.target FROM delivery_queue q
                             JOIN channels c ON c.id=q.channel_id
                             WHERE q.next_attempt_at<=? AND (q.claim_token IS NULL OR q.lease_until<=?)
                             ORDER BY q.next_attempt_at,q.id LIMIT 1""", (now, now)).fetchone()
        if row is None:
            return None
        con.execute("""UPDATE delivery_queue SET claim_token=?,lease_until=?,attempts=attempts+1 WHERE id=?""",
                    (token, now + LEASE_SECONDS, row["id"]))
        job = dict(row)
        job.update(claim_token=token, lease_until=now + LEASE_SECONDS, attempts=row["attempts"] + 1)
    return job


@contextmanager
def _keep_claim_alive(job):
    stop = threading.Event()

    def renew():
        while not stop.wait(LEASE_SECONDS / 3):
            try:
                with db.tx() as con:
                    con.execute("""UPDATE delivery_queue SET lease_until=?
                                   WHERE id=? AND claim_token=?""",
                                (db.now() + LEASE_SECONDS, job["id"], job["claim_token"]))
            except sqlite3.Error:
                continue

    thread = threading.Thread(target=renew, name="leadtime-delivery-lease", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def finish_job(job, result, now=None):
    now = db.now() if now is None else now
    error = None if result.ok else (result.error or "failed")[:200]
    with db.tx() as con:
        con.execute("BEGIN IMMEDIATE")
        owned = con.execute("SELECT 1 FROM delivery_queue WHERE id=? AND claim_token=?",
                            (job["id"], job["claim_token"])).fetchone()
        if owned is None:
            return "obsolete"
        con.execute("""INSERT INTO deliveries(channel_id,event_key,sent_at,ok,error) VALUES (?,?,?,?,?)
                       ON CONFLICT(channel_id,event_key) DO UPDATE SET
                       sent_at=excluded.sent_at,ok=excluded.ok,error=excluded.error""",
                    (job["channel_id"], job["event_key"], now, int(result.ok), error))
        con.execute("""UPDATE channels SET last_ok_at=COALESCE(?,last_ok_at),last_error=? WHERE id=?""",
                    (now if result.ok else None, error, job["channel_id"]))
        if result.ok:
            con.execute("DELETE FROM delivery_queue WHERE id=?", (job["id"],))
        else:
            con.execute("""UPDATE delivery_queue SET claim_token=NULL,lease_until=NULL,
                           next_attempt_at=?,last_error=? WHERE id=?""",
                        (now + retry_delay(job["attempts"]), error, job["id"]))
    return "ok" if result.ok else "fail"


def work_pending(limit=MAX_JOBS_PER_RUN):
    """Retry saved payloads independently of the latest snapshot, outside DB locks."""
    stats = {"ok": 0, "fail": 0, "obsolete": 0}
    for _ in range(limit):
        job = claim_next()
        if job is None:
            break
        with _keep_claim_alive(job):
            try:
                msg = notify.Message(**json.loads(job["payload"]))
                result = notify.send(job["kind"], json.loads(job["target"]), msg)
            except Exception:
                result = notify.Result(False, "Delivery worker failed; retry scheduled.")
            outcome = finish_job(job, result)
        stats[outcome] += 1
    return stats


def preview(snap, digest=False):
    """Read-only preview: no DB initialization, migration, claims, sends or state writes."""
    con = None
    try:
        if Path(db.DB_PATH).exists():
            uri = Path(db.DB_PATH).resolve().as_uri() + "?mode=ro"
            con = sqlite3.connect(uri, uri=True)
            con.row_factory = sqlite3.Row
        state, first = _load_state(con)
        stale = _is_stale(snap, state)
        calls = [] if stale else call_events(snap, state)
        flags = [] if stale else new_flags(snap, state)
        if not first and not stale:
            for ch, key, msg in _channel_events(con, snap, calls, flags, digest, readonly=True):
                print(f"  [dry] ch{ch['id']} {ch['kind']} {key}: {msg.title}")
        if con is not None and _has_table(con, "delivery_queue"):
            count = con.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0]
            print(f"  [dry] {count} saved jobs; no claims or sends performed")
        return {"calls": len(calls), "flags": len(flags), "queued": 0}
    finally:
        if con is not None:
            con.close()


def dispatch_snapshot(snap, digest=False, dry_run=False):
    if dry_run:
        return preview(snap, digest)
    db.init()
    events = ingest(snap, digest)
    stats = work_pending()
    with db.tx() as con:
        legacy = con.execute("""SELECT COUNT(*) FROM deliveries d WHERE d.ok=0 AND NOT EXISTS (
                                SELECT 1 FROM delivery_queue q WHERE q.channel_id=d.channel_id
                                AND q.event_key=d.event_key)""").fetchone()[0]
    if legacy:
        print(f"{legacy} old failed ledger entries have no stored payload; historical alerts cannot be replayed automatically")
    print(f"call changes {events['calls']}, new flags {events['flags']}, queued {events['queued']}, deliveries {stats}")
    return {**events, **stats}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retry-only", action="store_true", help="retry saved jobs without reading a snapshot")
    args = ap.parse_args()
    if args.retry_only:
        if args.dry_run:
            print("  [dry] retry-only: no claims, sends, database or state writes")
        else:
            db.init()
            print(f"saved-job deliveries {work_pending()}")
        return
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    dispatch_snapshot(snap, digest=args.digest, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
