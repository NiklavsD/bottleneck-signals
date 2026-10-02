"""SQLite storage for accounts, alert channels, preferences and the delivery ledger."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("LEADTIME_DB", os.path.join(os.path.dirname(__file__), "..", "data", "leadtime.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
  session_ver INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, target TEXT NOT NULL, label TEXT, created_at INTEGER NOT NULL,
  last_ok_at INTEGER, last_error TEXT,
  verified INTEGER NOT NULL DEFAULT 1, verify_hash TEXT, verify_sent_at INTEGER,
  verify_tries INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS prefs (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  themes TEXT NOT NULL DEFAULT '["*"]', ev_calls INTEGER NOT NULL DEFAULT 1,
  ev_discovery INTEGER NOT NULL DEFAULT 1, ev_digest INTEGER NOT NULL DEFAULT 1,
  discovery_min_z REAL NOT NULL DEFAULT 3.0, discovery_themed_only INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, event_key TEXT NOT NULL, sent_at INTEGER NOT NULL,
  ok INTEGER NOT NULL, error TEXT, UNIQUE(channel_id, event_key)
);
CREATE TABLE IF NOT EXISTS telegram_links (
  code TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rate_events (kind TEXT NOT NULL, key TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS rate_events_kind_key_at ON rate_events(kind, key, at);
"""
# Column notes
# users.id           AUTOINCREMENT, so a deleted account's id is never reused by a new signup.
# users.session_ver  copied into the signed session cookie; bumping it (logout, account delete)
#                    invalidates every outstanding cookie for that user on every device.
# channels.verified  1 = may receive real alerts. Email/SMS channels start at 0 and stay 0 until
#                    the user enters the code from the test message on /alerts; discord/slack/ntfy/
#                    webhook/telegram start at 1. engine/dispatch.py must skip verified=0 channels.
# channels.verify_hash / verify_sent_at / verify_tries  HMAC of the pending code, when it was sent,
#                    and wrong guesses so far (codes expire after 1h or 5 wrong tries).
# rate_events        generic sliding-window rate-limit log: (kind, key, unix time); pruned on use.
VERIFY_KINDS = ("email", "sms")

_USERS_REBUILD = """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
CREATE TABLE users_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
  session_ver INTEGER NOT NULL DEFAULT 0
);
INSERT INTO users_new(id, email, pw_hash, created_at, is_admin, session_ver)
  SELECT id, email, pw_hash, created_at, is_admin, {session_ver} FROM users;
DROP TABLE users;
ALTER TABLE users_new RENAME TO users;
COMMIT;
PRAGMA foreign_keys = ON;
"""


def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _columns(con, table):
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def init():
    """Create tables and bring an older database up to the current schema. Safe to run repeatedly."""
    with tx() as con:
        con.executescript(SCHEMA)
        users_sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
        if "AUTOINCREMENT" not in users_sql.upper():
            # Table rebuild (SQLite cannot add AUTOINCREMENT in place). Ids are preserved.
            sv = "session_ver" if "session_ver" in _columns(con, "users") else "0"
            con.executescript(_USERS_REBUILD.format(session_ver=sv))
            bad = con.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise RuntimeError(f"users migration left {len(bad)} dangling foreign keys")
        if "session_ver" not in _columns(con, "users"):
            con.execute("ALTER TABLE users ADD COLUMN session_ver INTEGER NOT NULL DEFAULT 0")
        cols = _columns(con, "channels")
        if "verified" not in cols:
            con.execute("ALTER TABLE channels ADD COLUMN verified INTEGER NOT NULL DEFAULT 1")
            con.execute("UPDATE channels SET verified=0 WHERE kind IN ('email', 'sms')")
        for name, decl in (("verify_hash", "TEXT"), ("verify_sent_at", "INTEGER"),
                           ("verify_tries", "INTEGER NOT NULL DEFAULT 0")):
            if name not in cols:
                con.execute(f"ALTER TABLE channels ADD COLUMN {name} {decl}")


def now():
    return int(time.time())


def get_prefs(con, user_id):
    row = con.execute("SELECT * FROM prefs WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        con.execute("INSERT INTO prefs(user_id) VALUES (?)", (user_id,))
        row = con.execute("SELECT * FROM prefs WHERE user_id=?", (user_id,)).fetchone()
    p = dict(row)
    p["themes"] = json.loads(p["themes"])
    return p
