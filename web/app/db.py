"""SQLite storage for accounts, alert channels, preferences and the delivery ledger."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("LEADTIME_DB", os.path.join(os.path.dirname(__file__), "..", "data", "leadtime.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, target TEXT NOT NULL, label TEXT, created_at INTEGER NOT NULL,
  last_ok_at INTEGER, last_error TEXT
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


def init():
    with tx() as con:
        con.executescript(SCHEMA)


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
