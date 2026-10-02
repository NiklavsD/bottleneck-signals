"""Send alerts for what changed in the latest snapshot.

Compares web/data/snapshot.json with the state saved at the last run (data/dispatch_state.json):
  - call changes on validated themes        -> "change" alerts
  - discovery flags published since last run -> one "flag" alert per channel, filtered by the user's prefs
  - --digest                                 -> one monthly summary

Every (channel, event_key) is recorded in the deliveries table, so re-running never double-sends:
a "pending" row (ok IS NULL) is committed before the send and finalised in its own short transaction after it.
No transaction is held across a network send, so the website can keep writing meanwhile. A pending row left by
a crash is never re-sent automatically (the message may already have gone out); it is logged for a manual check.
Channels with verified = 0 are skipped (the column is optional).
The first run only records state; it doesn't alert anyone about history.

    python engine/dispatch.py [--digest] [--dry-run]
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
from app import db  # noqa: E402
import notify  # noqa: E402

SNAPSHOT = ROOT / "web" / "data" / "snapshot.json"
STATE = ROOT / "data" / "dispatch_state.json"
SITE = os.environ.get("LEADTIME_URL", "https://leadtime.benbox.dev").rstrip("/")


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
                                  f"Pressure {t.get('C', 0):+.2f}, momentum {t.get('M', 0):+.2f}. Research, not advice."),
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
    hot = sorted(snap["themes"], key=lambda t: -(t.get("C") or -99))[:3]
    lines = [f"• {t['name']}: {t['call']}" for t in val]
    lines.append("Tightest now: " + ", ".join(f"{t['name']} ({t.get('C', 0):+.1f})" for t in hot))
    n = sum(1 for i in snap["discovery"]["items"] if i["month"] == snap["discovery"]["latest_month"])
    lines.append(f"{n} discovery flags for {snap['discovery']['latest_month'][:7]}.")
    return notify.Message(title="Leadtime monthly digest", body="\n".join(lines), url=SITE, severity="info")


def wants_theme(prefs, themes):
    return prefs["themes"] == ["*"] or bool(set(prefs["themes"]) & set(themes))


def ensure_deliveries_ok_nullable(con):
    """Idempotent migration: make deliveries.ok nullable so ok IS NULL can mean "pending".

    web/app/db.py (owned by the web app) creates deliveries with `ok INTEGER NOT NULL`; dispatch needs a pending
    state, so it relaxes the constraint here rather than in the shared schema. SQLite cannot drop NOT NULL in
    place, so the table is rebuilt from its own CREATE statement with only that constraint removed, all rows
    and any extra columns/indexes preserved. No-op once ok is nullable (or if the schema already says so)."""
    cols = con.execute("PRAGMA table_info(deliveries)").fetchall()
    if not any(c[1] == "ok" and c[3] for c in cols):
        return False
    sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='deliveries'").fetchone()[0]
    new_sql, n = re.subn(r"(\bok\s+INTEGER)\s+NOT\s+NULL", r"\1", sql, count=1, flags=re.I)
    if n != 1:
        raise RuntimeError(f"cannot relax deliveries.ok NOT NULL in: {sql}")
    extra = [r[0] for r in con.execute("SELECT sql FROM sqlite_master WHERE tbl_name='deliveries' "
                                       "AND type IN ('index','trigger') AND sql IS NOT NULL")]
    names = ", ".join(f'"{c[1]}"' for c in cols)
    con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        if not any(c[1] == "ok" and c[3] for c in con.execute("PRAGMA table_info(deliveries)")):
            con.rollback()  # another process migrated it meanwhile
            return False
        con.execute(re.sub(r"(?i)CREATE TABLE\s+(IF NOT EXISTS\s+)?\"?deliveries\"?", "CREATE TABLE deliveries_mig",
                           new_sql, count=1))
        con.execute(f"INSERT INTO deliveries_mig ({names}) SELECT {names} FROM deliveries")
        con.execute("DROP TABLE deliveries")
        con.execute("ALTER TABLE deliveries_mig RENAME TO deliveries")
        for x in extra:
            con.execute(x)
        con.commit()
    except Exception:
        con.rollback()
        raise
    return True


def channel_active(ch):
    """Skip channels whose owner hasn't verified them yet; older schemas without `verified` count as active."""
    return "verified" not in ch.keys() or ch["verified"] is None or bool(ch["verified"])


def deliver(ch, key, msg, dry):
    """Send one (channel, event_key) at most once. Each DB step is its own short transaction; the send is not."""
    if dry:
        con = db.connect()
        try:
            row = con.execute("SELECT ok FROM deliveries WHERE channel_id=? AND event_key=?", (ch["id"], key)).fetchone()
        finally:
            con.close()
        if row is not None:
            return "pending" if row["ok"] is None else "skip"
        print(f"  [dry] ch{ch['id']} {ch['kind']} {key}: {msg.title}")
        return "dry"
    with db.tx() as con:  # claim: a pending row (ok NULL), committed before anything leaves the machine
        claimed = con.execute("INSERT OR IGNORE INTO deliveries(channel_id, event_key, sent_at, ok, error) "
                              "VALUES (?,?,?,NULL,NULL)", (ch["id"], key, db.now())).rowcount == 1
        row = None if claimed else con.execute("SELECT ok FROM deliveries WHERE channel_id=? AND event_key=?",
                                               (ch["id"], key)).fetchone()
    if not claimed:
        if row is not None and row["ok"] is None:
            print(f"  ch{ch['id']} {key}: pending from previous run, skipped — check manually")
            return "pending"
        return "skip"
    try:
        res = notify.send(ch["kind"], json.loads(ch["target"]), msg)
        ok, err = bool(res.ok), None if res.ok else (res.error or "failed")[:200]
    except Exception as e:  # notify.send returns failures as data; this is a last resort
        ok, err = False, f"send raised {type(e).__name__}"
    with db.tx() as con:
        con.execute("UPDATE deliveries SET ok=?, error=?, sent_at=? WHERE channel_id=? AND event_key=?",
                    (int(ok), err, db.now(), ch["id"], key))
        con.execute("UPDATE channels SET last_ok_at=COALESCE(?, last_ok_at), last_error=? WHERE id=?",
                    (db.now() if ok else None, err, ch["id"]))
    return "ok" if ok else "fail"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    snap = json.loads(SNAPSHOT.read_text())
    names = {t["slug"]: t["name"] for t in snap["themes"]}
    first = not STATE.exists()
    state = {} if first else json.loads(STATE.read_text())
    calls = call_events(snap, state)
    flags = new_flags(snap, state)
    period = snap["discovery"]["latest_month"][:7]
    stats = {"ok": 0, "fail": 0, "skip": 0, "dry": 0, "pending": 0, "unverified": 0}
    if first:
        print("first run: recording state, no alerts sent")
    else:
        db.init()
        if not a.dry_run:  # a dry run only reads the ledger, so it leaves the schema alone
            con = db.connect()
            try:
                ensure_deliveries_ok_nullable(con)
            finally:
                con.close()
        jobs = []
        with db.tx() as con:  # short: read users, prefs and channels (get_prefs may insert defaults)
            users = con.execute("SELECT id FROM users").fetchall()
            for u in users:
                p = db.get_prefs(con, u["id"])
                chans = con.execute("SELECT * FROM channels WHERE user_id=?", (u["id"],)).fetchall()
                if not chans:
                    continue
                todo = []
                if p["ev_calls"]:
                    todo += [(e["key"], e["msg"], True) for e in calls if wants_theme(p, [e["theme"]])]
                if p["ev_discovery"]:
                    mine = [i for i in flags if i["z"] >= p["discovery_min_z"]
                            and (i["themes"] or not p["discovery_themed_only"])
                            and (not i["themes"] or wants_theme(p, i["themes"]))]
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
                if a.digest and p["ev_digest"]:
                    todo.append((f"digest:{period}", digest_msg(snap), False))
                for ch in chans:
                    if not channel_active(ch):
                        stats["unverified"] += 1
                        continue
                    for key, msg, is_call in todo:
                        if ch["kind"] == "sms" and not is_call:
                            continue  # SMS carries call changes only
                        jobs.append((dict(ch), key, msg))
        for ch, key, msg in jobs:  # outside any transaction: each delivery commits on its own
            stats[deliver(ch, key, msg, a.dry_run)] += 1
    if not a.dry_run:
        state = {"calls": {t["slug"]: t["call"] for t in snap["themes"] if t.get("call")},
                 "flags_published_through": max([state.get("flags_published_through", "")] +
                                                [i["published"] for i in snap["discovery"]["items"]]),
                 "snapshot": snap["generated_at"]}
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    print(f"call changes {len(calls)}, new flags {len(flags)}, deliveries {stats}")


if __name__ == "__main__":
    main()
