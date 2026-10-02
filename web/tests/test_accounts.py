"""Account/session and Telegram endpoint regressions, with isolated storage."""
import asyncio
import hashlib
import json
import threading

from argon2 import PasswordHasher
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
import pytest

from app import db, main


PASSWORD = "long-enough-password"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "accounts.db"))
    monkeypatch.setattr(main, "ph", PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
    monkeypatch.setattr(main, "TELEGRAM_BOT", "")
    monkeypatch.setattr(main, "TELEGRAM_HOOK_SECRET", "")
    monkeypatch.setattr(main, "snap", lambda: {
        "track": {"repo": "https://github.com/example/repo"}, "themes": [], "universes": [],
        "by_slug": {}, "generated_at": "2026-10-02T12:00:00Z", "prices_asof": "2026-10-01",
    })
    with TestClient(main.app, base_url="https://testserver", follow_redirects=False) as instance:
        yield instance


def signup(client, email="first@example.com"):
    assert client.get("/signup").status_code == 200
    response = client.post("/signup", data={"email": email, "password": PASSWORD,
                                          "csrf": client.cookies["lt_csrf"]})
    assert response.status_code == 303
    with db.tx() as con:
        return con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]


def replay(client, cookie):
    client.cookies.clear()
    client.cookies.set(main.COOKIE, cookie)
    return client.get("/alerts")


def test_deleted_account_session_cannot_access_replacement_account(client):
    old_id = signup(client)
    old_cookie = client.cookies[main.COOKIE]
    assert client.get("/alerts").status_code == 200
    deleted = client.post("/account/delete", data={"confirm": "first@example.com", "csrf": client.cookies["lt_csrf"]})
    assert deleted.status_code == 303
    new_id = signup(client, "replacement@example.com")
    assert new_id > old_id
    response = replay(client, old_cookie)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    with db.tx() as con:
        assert con.execute("SELECT email FROM users WHERE id=?", (new_id,)).fetchone()[0] == "replacement@example.com"
        assert con.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (old_id,)).fetchone()[0] == 0


@pytest.mark.parametrize("payload", [{"uid": 1}, [], {"sid": "x", "account_token": "y"}, {"sid": 1, "account_token": None}])
def test_legacy_and_malformed_signed_cookies_are_rejected(client, payload):
    signup(client)
    response = replay(client, main.signer.dumps(payload))
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_logout_revokes_replayed_cookie_but_retains_other_login(client):
    uid = signup(client)
    original = client.cookies[main.COOKIE]
    second_response = main.login_response(uid)
    second = second_response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    assert client.post("/logout", data={"csrf": client.cookies["lt_csrf"]}).status_code == 303
    assert replay(client, original).headers["location"] == "/login"
    assert replay(client, second).status_code == 200
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_cookie_requires_account_token_and_unexpired_server_session(client):
    uid = signup(client)
    cookie = client.cookies[main.COOKIE]
    data = main.signer.loads(cookie)
    session_id = hashlib.sha256(data["sid"].encode()).hexdigest()
    with db.tx() as con:
        stored = con.execute("SELECT id FROM sessions WHERE user_id=?", (uid,)).fetchone()[0]
        assert stored == session_id and stored != data["sid"]
        con.execute("UPDATE users SET account_token='new-account-token' WHERE id=?", (uid,))
    assert replay(client, cookie).headers["location"] == "/login"
    with db.tx() as con:
        con.execute("UPDATE users SET account_token=? WHERE id=?", (data["account_token"], uid))
        con.execute("UPDATE sessions SET expires_at=? WHERE id=?", (db.now() - 1, session_id))
    assert replay(client, cookie).headers["location"] == "/login"


def test_invalid_csrf_cannot_revoke_session_or_delete_account(client):
    signup(client)
    assert client.post("/logout", data={"csrf": "bad"}).status_code == 400
    assert client.post("/account/delete", data={"csrf": "bad", "confirm": "first@example.com"}).status_code == 400
    assert client.get("/alerts").status_code == 200


def test_channel_deletion_cascades_ledger_and_pending_delivery(client):
    uid = signup(client)
    with db.tx() as con:
        cid = con.execute("INSERT INTO channels(user_id,kind,target,created_at) VALUES (?, 'ntfy', '{}', 1)", (uid,)).lastrowid
        con.execute("INSERT INTO deliveries(channel_id,event_key,sent_at,ok) VALUES (?, 'sent', 1, 1)", (cid,))
        con.execute("INSERT INTO delivery_queue(channel_id,event_key,payload,created_at,next_attempt_at) VALUES (?, 'pending', '{}', 1, 1)", (cid,))
    assert client.post(f"/alerts/channel/{cid}/delete", data={"csrf": client.cookies["lt_csrf"]}).status_code == 303
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0] == 0
        next_id = con.execute("INSERT INTO channels(user_id,kind,target,created_at) VALUES (?, 'ntfy', '{}', 1)", (uid,)).lastrowid
        assert next_id > cid


def enable_telegram(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(main, "TELEGRAM_BOT", "leadtime_test_bot")
    monkeypatch.setattr(main, "TELEGRAM_HOOK_SECRET", "hook-secret")


@pytest.mark.parametrize("missing", ["token", "username", "hook"])
def test_telegram_readiness_requires_complete_configuration(client, monkeypatch, missing):
    enable_telegram(monkeypatch)
    if missing == "token":
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    else:
        monkeypatch.setattr(main, "TELEGRAM_BOT" if missing == "username" else "TELEGRAM_HOOK_SECRET", "")
    signup(client)
    page = client.get("/alerts")
    assert page.status_code == 200 and "Connect Telegram" not in page.text
    assert main.channel_status()["telegram"]["available"] is False
    assert client.post("/telegram/webhook/hook-secret", json={}).status_code == 404
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM telegram_links").fetchone()[0] == 0


def test_deleted_account_telegram_code_does_not_link_replacement(client, monkeypatch):
    enable_telegram(monkeypatch)
    signup(client)
    client.get("/alerts")
    with db.tx() as con:
        code = con.execute("SELECT code FROM telegram_links").fetchone()[0]
    client.post("/account/delete", data={"confirm": "first@example.com", "csrf": client.cookies["lt_csrf"]})
    signup(client, "replacement@example.com")
    response = client.post("/telegram/webhook/hook-secret", json={"message": {"text": f"/start {code}", "chat": {"id": 42}}})
    assert response.status_code == 200
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0


def test_telegram_confirmation_runs_after_commit_and_link_is_one_time(client, monkeypatch):
    enable_telegram(monkeypatch)
    uid = signup(client)
    client.get("/alerts")
    with db.tx() as con:
        code = con.execute("SELECT code FROM telegram_links").fetchone()[0]
    calls = []
    def send(channel, target, message):
        with db.tx() as con:
            # This would deadlock/time out if the handler still held its write transaction.
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT target FROM channels WHERE user_id=?", (uid,)).fetchone()
            assert json.loads(row[0]) == {"chat_id": "42"}
            assert not con.execute("SELECT 1 FROM telegram_links WHERE code=?", (code,)).fetchone()
        calls.append((channel, target))
        return main.notify.Result(True)
    monkeypatch.setattr(main.notify, "send", send)
    update = {"message": {"text": f"/start {code}", "chat": {"id": 42}}}
    assert client.post("/telegram/webhook/hook-secret", json=update).status_code == 200
    assert client.post("/telegram/webhook/hook-secret", json=update).status_code == 200
    assert calls == [("telegram", {"chat_id": "42"})]


def test_telegram_background_send_uses_worker_thread(client, monkeypatch):
    enable_telegram(monkeypatch)
    signup(client)
    client.get("/alerts")
    with db.tx() as con:
        code = con.execute("SELECT code FROM telegram_links").fetchone()[0]
    send_threads = []
    monkeypatch.setattr(main.notify, "send", lambda *args: send_threads.append(threading.get_ident()))
    class Request:
        async def json(self):
            return {"message": {"text": f"/start {code}", "chat": {"id": 42}}}
    async def run():
        event_loop_thread = threading.get_ident()
        tasks = BackgroundTasks()
        assert await main.telegram_webhook(Request(), "hook-secret", tasks) == {"ok": True}
        assert send_threads == []
        await tasks()
        assert send_threads and send_threads[0] != event_loop_thread
    asyncio.run(run())


def test_slack_target_masks_secret_path(client):
    url = "https://hooks.slack.com/services/T123/B456/very-secret-token"
    assert main.mask_target("slack", {"webhook_url": url}) == "hooks.slack.com/…oken"
    uid = signup(client)
    with db.tx() as con:
        con.execute("INSERT INTO channels(user_id,kind,target,created_at) VALUES (?, 'slack', ?, 1)",
                    (uid, json.dumps({"webhook_url": url})))
    page = client.get("/alerts")
    assert "very-secret-token" not in page.text and "T123/B456" not in page.text
