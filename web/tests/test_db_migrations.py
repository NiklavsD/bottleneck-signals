"""Regression checks against the actual pre-session account schema."""
import sqlite3

import pytest

from app import db


LEGACY_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL, created_at INTEGER NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0);
CREATE TABLE channels (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, target TEXT NOT NULL, label TEXT, created_at INTEGER NOT NULL,
  last_ok_at INTEGER, last_error TEXT);
CREATE TABLE prefs (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  themes TEXT NOT NULL DEFAULT '["*"]', ev_calls INTEGER NOT NULL DEFAULT 1,
  ev_discovery INTEGER NOT NULL DEFAULT 1, ev_digest INTEGER NOT NULL DEFAULT 1,
  discovery_min_z REAL NOT NULL DEFAULT 3.0, discovery_themed_only INTEGER NOT NULL DEFAULT 1);
CREATE TABLE deliveries (id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
  event_key TEXT NOT NULL, sent_at INTEGER NOT NULL, ok INTEGER NOT NULL,
  error TEXT, UNIQUE(channel_id, event_key));
CREATE TABLE telegram_links (code TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE login_attempts (ip TEXT NOT NULL, at INTEGER NOT NULL);
"""


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "accounts.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    with sqlite3.connect(path) as con:
        con.executescript(LEGACY_SCHEMA)
        con.execute("INSERT INTO users VALUES (7, 'kept@example.com', 'original-hash', 100, 1)")
        con.execute("INSERT INTO channels VALUES (12, 7, 'ntfy', '{\"topic\":\"secret-topic\"}', 'Phone', 100, 150, NULL)")
        con.execute("INSERT INTO prefs(user_id, themes, ev_discovery, discovery_min_z) VALUES (7, '[\"memory\"]', 0, 4)")
        con.execute("INSERT INTO deliveries VALUES (1, 12, 'digest:2026-09', 150, 1, NULL)")
        con.execute("INSERT INTO deliveries VALUES (2, 12, 'call:failed', 160, 0, 'Delivery timed out.')")
        con.execute("INSERT INTO telegram_links VALUES ('valid-code', 7, 170)")
        # Old tables had no ownership constraints; preserve their ID high-water marks.
        con.execute("INSERT INTO channels VALUES (20, 99, 'ntfy', '{}', NULL, 100, NULL, NULL)")
        con.execute("INSERT INTO prefs(user_id) VALUES (99)")
        con.execute("INSERT INTO deliveries VALUES (3, 40, 'orphan', 150, 1, NULL)")
        con.execute("INSERT INTO telegram_links VALUES ('orphan-code', 99, 170)")
    return path


def test_upgrade_preserves_accounts_preferences_and_valid_delivery_history(legacy_db):
    db.init()
    with db.tx() as con:
        user = dict(con.execute("SELECT * FROM users").fetchone())
        assert user["id"] == 7 and user["pw_hash"] == "original-hash" and user["is_admin"] == 1
        assert len(user["account_token"]) >= 32
        channel = dict(con.execute("SELECT * FROM channels").fetchone())
        assert channel["id"] == 12 and channel["label"] == "Phone" and channel["last_ok_at"] == 150
        assert db.get_prefs(con, 7)["themes"] == ["memory"]
        assert db.get_prefs(con, 7)["ev_discovery"] == 0
        assert db.get_prefs(con, 7)["discovery_min_z"] == 4
        history = [tuple(r) for r in con.execute("SELECT id, event_key, ok, error FROM deliveries ORDER BY id")]
        assert history == [(1, "digest:2026-09", 1, None), (2, "call:failed", 0, "Delivery timed out.")]
        assert con.execute("SELECT COUNT(*) FROM telegram_links").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM prefs").fetchone()[0] == 1
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1


def test_upgrade_does_not_reuse_deleted_account_or_channel_ids(legacy_db):
    db.init()
    with db.tx() as con:
        con.execute("DELETE FROM users WHERE id=7")
        uid = con.execute("INSERT INTO users(email,pw_hash,created_at) VALUES ('new@example.com','hash',200)").lastrowid
        cid = con.execute("INSERT INTO channels(user_id,kind,target,created_at) VALUES (?, 'ntfy', '{}', 200)", (uid,)).lastrowid
        assert uid > 99 and cid > 40
        con.execute("DELETE FROM users WHERE id=?", (uid,))
        uid2 = con.execute("INSERT INTO users(email,pw_hash,created_at) VALUES ('next@example.com','hash',201)").lastrowid
        cid2 = con.execute("INSERT INTO channels(user_id,kind,target,created_at) VALUES (?, 'ntfy', '{}', 201)", (uid2,)).lastrowid
        assert uid2 > uid and cid2 > cid
        assert con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM telegram_links").fetchone()[0] == 0


def test_preupgrade_reused_ids_expire_stale_links_and_ledger(legacy_db):
    with sqlite3.connect(legacy_db) as con:
        # These IDs were reused by the old schema before the upgrade was deployed.
        con.execute("UPDATE users SET created_at=200 WHERE id=7")
        con.execute("UPDATE channels SET created_at=200 WHERE id=12")
        con.execute("INSERT INTO deliveries VALUES (4, 12, 'real-new-delivery', 250, 1, NULL)")
        con.execute("INSERT INTO telegram_links VALUES ('current-but-unbound-code', 7, 220)")
    db.init()
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM telegram_links").fetchone()[0] == 0
        assert [r[0] for r in con.execute("SELECT event_key FROM deliveries")] == ["real-new-delivery"]


def test_upgrade_is_idempotent_and_account_deletion_cascades_outbox_sessions(legacy_db):
    db.init()
    with db.tx() as con:
        token = con.execute("SELECT account_token FROM users WHERE id=7").fetchone()[0]
        con.execute("INSERT INTO telegram_links VALUES ('post-upgrade-code', 7, 200)")
        con.execute("INSERT INTO sessions VALUES ('session-hash', 7, ?, 100, 9999999999)", (token,))
        con.execute("INSERT INTO delivery_queue(channel_id,event_key,payload,created_at,next_attempt_at) VALUES (12, 'queued', '{}', 100, 100)")
        con.execute("INSERT INTO dispatch_metadata VALUES ('state', '{\"snapshot\":\"saved\"}')")
    db.init()
    with db.tx() as con:
        assert con.execute("SELECT account_token FROM users WHERE id=7").fetchone()[0] == token
        assert con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0] == 1
        assert con.execute("SELECT code FROM telegram_links").fetchone()[0] == "post-upgrade-code"
        con.execute("DELETE FROM users WHERE id=7")
        for table in ("channels", "prefs", "sessions", "telegram_links", "deliveries", "delivery_queue"):
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert con.execute("SELECT value FROM dispatch_metadata WHERE key='state'").fetchone()[0] == '{"snapshot":"saved"}'


def test_migration_failure_rolls_back_schema_and_rows(legacy_db, monkeypatch):
    def failed_token(_):
        raise RuntimeError("injected migration failure")
    monkeypatch.setattr(db.secrets, "token_urlsafe", failed_token)
    with pytest.raises(RuntimeError, match="injected"):
        db.init()
    with sqlite3.connect(legacy_db) as con:
        assert "account_token" not in {r[1] for r in con.execute("PRAGMA table_info(users)")}
        assert con.execute("SELECT pw_hash FROM users WHERE id=7").fetchone()[0] == "original-hash"
        assert con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 3
        assert not con.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_new'").fetchall()
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0


def test_upgrade_preserves_existing_autoincrement_high_water_marks(tmp_path, monkeypatch):
    path = tmp_path / "partially-upgraded.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    with sqlite3.connect(path) as con:
        con.executescript(LEGACY_SCHEMA.replace("id INTEGER PRIMARY KEY,", "id INTEGER PRIMARY KEY AUTOINCREMENT,"))
        con.execute("INSERT INTO users VALUES (7, 'kept@example.com', 'hash', 1, 0)")
        con.execute("INSERT INTO users VALUES (50, 'deleted@example.com', 'hash', 1, 0)")
        con.execute("DELETE FROM users WHERE id=50")
        con.execute("INSERT INTO channels VALUES (60, 7, 'ntfy', '{}', NULL, 1, NULL, NULL)")
        con.execute("DELETE FROM channels WHERE id=60")
    db.init()
    with db.tx() as con:
        uid = con.execute("INSERT INTO users(email,pw_hash,created_at) VALUES ('new@example.com','hash',2)").lastrowid
        cid = con.execute("INSERT INTO channels(user_id,kind,target,created_at) VALUES (?, 'ntfy', '{}', 2)", (uid,)).lastrowid
        assert uid > 50 and cid > 60


def test_fresh_database_has_random_account_identity_and_foreign_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "new.db"))
    db.init()
    with db.tx() as con:
        for email in ("a@example.com", "b@example.com"):
            con.execute("INSERT INTO users(email,pw_hash,created_at) VALUES (?, 'hash', 1)", (email,))
        tokens = [r[0] for r in con.execute("SELECT account_token FROM users")]
        assert tokens[0] != tokens[1] and all(len(t) >= 32 for t in tokens)
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO telegram_links VALUES ('no-user', 999, 1)")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO deliveries(channel_id,event_key,sent_at,ok) VALUES (999,'missing',1,1)")
