"""Leadtime web app: public research pages + accounts + alert-channel settings."""
import base64
import hashlib
import json
import re
import os
import secrets
import sys
import time
from functools import lru_cache
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
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
SECRET = os.environ.get("LEADTIME_SECRET") or secrets.token_hex(32)
SITE_URL = os.environ.get("LEADTIME_URL", "https://leadtime.benbox.dev")
TELEGRAM_BOT = os.environ.get("TELEGRAM_BOT_USERNAME", "")
TELEGRAM_HOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
COOKIE = "lt_session"
signer = URLSafeTimedSerializer(SECRET, salt="session")
ph = PasswordHasher()

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
    return resp


@app.on_event("startup")
def _startup():
    db.init()


# ---------------------------------------------------------------- data
_cache = {"mtime": 0, "data": None}


def snap():
    m = SNAPSHOT.stat().st_mtime
    if m != _cache["mtime"]:
        _cache["data"] = json.loads(SNAPSHOT.read_text())
        _cache["mtime"] = m
        d = _cache["data"]
        d["by_slug"] = {t["slug"]: t for t in d["themes"]}
        comp = {}
        for t in d["themes"]:
            for b in t["basket"]:
                c = comp.setdefault(b["ticker"], dict(b, themes=[]))
                c["themes"].append({"slug": t["slug"], "name": t["name"], "role": b.get("role")})
        d["companies"] = comp
    return _cache["data"]


# ---------------------------------------------------------------- session / auth
def current_user(request: Request):
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        data = signer.loads(raw, max_age=60 * 60 * 24 * 30)
    except BadSignature:
        return None
    with db.tx() as con:
        row = con.execute("SELECT id, email, is_admin FROM users WHERE id=?", (data["uid"],)).fetchone()
    return dict(row) if row else None


def csrf_token(request: Request):
    tok = request.cookies.get("lt_csrf")
    return tok or secrets.token_urlsafe(24)


def check_csrf(request: Request, token: str):
    if not token or token != request.cookies.get("lt_csrf"):
        raise HTTPException(400, "Form expired. Reload the page and try again.")


def render(request: Request, name: str, **ctx):
    tok = csrf_token(request)
    d = snap()
    st = notify.channel_status()
    ctx.update(request=request, user=current_user(request), csrf=tok, snap=d, site_url=SITE_URL,
               path=request.url.path, channels_live=[c for c in CHANNELS if st[c["kind"]]["available"]],
               channels_soon=[c for c in CHANNELS if not st[c["kind"]]["available"]])
    resp = templates.TemplateResponse(request, name, ctx)
    if request.cookies.get("lt_csrf") != tok:
        resp.set_cookie("lt_csrf", tok, httponly=True, samesite="lax", secure=SITE_URL.startswith("https"))
    return resp


def login_response(uid: int, to="/alerts"):
    resp = RedirectResponse(to, status_code=303)
    resp.set_cookie(COOKIE, signer.dumps({"uid": uid}), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax",
                    secure=SITE_URL.startswith("https"))
    return resp


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


@app.get("/healthz")
def healthz():
    d = snap()
    return {"ok": True, "generated_at": d["generated_at"], "prices_asof": d["prices_asof"]}


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
    with db.tx() as con:
        if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return render(request, "auth.html", mode="signup", error="That email already has an account. Sign in instead.")
        cur = con.execute("INSERT INTO users(email, pw_hash, created_at) VALUES (?, ?, ?)",
                          (email, ph.hash(password), db.now()))
        uid = cur.lastrowid
        db.get_prefs(con, uid)
    return login_response(uid, "/alerts?welcome=1")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "auth.html", mode="login", error=None)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form("")):
    check_csrf(request, csrf)
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    if too_many_attempts(ip):
        return render(request, "auth.html", mode="login", error="Too many attempts. Wait 15 minutes and try again.")
    with db.tx() as con:
        row = con.execute("SELECT id, pw_hash FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    try:
        if not row:
            raise VerifyMismatchError
        ph.verify(row["pw_hash"], password)
    except VerifyMismatchError:
        return render(request, "auth.html", mode="login", error="Email or password is incorrect.")
    return login_response(row["id"])


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    check_csrf(request, csrf)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- alert settings
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
def alerts_page(request: Request, welcome: int = 0, msg: str = "", err: str = ""):
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
    return render(request, "alerts.html", channels=chans, prefs=prefs, status=status, catalog=CHANNELS,
                  welcome=welcome, msg=msg, err=err, tg_link=tg_link)


@app.post("/alerts/channel")
def add_channel(request: Request, kind: str = Form(...), value: str = Form(""), secret: str = Form(""),
                label: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    u = require_user(request)
    spec = next((c for c in CHANNELS if c["kind"] == kind and c["field"]), None)
    if not spec:
        return RedirectResponse("/alerts?err=Unknown+channel", status_code=303)
    if not notify.channel_status()[kind]["available"]:
        return RedirectResponse(f"/alerts?err={spec['name']}+is+not+enabled+on+this+server+yet", status_code=303)
    target = {spec["field"]: value.strip()}
    if kind == "webhook" and secret.strip():
        target["secret"] = secret.strip()
    ok, error = notify.validate_target(kind, target)
    if not ok:
        return RedirectResponse(f"/alerts?err={error}", status_code=303)
    with db.tx() as con:
        n = con.execute("SELECT COUNT(*) FROM channels WHERE user_id=?", (u["id"],)).fetchone()[0]
        if n >= 10:
            return RedirectResponse("/alerts?err=Limit+of+10+channels+reached", status_code=303)
        cur = con.execute("INSERT INTO channels(user_id, kind, target, label, created_at) VALUES (?, ?, ?, ?, ?)",
                          (u["id"], kind, json.dumps(target), label.strip()[:40] or None, db.now()))
        cid = cur.lastrowid
    return _send_test(u["id"], cid, added=True)


def _send_test(uid, cid, added=False):
    with db.tx() as con:
        row = con.execute("SELECT * FROM channels WHERE id=? AND user_id=?", (cid, uid)).fetchone()
        if not row:
            return RedirectResponse("/alerts?err=Channel+not+found", status_code=303)
        msg = notify.Message(title="Leadtime alerts are connected",
                             body="This is a test. You'll get call changes and discovery flags here, based on your preferences.",
                             url=SITE_URL + "/alerts", severity="info")
        res = notify.send(row["kind"], json.loads(row["target"]), msg)
        con.execute("UPDATE channels SET last_ok_at=?, last_error=? WHERE id=?",
                    (db.now() if res.ok else row["last_ok_at"], None if res.ok else (res.error or "failed")[:200], cid))
    if res.ok:
        return RedirectResponse(f"/alerts?msg={'Channel+added.+' if added else ''}Test+message+sent.", status_code=303)
    return RedirectResponse(f"/alerts?err={'Saved,+but+the+' if added else 'The+'}test+message+failed:+{(res.error or '')[:120]}",
                            status_code=303)


@app.post("/alerts/channel/{cid}/test")
def test_channel(request: Request, cid: int, csrf: str = Form("")):
    check_csrf(request, csrf)
    u = require_user(request)
    return _send_test(u["id"], cid)


@app.post("/alerts/channel/{cid}/delete")
def delete_channel(request: Request, cid: int, csrf: str = Form("")):
    check_csrf(request, csrf)
    u = require_user(request)
    with db.tx() as con:
        con.execute("DELETE FROM channels WHERE id=? AND user_id=?", (cid, u["id"]))
    return RedirectResponse("/alerts?msg=Channel+removed.", status_code=303)


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
    return RedirectResponse("/alerts?msg=Preferences+saved.", status_code=303)


@app.post("/account/delete")
def delete_account(request: Request, csrf: str = Form(""), confirm: str = Form("")):
    check_csrf(request, csrf)
    u = require_user(request)
    if confirm.strip().lower() != u["email"]:
        return RedirectResponse("/alerts?err=Type+your+email+to+confirm+deletion", status_code=303)
    with db.tx() as con:
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
