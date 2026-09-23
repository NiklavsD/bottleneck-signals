"""Send alerts for what changed in the latest snapshot.

Compares web/data/snapshot.json with the state saved at the last run (data/dispatch_state.json):
  - call changes on validated themes        -> "change" alerts
  - discovery flags published since last run -> one "flag" alert per channel, filtered by the user's prefs
  - --digest                                 -> one monthly summary

Every (channel, event_key) is recorded in the deliveries table, so re-running never double-sends.
The first run only records state; it doesn't alert anyone about history.

    python engine/dispatch.py [--digest] [--dry-run]
"""
import argparse
import json
import os
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


def deliver(con, ch, key, msg, dry):
    if con.execute("SELECT 1 FROM deliveries WHERE channel_id=? AND event_key=?", (ch["id"], key)).fetchone():
        return "skip"
    if dry:
        print(f"  [dry] ch{ch['id']} {ch['kind']} {key}: {msg.title}")
        return "dry"
    res = notify.send(ch["kind"], json.loads(ch["target"]), msg)
    con.execute("INSERT INTO deliveries(channel_id, event_key, sent_at, ok, error) VALUES (?,?,?,?,?)",
                (ch["id"], key, db.now(), int(res.ok), None if res.ok else (res.error or "failed")[:200]))
    con.execute("UPDATE channels SET last_ok_at=COALESCE(?, last_ok_at), last_error=? WHERE id=?",
                (db.now() if res.ok else None, None if res.ok else (res.error or "failed")[:200], ch["id"]))
    return "ok" if res.ok else "fail"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    snap = json.loads(SNAPSHOT.read_text())
    names = {t["slug"]: t["name"] for t in snap["themes"]}
    first = not STATE.exists()
    state = {} if first else json.loads(STATE.read_text())
    calls = call_events(snap, state)
    flags = new_flags(snap, state)
    period = snap["discovery"]["latest_month"][:7]
    stats = {"ok": 0, "fail": 0, "skip": 0, "dry": 0}
    if first:
        print("first run: recording state, no alerts sent")
    else:
        db.init()
        with db.tx() as con:
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
                    for key, msg, is_call in todo:
                        if ch["kind"] == "sms" and not is_call:
                            continue  # SMS carries call changes only
                        stats[deliver(con, ch, key, msg, a.dry_run)] += 1
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
