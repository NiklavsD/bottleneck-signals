"""SQLite storage for accounts, alert channels, preferences and the delivery ledger."""
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("LEADTIME_DB", os.path.join(os.path.dirname(__file__), "..", "data", "leadtime.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
  account_token TEXT NOT NULL DEFAULT (lower(hex(randomblob(32))))
);
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
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
  id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  event_key TEXT NOT NULL, sent_at INTEGER NOT NULL,
  ok INTEGER NOT NULL, error TEXT, UNIQUE(channel_id, event_key)
);
CREATE TABLE IF NOT EXISTS telegram_links (
  code TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  account_token TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS channels_user ON channels(user_id);
CREATE INDEX IF NOT EXISTS telegram_links_user ON telegram_links(user_id);
CREATE TABLE IF NOT EXISTS delivery_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  event_key TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL,
  next_attempt_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  claim_token TEXT, lease_until INTEGER, last_error TEXT, UNIQUE(channel_id, event_key)
);
CREATE TABLE IF NOT EXISTS dispatch_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def init():
    """Upgrade the original account database without changing IDs or valid ledger rows.

    Rebuilding the parent tables needs foreign keys disabled before the transaction.
    BEGIN IMMEDIATE serializes simultaneous app/dispatcher starts; every schema and
    data change commits together, after a complete foreign-key check.
    """
    con = connect()
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute("BEGIN IMMEDIATE")
        tables = {r[0]: r[1] for r in con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'")}
        if "users" in tables and ("account_token" not in {
                r[1] for r in con.execute("PRAGMA table_info(users)")}
                or "AUTOINCREMENT" not in tables["users"].upper()
                or "AUTOINCREMENT" not in tables.get("channels", "").upper()):
            _migrate_accounts(con, tables)
        # executescript implicitly commits; individual statements keep this atomic.
        for statement in SCHEMA.split(";"):
            if statement.strip():
                con.execute(statement)
        con.execute("DELETE FROM prefs WHERE user_id NOT IN (SELECT id FROM users)")
        con.execute("DELETE FROM sessions WHERE user_id NOT IN (SELECT id FROM users)")
        con.execute("DELETE FROM delivery_queue WHERE channel_id NOT IN (SELECT id FROM channels)")
        if con.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("Account migration failed its foreign-key check")
        con.execute("PRAGMA user_version = 1")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def _migrate_accounts(con, tables):
    # Keep the known historical maxima, including orphan links and ledger rows.
    user_max = con.execute("SELECT COALESCE(MAX(id), 0) FROM users").fetchone()[0]
    channel_max = con.execute("SELECT COALESCE(MAX(id), 0) FROM channels").fetchone()[0]
    for table, column in (("telegram_links", "user_id"), ("channels", "user_id"),
                          ("prefs", "user_id"), ("sessions", "user_id")):
        if table in tables:
            user_max = max(user_max, con.execute(
                f"SELECT COALESCE(MAX({column}), 0) FROM {table}").fetchone()[0])
    for table in ("deliveries", "delivery_queue"):
        if table in tables:
            channel_max = max(channel_max, con.execute(
                f"SELECT COALESCE(MAX(channel_id), 0) FROM {table}").fetchone()[0])
    if "sqlite_sequence" in tables:
        for name, seq in con.execute("SELECT name, seq FROM sqlite_sequence"):
            if name == "users":
                user_max = max(user_max, seq)
            elif name == "channels":
                channel_max = max(channel_max, seq)
    for table in ("users", "channels", "deliveries", "telegram_links"):
        definition = next(s for s in SCHEMA.split(";")
                          if s.strip().startswith(f"CREATE TABLE IF NOT EXISTS {table} ("))
        con.execute(definition.replace(f"IF NOT EXISTS {table}", f"{table}_new", 1))
    for row in con.execute("SELECT * FROM users").fetchall():
        token = row["account_token"] if "account_token" in row.keys() else secrets.token_urlsafe(32)
        con.execute("INSERT INTO users_new VALUES (?, ?, ?, ?, ?, ?)",
                    (row["id"], row["email"], row["pw_hash"], row["created_at"], row["is_admin"], token))
    con.execute("""INSERT INTO channels_new SELECT * FROM channels
                   WHERE user_id IN (SELECT id FROM users_new)""")
    if "deliveries" in tables:
        con.execute("""INSERT INTO deliveries_new SELECT d.* FROM deliveries d
                       JOIN channels_new c ON c.id=d.channel_id WHERE d.sent_at>=c.created_at""")
    # Legacy codes carry no account identity. A deleted user's ID may already have
    # been reused before this upgrade, so even apparently valid codes must expire.
    # Existing Telegram channels stay connected; users can request a fresh code.
    for table in ("deliveries", "telegram_links", "channels", "users"):
        if table in tables:
            con.execute(f"DROP TABLE {table}")
        con.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    for table, highest in (("users", user_max), ("channels", channel_max)):
        con.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        con.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, highest))


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
