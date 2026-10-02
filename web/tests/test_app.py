"""Web-app security behaviour: sessions, CSRF, rate limits, signup/login, verification, healthz, safe_url."""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Must be set before app.main is imported (it refuses to start without a strong secret).
os.environ["LEADTIME_SECRET"] = "t" * 48
os.environ.setdefault("LEADTIME_DB", os.path.join(tempfile.mkdtemp(), "import.db"))
os.environ.pop("LEADTIME_URL", None)  # default https://… so the __Host- cookie path is exercised

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main  # noqa: E402
from notify import Result  # noqa: E402

WEB = Path(__file__).resolve().parent.parent
PASSWORD = "correct horse battery"


@pytest.fixture
def sent(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "leadtime.db"))
    db.init()
    outbox = []

    def fake_send(channel, target, msg):
        outbox.append((channel, target, msg))
        return Result(True)

    monkeypatch.setattr(main.notify, "send", fake_send)
    return outbox


def new_client(ip="203.0.113.10"):
    return TestClient(main.app, base_url="https://testserver", headers={"cf-connecting-ip": ip},
                      follow_redirects=False)


def csrf(client, path="/login"):
    r = client.get(path)
    return re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)


def signup(client, email, password=PASSWORD):
    return client.post("/signup", data={"email": email, "password": password, "csrf": csrf(client, "/signup")})


def login(client, email, password=PASSWORD):
    return client.post("/login", data={"email": email, "password": password, "csrf": csrf(client)})


def account(email, ip="203.0.113.10"):
    c = new_client(ip)
    assert signup(c, email).status_code == 303
    r = login(c, email)
    assert r.status_code == 303 and r.headers["location"] == "/alerts"
    return c


def alerts_location(r):
    assert r.status_code == 303
    return r.headers["location"]


# ---------------------------------------------------------------- sessions (#8)
def test_logout_revokes_every_session(sent):
    a = account("u@example.com")
    b = new_client()
    login(b, "u@example.com")
    assert b.get("/alerts").status_code == 200
    stolen = b.cookies.get(main.COOKIE)
    assert a.post("/logout", data={"csrf": csrf(a, "/alerts")}).status_code == 303
    assert b.get("/alerts").headers["location"] == "/login"  # other device is signed out too
    replay = new_client()
    replay.cookies.set(main.COOKIE, stolen)
    assert replay.get("/alerts").headers["location"] == "/login"
    login(b, "u@example.com")
    assert b.get("/alerts").status_code == 200  # signing in again issues a fresh, valid cookie


def test_deleted_account_cookie_never_opens_a_new_account(sent):
    a = account("old@example.com")
    old_cookie = a.cookies.get(main.COOKIE)
    with db.tx() as con:
        old_id = con.execute("SELECT id FROM users WHERE email='old@example.com'").fetchone()[0]
    r = a.post("/account/delete", data={"csrf": csrf(a, "/alerts"), "confirm": "old@example.com"})
    assert r.status_code == 303
    account("new@example.com")
    with db.tx() as con:
        new_id = con.execute("SELECT id FROM users WHERE email='new@example.com'").fetchone()[0]
    assert new_id != old_id  # AUTOINCREMENT: ids are never reused
    c = new_client()
    c.cookies.set(main.COOKIE, old_cookie)
    assert c.get("/alerts").headers["location"] == "/login"


def test_cookie_without_session_version_is_rejected(sent):
    account("u@example.com")
    with db.tx() as con:
        uid = con.execute("SELECT id FROM users").fetchone()[0]
    c = new_client()
    c.cookies.set(main.COOKIE, main.signer.dumps({"uid": uid}))  # pre-fix cookie format
    assert c.get("/alerts").headers["location"] == "/login"


def test_init_is_idempotent_and_migrates_old_users_table(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
          created_at INTEGER NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE channels (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, target TEXT NOT NULL, label TEXT, created_at INTEGER NOT NULL, last_ok_at INTEGER, last_error TEXT);
        INSERT INTO users VALUES (3, 'a@example.com', 'h', 1, 0);
        INSERT INTO channels(user_id, kind, target, created_at) VALUES (3, 'sms', '{}', 1), (3, 'discord', '{}', 1);
    """)
    con.commit()
    con.close()
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init()
    db.init()
    with db.tx() as con:
        assert "AUTOINCREMENT" in con.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
        assert [tuple(r) for r in con.execute("SELECT id, session_ver FROM users")] == [(3, 0)]
        assert [tuple(r) for r in con.execute("SELECT kind, verified FROM channels ORDER BY id")] == [("sms", 0), ("discord", 1)]
        con.execute("DELETE FROM users WHERE id=3")
        assert con.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0  # FK cascade survived the rebuild


def test_startup_fails_without_strong_secret():
    env = {k: v for k, v in os.environ.items() if k != "LEADTIME_SECRET"}
    for secret in (None, "short"):
        if secret:
            env["LEADTIME_SECRET"] = secret
        r = subprocess.run([sys.executable, "-c", "import app.main"], cwd=WEB, env=env, capture_output=True, text=True)
        assert r.returncode != 0 and "LEADTIME_SECRET" in r.stderr


# ---------------------------------------------------------------- CSRF (#11)
def test_csrf_cookie_is_host_prefixed_and_token_is_hmac(sent):
    c = new_client()
    r = c.get("/login")
    header = r.headers["set-cookie"]
    assert header.startswith("__Host-lt_csrf=") and "Secure" in header and "Path=/" in header and "Domain" not in header
    browser_id = c.cookies.get("__Host-lt_csrf")
    token = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
    assert token != browser_id and token == main.csrf_for(browser_id)


@pytest.mark.parametrize("forge", ["planted_cookie_as_token", "other_browsers_token", "empty"])
def test_csrf_rejects_forged_cookie(sent, forge):
    attacker = new_client()
    attacker_token = csrf(attacker)
    victim = new_client()
    planted = "A" * 32
    victim.cookies.set("__Host-lt_csrf", planted)
    token = {"planted_cookie_as_token": planted, "other_browsers_token": attacker_token, "empty": ""}[forge]
    r = victim.post("/login", data={"email": "x@example.com", "password": "whatever-123", "csrf": token})
    assert r.status_code == 400


def test_csrf_valid_form_round_trip(sent):
    c = new_client()
    r = c.post("/login", data={"email": "nobody@example.com", "password": "x" * 12, "csrf": csrf(c)})
    assert r.status_code == 200 and "Email or password is incorrect." in r.text


# ---------------------------------------------------------------- signup / login (#12)
def test_signup_is_neutral_for_existing_email(sent):
    first = new_client("198.51.100.1")
    r1 = signup(first, "taken@example.com")
    second = new_client("198.51.100.2")
    r2 = signup(second, "taken@example.com", password="attacker-password")
    for r in (r1, r2):
        assert r.status_code == 303 and r.headers["location"] == "/login?created=1"
        assert main.COOKIE not in r.headers.get("set-cookie", "")
    assert "That email already has an account" not in second.get("/login?created=1").text
    assert login(new_client(), "taken@example.com").headers.get("location") == "/alerts"
    assert login(new_client(), "taken@example.com", "attacker-password").status_code == 200  # unchanged


def test_login_unknown_email_still_runs_argon2(sent, monkeypatch):
    seen = []
    real = main.ph

    class Spy:
        def verify(self, h, p):
            seen.append(h)
            return real.verify(h, p)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(main, "ph", Spy())
    r = login(new_client(), "ghost@example.com")
    assert r.status_code == 200 and "Email or password is incorrect." in r.text
    assert seen == [main.DUMMY_HASH]


# ---------------------------------------------------------------- rate limits (#7)
def add_discord(c, n=0):
    return c.post("/alerts/channel", data={"kind": "discord", "value": f"https://discord.com/api/webhooks/{n + 1}/tok",
                                           "csrf": csrf(c, "/alerts")})


def test_test_sends_limited_per_user(sent):
    c = account("u@example.com")
    assert alerts_location(add_discord(c)) == "/alerts?msg=added"
    with db.tx() as con:
        cid = con.execute("SELECT id FROM channels").fetchone()[0]
    tok = csrf(c, "/alerts")
    for _ in range(4):
        assert alerts_location(c.post(f"/alerts/channel/{cid}/test", data={"csrf": tok})) == "/alerts?msg=test_sent"
    assert alerts_location(c.post(f"/alerts/channel/{cid}/test", data={"csrf": tok})) == "/alerts?err=rate"
    assert alerts_location(add_discord(c, 1)) == "/alerts?err=rate"  # adding a channel counts too
    assert len(sent) == 5
    assert "Too many test messages" in c.get("/alerts?err=rate").text


def test_test_sends_limited_per_ip(sent):
    ip = "198.51.100.77"
    clients = [account(f"u{i}@example.com", ip) for i in range(5)]
    for c in clients[:4]:
        for n in range(5):
            assert alerts_location(add_discord(c, n)) == "/alerts?msg=added"
    assert alerts_location(add_discord(clients[4])) == "/alerts?err=rate"
    assert len(sent) == 20


def test_signup_limited_per_ip(sent):
    c = new_client("198.51.100.200")
    for i in range(5):
        assert signup(c, f"s{i}@example.com").status_code == 303
    r = signup(c, "s5@example.com")
    assert r.status_code == 200 and "Too many sign-ups" in r.text
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 5
    assert signup(new_client("198.51.100.201"), "s5@example.com").status_code == 303  # other IPs unaffected


# ---------------------------------------------------------------- email/SMS verification (#7)
def test_email_channel_requires_code_before_alerts(sent, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "alerts@example.com")
    c = account("u@example.com")
    r = c.post("/alerts/channel", data={"kind": "email", "value": "me@example.org", "csrf": csrf(c, "/alerts")})
    assert alerts_location(r) == "/alerts?msg=code_sent"
    code = re.search(r"\b(\d{6})\b", sent[-1][2].title).group(1)
    with db.tx() as con:
        cid, verified = con.execute("SELECT id, verified FROM channels").fetchone()
    assert verified == 0
    page = c.get("/alerts").text
    assert f"/alerts/channel/{cid}/verify" in page and "Waiting for confirmation" in page
    tok = csrf(c, "/alerts")
    wrong = "000000" if code != "000000" else "111111"
    assert alerts_location(c.post(f"/alerts/channel/{cid}/verify", data={"code": wrong, "csrf": tok})) == "/alerts?err=bad_code"
    assert alerts_location(c.post(f"/alerts/channel/{cid}/verify", data={"code": code, "csrf": tok})) == "/alerts?msg=verified"
    with db.tx() as con:
        assert tuple(con.execute("SELECT verified, verify_hash FROM channels").fetchone()) == (1, None)


def test_verification_code_locks_after_five_wrong_tries(sent, monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setenv("TWILIO_FROM", "+14155550100")
    c = account("u@example.com")
    c.post("/alerts/channel", data={"kind": "sms", "value": "+14155550123", "csrf": csrf(c, "/alerts")})
    code = re.search(r"\b(\d{6})\b", sent[-1][2].title).group(1)
    wrong = "000000" if code != "000000" else "111111"
    tok = csrf(c, "/alerts")
    for _ in range(5):
        c.post("/alerts/channel/1/verify", data={"code": wrong, "csrf": tok})
    r = c.post("/alerts/channel/1/verify", data={"code": code, "csrf": tok})
    assert alerts_location(r) == "/alerts?err=code_expired"


def test_discord_channel_is_verified_immediately(sent):
    c = account("u@example.com")
    add_discord(c)
    with db.tx() as con:
        assert con.execute("SELECT verified FROM channels").fetchone()[0] == 1


# ---------------------------------------------------------------- fixed messages, HSTS (#15)
def test_alert_messages_are_fixed_codes(sent):
    c = account("u@example.com")
    page = c.get("/alerts", params={"err": "<b>Call +1-555 now</b>", "msg": "Your account is compromised"}).text
    assert "Call +1-555" not in page and "compromised" not in page
    assert "The test message failed: delivery failed (HTTP 404)." in c.get("/alerts?err=test_failed&http=404").text
    assert "Saved, but the test message failed: delivery failed." in c.get("/alerts?err=added_test_failed").text


def test_failed_test_send_shows_only_generic_error(sent, monkeypatch):
    monkeypatch.setattr(main.notify, "send", lambda *a: Result(False, "connect to 10.0.0.5:6379 refused", http_status=None))
    c = account("u@example.com")
    loc = alerts_location(add_discord(c))
    assert loc == "/alerts?err=added_test_failed"
    page = c.get(loc).text
    assert "10.0.0.5" not in page and "Last test failed: Delivery failed." in page


def test_hsts_header(sent):
    r = new_client().get("/login")
    assert r.headers["strict-transport-security"] == "max-age=31536000"


# ---------------------------------------------------------------- healthz (#9)
@pytest.fixture
def snapshot_file(monkeypatch, tmp_path):
    def write(generated_at, prices_asof):
        p = tmp_path / "snapshot.json"
        p.write_text(json.dumps({"generated_at": generated_at, "prices_asof": prices_asof, "themes": []}))
        monkeypatch.setattr(main, "SNAPSHOT", p)
        monkeypatch.setitem(main._cache, "entry", (0, None))
    return write


def test_healthz_fresh(snapshot_file):
    now = datetime.now(timezone.utc)
    snapshot_file((now - timedelta(hours=3)).isoformat(), (now - timedelta(days=1)).date().isoformat())
    r = new_client().get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["prices_age_days"] == 1 and 2.9 <= body["generated_age_hours"] <= 3.1


@pytest.mark.parametrize("gen_hours,price_days,needle", [(3, 5, "prices_asof"), (37, 1, "generated"), (40, 9, "")])
def test_healthz_stale(snapshot_file, gen_hours, price_days, needle):
    now = datetime.now(timezone.utc)
    snapshot_file((now - timedelta(hours=gen_hours)).isoformat(), (now - timedelta(days=price_days)).date().isoformat())
    r = new_client().get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False and body["reasons"] and all(isinstance(x, str) for x in body["reasons"])
    assert any(needle in x for x in body["reasons"])
    assert len(body["reasons"]) == (2 if not needle else 1)


def test_healthz_unparseable(snapshot_file):
    snapshot_file("garbage", None)
    r = new_client().get("/healthz")
    assert r.status_code == 503 and len(r.json()["reasons"]) == 2


# ---------------------------------------------------------------- safe_url (#15)
@pytest.mark.parametrize("value,expected", [
    ("https://example.com/a?b=1", "https://example.com/a?b=1"),
    ("HTTP://example.com/", "HTTP://example.com/"),
    ("javascript:alert(1)", "#"), ("JavaScript:alert(1)", "#"), (" javascript:alert(1)", "#"),
    ("java\nscript:alert(1)", "#"), ("java\tscript:alert(1)", "#"), ("data:text/html,<script>", "#"),
    ("vbscript:x", "#"), ("//evil.example/x", "#"), ("/relative", "#"), ("https://", "#"), ("", "#"),
    (None, "#"), (42, "#"), ("https://[bad", "#"),
])
def test_safe_url(value, expected):
    assert main.safe_url(value) == expected


def test_press_list_renders_only_http_links():
    tpl = main.templates.env.from_string(
        '{% from "_macros.html" import press_list %}{{ press_list(items, {}) }}')
    html = tpl.render(items=[
        {"url": "javascript:alert(document.cookie)", "title": "evil", "signal": "neutral", "claim": "", "source_name": "s",
         "published": "2026-10-01", "kind": "press", "themes": []},
        {"url": "https://ok.example/a", "title": "fine", "signal": "neutral", "claim": "", "source_name": "s",
         "published": "2026-10-01", "kind": "press", "themes": []},
    ])
    assert "javascript:" not in html and 'href="#"' in html and 'href="https://ok.example/a"' in html
