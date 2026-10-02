"""Leadtime web app: public research pages + accounts + alert-channel settings."""
import base64
import hashlib
import hmac
import json
import re
import os
import secrets
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import db

WEB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB))
import notify  # noqa: E402  (vendored alert-delivery package)

SNAPSHOT = WEB / "data" / "snapshot.json"
SECRET = os.environ.get("LEADTIME_SECRET", "")
if len(SECRET) < 32:
    # A random per-process fallback would silently log everyone out on restart and break
    # sessions/CSRF across uvicorn workers, so refuse to start instead.
    raise RuntimeError("LEADTIME_SECRET must be set to at least 32 characters "
                       "(generate one with: python3 -c 'import secrets; print(secrets.token_hex(32))').")
SITE_URL = os.environ.get("LEADTIME_URL", "https://leadtime.benbox.dev")
HTTPS = SITE_URL.startswith("https")
TELEGRAM_BOT = os.environ.get("TELEGRAM_BOT_USERNAME", "")
TELEGRAM_HOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
COOKIE = "lt_session"
# __Host- prefix: browser only accepts it Secure, Path=/, without Domain, so sibling *.benbox.dev
# hosts cannot plant it. Plain name for http:// local dev, where Secure cookies are not stored.
CSRF_COOKIE = "__Host-lt_csrf" if HTTPS else "lt_csrf"
signer = URLSafeTimedSerializer(SECRET, salt="session")
ph = PasswordHasher()
# Verified against when the email is unknown, so a login costs the same either way.
DUMMY_HASH = ph.hash(secrets.token_hex(16))

app = FastAPI(title="Leadtime", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
templates = Jinja2Templates(directory=WEB / "templates")

CHANNELS = [
    {"kind": "discord", "name": "Discord", "icon": "discord-logo", "field": "webhook_url",
     "placeholder": "https://discord.com/api/webhooks/…", "help": "Server Settings → Integrations → Webhooks → New Webhook → Copy URL."},
    {"kind": "telegram", "name": "Telegram", "icon": "telegram-logo", "field": None, "placeholder": "",
     "help": "Link your Telegram account in one tap. We message you from our bot."},
    {"kind": "email", "name": "Email", "icon": "envelope-simple", "field": "address", "placeholder": "you@example.com",
     "help": "Plain, short emails. One per event, plus the monthly digest if enabled."},
    {"kind": "sms", "name": "SMS", "icon": "chat-text", "field": "phone", "placeholder": "+15551234567",
     "help": "International format. Only call changes are sent by SMS."},
    {"kind": "ntfy", "name": "Phone push (ntfy)", "icon": "device-mobile", "field": "topic",
     "placeholder": "leadtime-yourname-8x2k", "help": "Install the free ntfy app, subscribe to a hard-to-guess topic, and paste it here."},
    {"kind": "slack", "name": "Slack", "icon": "slack-logo", "field": "webhook_url",
     "placeholder": "https://hooks.slack.com/services/…", "help": "Create an Incoming Webhook in your Slack workspace."},
    {"kind": "webhook", "name": "Webhook", "icon": "webhooks-logo", "field": "url", "placeholder": "https://your-server/hook",
     "help": "We POST JSON {title, body, url, severity, sent_at}. Optional HMAC secret."},
]


_INLINE = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(m.encode()).digest()).decode()
                   for m in re.findall(r"<script>(.*?)</script>", (WEB / "templates" / "base.html").read_text(), re.S))
CSP = (f"default-src 'self'; script-src 'self' {_INLINE}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    if HTTPS:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


@app.on_event("startup")
def _startup():
    db.init()


# ---------------------------------------------------------------- data
# (mtime, data) replaced as one tuple, so a concurrent reader never sees a new mtime with a
# half-built dict.
_cache = {"entry": (0, None)}


def snap():
    m = SNAPSHOT.stat().st_mtime
    mtime, data = _cache["entry"]
    if m != mtime:
        d = json.loads(SNAPSHOT.read_text())
        d["by_slug"] = {t["slug"]: t for t in d["themes"]}
        comp = {}
        for t in d["themes"]:
            for b in t["basket"]:
                c = comp.setdefault(b["ticker"], dict(b, themes=[]))
                c["themes"].append({"slug": t["slug"], "name": t["name"], "role": b.get("role")})
        d["companies"] = comp
        _cache["entry"] = (m, d)
        data = d
    return data


def safe_url(value):
    """Jinja filter: pass through only absolute http(s) URLs; anything else becomes '#'."""
    if not isinstance(value, str) or any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        return "#"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "#"
    return value if parts.scheme.lower() in ("http", "https") and parts.netloc else "#"


templates.env.filters["safe_url"] = safe_url


# ---------------------------------------------------------------- session / auth
def current_user(request: Request):
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        data = signer.loads(raw, max_age=60 * 60 * 24 * 30)
    except BadSignature:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("uid"), int) or not isinstance(data.get("sv"), int):
        return None
    with db.tx() as con:
        row = con.execute("SELECT id, email, is_admin, session_ver FROM users WHERE id=?", (data["uid"],)).fetchone()
    if not row or row["session_ver"] != data["sv"]:
        return None  # account gone, or signed out (everywhere) since this cookie was issued
    return dict(row)


_BROWSER_ID = re.compile(r"[A-Za-z0-9_-]{32}")


def csrf_browser_id(request: Request):
    v = request.cookies.get(CSRF_COOKIE)
    return v if v and _BROWSER_ID.fullmatch(v) else None


def csrf_for(browser_id: str) -> str:
    """Form token = HMAC(SECRET, per-browser id). A planted cookie is useless without its HMAC."""
    return hmac.new(SECRET.encode(), b"csrf|" + browser_id.encode(), hashlib.sha256).hexdigest()


def check_csrf(request: Request, token: str):
    bid = csrf_browser_id(request)
    if not bid or not isinstance(token, str) or not hmac.compare_digest(token, csrf_for(bid)):
        raise HTTPException(400, "Form expired. Reload the page and try again.")


def asset_version():
    """Changes whenever app.css or app.js changes, so browsers never pair new HTML with a cached old stylesheet."""
    h = hashlib.sha1()
    for f in ("css/app.css", "js/app.js", "css/fonts.css"):
        p = WEB / "static" / f
        h.update(f"{p.stat().st_mtime_ns}".encode())
    return h.hexdigest()[:10]


def render(request: Request, name: str, **ctx):
    bid = csrf_browser_id(request) or secrets.token_urlsafe(24)
    tok = csrf_for(bid)
    d = snap()
    st = notify.channel_status()
    ctx.update(request=request, user=current_user(request), csrf=tok, snap=d, site_url=SITE_URL, asset_v=asset_version(),
               path=request.url.path, channels_live=[c for c in CHANNELS if st[c["kind"]]["available"]],
               channels_soon=[c for c in CHANNELS if not st[c["kind"]]["available"]])
    resp = templates.TemplateResponse(request, name, ctx)
    if request.cookies.get(CSRF_COOKIE) != bid:
        resp.set_cookie(CSRF_COOKIE, bid, path="/", httponly=True, samesite="lax", secure=HTTPS)
    return resp


def login_response(uid: int, session_ver: int, to="/alerts"):
    resp = RedirectResponse(to, status_code=303)
    resp.set_cookie(COOKIE, signer.dumps({"uid": uid, "sv": session_ver}), max_age=60 * 60 * 24 * 30, httponly=True,
                    samesite="lax", secure=HTTPS)
    return resp


def client_ip(request: Request) -> str:
    # The app listens on 127.0.0.1 behind Cloudflare, which sets cf-connecting-ip.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def rate_limited(kind: str, limits, window: int = 3600) -> bool:
    """Sliding-window limit stored in SQLite (shared by all workers). limits = [(key, max_events), ...].
    Returns True (and records nothing) if any key is at its cap; otherwise records one event per key."""
    t = db.now()
    with db.tx() as con:
        # The DELETE opens the write transaction, so count-then-insert is serialized across workers.
        con.execute("DELETE FROM rate_events WHERE kind=? AND at < ?", (kind, t - window))
        for key, cap in limits:
            n = con.execute("SELECT COUNT(*) FROM rate_events WHERE kind=? AND key=?", (kind, key)).fetchone()[0]
            if n >= cap:
                return True
        con.executemany("INSERT INTO rate_events(kind, key, at) VALUES (?, ?, ?)", [(kind, k, t) for k, _ in limits])
    return False


def over_test_send_limit(request: Request, uid: int) -> bool:
    """Test messages (including the one sent when a channel is added): 5/hour per user, 20/hour per IP."""
    return rate_limited("test_send", [(f"user:{uid}", 5), (f"ip:{client_ip(request)}", 20)])


def too_many_attempts(ip: str) -> bool:
    with db.tx() as con:
        con.execute("DELETE FROM login_attempts WHERE at < ?", (db.now() - 900,))
        n = con.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=?", (ip,)).fetchone()[0]
        con.execute("INSERT INTO login_attempts(ip, at) VALUES (?, ?)", (ip, db.now()))
    return n >= 10


# ---------------------------------------------------------------- public pages
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render(request, "home.html")


@app.get("/map", response_class=HTMLResponse)
def bottleneck_map(request: Request):
    return render(request, "map.html")


@app.get("/t/{slug}", response_class=HTMLResponse)
def theme(request: Request, slug: str):
    t = snap()["by_slug"].get(slug)
    if not t:
        raise HTTPException(404)
    return render(request, "theme.html", t=t)


@app.get("/discovery", response_class=HTMLResponse)
def discovery(request: Request, kind: str = "all", theme: str = ""):
    return render(request, "discovery.html", kind=kind, theme_filter=theme)


@app.get("/press", response_class=HTMLResponse)
def press(request: Request, theme: str = "", signal: str = "", kind: str = ""):
    items = (snap().get("context") or {}).get("all", [])
    if theme:
        items = [i for i in items if theme in i["themes"]]
    if signal in ("tightening", "easing", "neutral"):
        items = [i for i in items if i["signal"] == signal]
    if kind in ("newsletter", "research", "press", "news"):
        items = [i for i in items if i["kind"] == kind]
    return render(request, "press.html", items=items[:150], n=len(items), theme_filter=theme, signal=signal, kind=kind)


@app.get("/companies", response_class=HTMLResponse)
def companies(request: Request, q: str = ""):
    return render(request, "companies.html", q=q)


@app.get("/c/{ticker}", response_class=HTMLResponse)
def company(request: Request, ticker: str):
    c = snap()["companies"].get(ticker)
    if not c:
        raise HTTPException(404)
    flags = [i for i in snap()["discovery"]["items"] if i.get("ticker") == ticker]
    return render(request, "company.html", c=c, flags=flags)


@app.get("/track-record", response_class=HTMLResponse)
def track(request: Request):
    return render(request, "track.html")


@app.get("/method", response_class=HTMLResponse)
def method(request: Request):
    return render(request, "method.html")


HEALTH_MAX_PRICE_AGE_DAYS = 4
HEALTH_MAX_GENERATED_AGE_HOURS = 36


@app.get("/healthz")
def healthz():
    """503 when the published snapshot is stale, so an uptime check catches a silently failing cron."""
    reasons, body = [], {}
    try:
        d = snap()
    except Exception:
        return JSONResponse({"ok": False, "reasons": ["snapshot unreadable"]}, status_code=503)
    now = datetime.now(timezone.utc)
    body["generated_at"], body["prices_asof"] = d.get("generated_at"), d.get("prices_asof")
    try:
        gen = datetime.fromisoformat(str(d["generated_at"]))
        gen = gen if gen.tzinfo else gen.replace(tzinfo=timezone.utc)
        body["generated_age_hours"] = round((now - gen).total_seconds() / 3600, 1)
        if body["generated_age_hours"] > HEALTH_MAX_GENERATED_AGE_HOURS:
            reasons.append(f"snapshot generated {body['generated_age_hours']}h ago (limit {HEALTH_MAX_GENERATED_AGE_HOURS}h)")
    except (KeyError, ValueError):
        reasons.append("generated_at missing or unparseable")
    try:
        body["prices_age_days"] = (now.date() - date.fromisoformat(str(d["prices_asof"])[:10])).days
        if body["prices_age_days"] > HEALTH_MAX_PRICE_AGE_DAYS:
            reasons.append(f"prices_asof is {body['prices_age_days']} days old (limit {HEALTH_MAX_PRICE_AGE_DAYS})")
    except (KeyError, ValueError):
        reasons.append("prices_asof missing or unparseable")
    if reasons:
        return JSONResponse({"ok": False, "reasons": reasons, **body}, status_code=503)
    return {"ok": True, **body}


@app.get("/api/snapshot")
def api_snapshot():
    d = dict(snap())
    for k in ("by_slug", "companies"):
        d.pop(k, None)
    return JSONResponse(d)


@app.get("/api/theme/{slug}")
def api_theme(slug: str):
    t = snap()["by_slug"].get(slug)
    if not t:
        raise HTTPException(404)
    return JSONResponse(t)


# ---------------------------------------------------------------- accounts
SIGNUP_NOTICE = ("Account ready. Sign in with your email and the password you just chose. "
                 "If that email already had an account, nothing was changed; sign in with its existing password.")


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return render(request, "auth.html", mode="signup", error=None)


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form("")):
    check_csrf(request, csrf)
    email = email.strip().lower()
    if "@" not in email or len(email) > 254:
        return render(request, "auth.html", mode="signup", error="Enter a valid email address.")
    if len(password) < 10:
        return render(request, "auth.html", mode="signup", error="Use at least 10 characters for your password.")
    if rate_limited("signup", [(f"ip:{client_ip(request)}", 5)]):
        return render(request, "auth.html", mode="signup", error="Too many sign-ups from this network. Try again in an hour.")
    # Hash before looking the email up, and answer identically either way, so neither the
    # response nor its timing reveals whether an account already exists. Nobody is logged in here.
    pw_hash = ph.hash(password)
    with db.tx() as con:
        cur = con.execute("INSERT OR IGNORE INTO users(email, pw_hash, created_at) VALUES (?, ?, ?)",
                          (email, pw_hash, db.now()))
        if cur.rowcount:
            db.get_prefs(con, cur.lastrowid)
    return RedirectResponse("/login?created=1", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, created: int = 0):
    return render(request, "auth.html", mode="login", error=None, notice=SIGNUP_NOTICE if created else None)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form("")):
    check_csrf(request, csrf)
    if too_many_attempts(client_ip(request)):
        return render(request, "auth.html", mode="login", error="Too many attempts. Wait 15 minutes and try again.")
    with db.tx() as con:
        row = con.execute("SELECT id, pw_hash, session_ver FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    try:
        ph.verify(row["pw_hash"] if row else DUMMY_HASH, password)
        if not row:
            raise VerificationError
    except (VerificationError, InvalidHashError):
        return render(request, "auth.html", mode="login", error="Email or password is incorrect.")
    return login_response(row["id"], row["session_ver"])


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    """Signs out on every device: bumping session_ver invalidates all cookies issued so far."""
    check_csrf(request, csrf)
    u = current_user(request)
    if u:
        with db.tx() as con:
            con.execute("UPDATE users SET session_ver = session_ver + 1 WHERE id=?", (u["id"],))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- alert settings
# /alerts?msg=… / ?err=… carry fixed codes only; the page shows the mapped text, never the query value.
MESSAGES = {
    "added": "Channel added. Test message sent.",
    "test_sent": "Test message sent.",
    "code_sent": "We sent a 6-digit code to that channel. Enter it below to start alerts there.",
    "verified": "Channel confirmed. Alerts will be delivered there.",
    "removed": "Channel removed.",
    "prefs_saved": "Preferences saved.",
}
INVALID_HINTS = {
    "discord": "That isn't a Discord webhook URL. Paste the full https://discord.com/api/webhooks/… link.",
    "slack": "That isn't a Slack webhook URL. Paste the full https://hooks.slack.com/services/… link.",
    "webhook": "Use a public https:// URL with a hostname (no IP addresses, local names, credentials or #fragment).",
    "email": "Enter a valid email address.",
    "sms": "Use international format, e.g. +15551234567.",
    "ntfy": "Topics are 6–64 letters, digits, underscores or hyphens.",
}
ERRORS = {
    "unknown_channel": "Unknown channel.",
    "not_enabled": "That channel isn't enabled on this server yet.",
    "limit": "Limit of 10 channels reached.",
    "rate": "Too many test messages. Wait an hour and try again.",
    "not_found": "Channel not found.",
    "added_test_failed": "Saved, but the test message failed: delivery failed",
    "test_failed": "The test message failed: delivery failed",
    "bad_code": "That code didn't match. Check the most recent message and try again.",
    "code_expired": "That code expired or had too many wrong tries. Press Test to get a new one.",
    "confirm_delete": "Type your email to confirm deletion.",
    **{f"invalid_{k}": v for k, v in INVALID_HINTS.items()},
}
VERIFY_TTL = 3600
VERIFY_MAX_TRIES = 5


def to_alerts(msg: str = "", err: str = "", http: int | None = None):
    q = f"msg={msg}" if msg else f"err={err}"
    if http:
        q += f"&http={int(http)}"
    return RedirectResponse(f"/alerts?{q}", status_code=303)


def require_user(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(303, headers={"Location": "/login"})
    return u


def load_channels(con, uid):
    rows = [dict(r) for r in con.execute("SELECT * FROM channels WHERE user_id=? ORDER BY id", (uid,))]
    for r in rows:
        r["target"] = json.loads(r["target"])
        r["display"] = mask_target(r["kind"], r["target"])
    return rows


def mask_target(kind, t):
    if kind in ("discord", "slack"):
        return t["webhook_url"].split("/api/webhooks/")[0].replace("https://", "") + "/…" + t["webhook_url"][-4:]
    if kind == "webhook":
        return t["url"].split("?")[0][:48]
    if kind == "email":
        a, _, d = t["address"].partition("@")
        return a[:2] + "…@" + d
    if kind == "sms":
        return t["phone"][:4] + " … " + t["phone"][-3:]
    if kind == "ntfy":
        return t.get("server", "https://ntfy.sh").replace("https://", "") + "/" + t["topic"]
    if kind == "telegram":
        return "Linked chat"
    return ""


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, welcome: int = 0, msg: str = "", err: str = "", http: int = 0):
    u = require_user(request)
    with db.tx() as con:
        chans = load_channels(con, u["id"])
        prefs = db.get_prefs(con, u["id"])
        code = None
        if TELEGRAM_BOT and notify.channel_status()["telegram"]["available"]:
            code = secrets.token_urlsafe(12)
            con.execute("INSERT INTO telegram_links(code, user_id, created_at) VALUES (?, ?, ?)", (code, u["id"], db.now()))
    status = notify.channel_status()
    tg_link = notify.make_start_link(TELEGRAM_BOT, code) if code else None
    err_text = ERRORS.get(err)
    if err in ("added_test_failed", "test_failed"):
        err_text += f" (HTTP {http})." if 100 <= http <= 599 else "."
    return render(request, "alerts.html", channels=chans, prefs=prefs, status=status, catalog=CHANNELS,
                  welcome=welcome, msg=MESSAGES.get(msg), err=err_text, tg_link=tg_link)


@app.post("/alerts/channel")
def add_channel(request: Request, kind: str = Form(...), value: str = Form(""), secret: str = Form(""),
                label: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    u = require_user(request)
    spec = next((c for c in CHANNELS if c["kind"] == kind and c["field"]), None)
    if not spec:
        return to_alerts(err="unknown_channel")
    if not notify.channel_status()[kind]["available"]:
        return to_alerts(err="not_enabled")
    target = {spec["field"]: value.strip()}
    if kind == "webhook" and secret.strip():
        target["secret"] = secret.strip()
    ok, _ = notify.validate_target(kind, target)
    if not ok:
        return to_alerts(err=f"invalid_{kind}")
    if over_test_send_limit(request, u["id"]):  # adding a channel sends a test, so it counts
        return to_alerts(err="rate")
    with db.tx() as con:
        if con.execute("SELECT COUNT(*) FROM channels WHERE user_id=?", (u["id"],)).fetchone()[0] >= 10:
            return to_alerts(err="limit")
        cur = con.execute("INSERT INTO channels(user_id, kind, target, label, created_at, verified) VALUES (?, ?, ?, ?, ?, ?)",
                          (u["id"], kind, json.dumps(target), label.strip()[:40] or None, db.now(),
                           0 if kind in db.VERIFY_KINDS else 1))
        cid = cur.lastrowid
    return _send_test(u["id"], cid, added=True)


def _verify_hash(cid: int, code: str) -> str:
    return hmac.new(SECRET.encode(), f"verify|{cid}|{code}".encode(), hashlib.sha256).hexdigest()


def _send_test(uid, cid, added=False):
    """Caller has already charged the test-send rate limit. The network call runs outside any DB transaction."""
    with db.tx() as con:
        row = con.execute("SELECT * FROM channels WHERE id=? AND user_id=?", (cid, uid)).fetchone()
        if not row:
            return to_alerts(err="not_found")
        needs_code = row["kind"] in db.VERIFY_KINDS and not row["verified"]
        if needs_code:
            # Email/SMS must prove the address belongs to the user before any real alert goes there.
            code = f"{secrets.randbelow(10 ** 6):06d}"
            con.execute("UPDATE channels SET verify_hash=?, verify_sent_at=?, verify_tries=0 WHERE id=?",
                        (_verify_hash(cid, code), db.now(), cid))
            # SMS carries only the title, so the code goes there.
            msg = notify.Message(title=f"Leadtime confirmation code: {code}",
                                 body="Enter this code on your Leadtime alerts page to confirm this channel. "
                                      "If you didn't sign up for Leadtime alerts, ignore this message.",
                                 url=SITE_URL + "/alerts", severity="info")
        else:
            msg = notify.Message(title="Leadtime alerts are connected",
                                 body="This is a test. You'll get call changes and discovery flags here, based on your preferences.",
                                 url=SITE_URL + "/alerts", severity="info")
    res = notify.send(row["kind"], json.loads(row["target"]), msg)
    shown_error = None if res.ok else ("Delivery failed" + (f" (HTTP {res.http_status})" if res.http_status else "") + ".")
    with db.tx() as con:
        con.execute("UPDATE channels SET last_ok_at=COALESCE(?, last_ok_at), last_error=? WHERE id=?",
                    (db.now() if res.ok else None, shown_error, cid))
    if res.ok:
        return to_alerts(msg="code_sent" if needs_code else "added" if added else "test_sent")
    return to_alerts(err="added_test_failed" if added else "test_failed", http=res.http_status)


@app.post("/alerts/channel/{cid}/test")
def test_channel(request: Request, cid: int, csrf: str = Form("")):
    check_csrf(request, csrf)
    u = require_user(request)
    if over_test_send_limit(request, u["id"]):
        return to_alerts(err="rate")
    return _send_test(u["id"], cid)


@app.post("/alerts/channel/{cid}/verify")
def verify_channel(request: Request, cid: int, code: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    u = require_user(request)
    code = re.sub(r"\D", "", code)[:6]
    with db.tx() as con:
        row = con.execute("SELECT verified, verify_hash, verify_sent_at, verify_tries FROM channels WHERE id=? AND user_id=?",
                          (cid, u["id"])).fetchone()
        if not row:
            return to_alerts(err="not_found")
        if row["verified"]:
            return to_alerts(msg="verified")
        if (not row["verify_hash"] or (row["verify_sent_at"] or 0) < db.now() - VERIFY_TTL
                or row["verify_tries"] >= VERIFY_MAX_TRIES):
            return to_alerts(err="code_expired")
        if len(code) == 6 and hmac.compare_digest(_verify_hash(cid, code), row["verify_hash"]):
            con.execute("UPDATE channels SET verified=1, verify_hash=NULL, verify_tries=0 WHERE id=?", (cid,))
            return to_alerts(msg="verified")
        con.execute("UPDATE channels SET verify_tries = verify_tries + 1 WHERE id=?", (cid,))
    return to_alerts(err="bad_code")


@app.post("/alerts/channel/{cid}/delete")
def delete_channel(request: Request, cid: int, csrf: str = Form("")):
    check_csrf(request, csrf)
    u = require_user(request)
    with db.tx() as con:
        con.execute("DELETE FROM channels WHERE id=? AND user_id=?", (cid, u["id"]))
    return to_alerts(msg="removed")


@app.post("/alerts/prefs")
async def save_prefs(request: Request):
    form = await request.form()
    check_csrf(request, form.get("csrf", ""))
    u = require_user(request)
    valid = set(snap()["by_slug"])
    chosen = [s for s in form.getlist("themes") if s in valid]
    themes = ["*"] if form.get("all_themes") or not chosen else chosen
    try:
        mz = min(6.0, max(2.0, float(form.get("discovery_min_z", 3))))
    except ValueError:
        mz = 3.0
    with db.tx() as con:
        db.get_prefs(con, u["id"])
        con.execute("""UPDATE prefs SET themes=?, ev_calls=?, ev_discovery=?, ev_digest=?, discovery_min_z=?,
                       discovery_themed_only=? WHERE user_id=?""",
                    (json.dumps(themes), int(bool(form.get("ev_calls"))), int(bool(form.get("ev_discovery"))),
                     int(bool(form.get("ev_digest"))), mz, int(bool(form.get("discovery_themed_only"))), u["id"]))
    return to_alerts(msg="prefs_saved")


@app.post("/account/delete")
def delete_account(request: Request, csrf: str = Form(""), confirm: str = Form("")):
    check_csrf(request, csrf)
    u = require_user(request)
    if confirm.strip().lower() != u["email"]:
        return to_alerts(err="confirm_delete")
    with db.tx() as con:
        # Bump first for symmetry with logout; the row (and its id, never reused thanks to
        # AUTOINCREMENT) is then gone, so no outstanding cookie can match anything.
        con.execute("UPDATE users SET session_ver = session_ver + 1 WHERE id=?", (u["id"],))
        con.execute("DELETE FROM users WHERE id=?", (u["id"],))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- telegram linking
@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(request: Request, secret: str):
    if not TELEGRAM_HOOK_SECRET or not secrets.compare_digest(secret, TELEGRAM_HOOK_SECRET):
        raise HTTPException(404)
    upd = await request.json()
    parsed = notify.parse_update_for_code(upd)
    if parsed:
        code, chat_id = parsed
        with db.tx() as con:
            row = con.execute("SELECT user_id FROM telegram_links WHERE code=? AND created_at > ?",
                              (code, db.now() - 3600)).fetchone()
            if row:
                con.execute("DELETE FROM channels WHERE user_id=? AND kind='telegram'", (row["user_id"],))
                con.execute("INSERT INTO channels(user_id, kind, target, label, created_at) VALUES (?, 'telegram', ?, 'Telegram', ?)",
                            (row["user_id"], json.dumps({"chat_id": str(chat_id)}), db.now()))
                con.execute("DELETE FROM telegram_links WHERE code=?", (code,))
                notify.send("telegram", {"chat_id": str(chat_id)},
                            notify.Message("Linked to Leadtime", "You'll receive your alerts here.", SITE_URL + "/alerts", "info"))
    return {"ok": True}


@app.exception_handler(404)
async def not_found(request: Request, exc):
    r = render(request, "404.html")
    r.status_code = 404
    return r


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    r = render(request, "404.html") if exc.status_code == 404 else \
        render(request, "error.html", message=exc.detail, status=exc.status_code)
    r.status_code = exc.status_code
    return r
