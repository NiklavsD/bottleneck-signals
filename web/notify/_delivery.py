"""Provider payloads and bounded, synchronous delivery."""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parsedate_to_datetime
import base64
import hashlib
import hmac
from html import escape
import json
import math
import os
import smtplib
import ssl
import time
from typing import Literal

import httpx

from ._redaction import redact_transport_logs
from ._validation import CHANNELS, valid_url, validate_target

TIMEOUT = 10.0
COLORS = {"info": 0x3B82F6, "change": 0xF59E0B, "flag": 0x10B981}
_REQUIRED = {
    "telegram": ("TELEGRAM_BOT_TOKEN",),
    "email": ("SMTP_HOST", "SMTP_FROM"),
    "sms": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"),
}


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    url: str | None = None
    severity: Literal["info", "change", "flag"] = "info"


@dataclass(frozen=True)
class Result:
    ok: bool
    error: str | None = None
    provider_id: str | None = None


def channel_status() -> dict[str, dict[str, bool | str]]:
    status = {}
    for channel in CHANNELS:
        missing = [key for key in _REQUIRED.get(channel, ()) if not os.environ.get(key, "").strip()]
        status[channel] = {
            "available": not missing,
            "reason": "Missing environment variables: " + ", ".join(missing) if missing else "Available.",
        }
    return status


def _message_error(msg: Message) -> str | None:
    if not isinstance(msg, Message):
        return "msg must be a Message."
    if not isinstance(msg.title, str) or not msg.title.strip() or any(ord(c) < 32 or ord(c) == 127 for c in msg.title):
        return "Title must be nonempty text without control characters."
    if not isinstance(msg.body, str) or len(msg.body) > 1500:
        return "Body must be plain text of at most 1500 characters."
    if not isinstance(msg.severity, str) or msg.severity not in COLORS:
        return "Severity must be info, change or flag."
    if msg.url is not None and not valid_url(msg.url, https_only=False):
        return "Message URL must be an HTTP or HTTPS URL without credentials."
    return None


def _retry_delay(value: str | None) -> float:
    if value is None:
        return 1.0
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            return 1.0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 1.0
    return min(5.0, max(0.0, seconds))


def _post(url: str, **kwargs) -> httpx.Response:
    # Redirects must not forward a signed payload or credentials elsewhere.
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
        for attempt in range(2):
            response = client.post(url, **kwargs)
            if attempt == 0 and (response.status_code == 429 or 500 <= response.status_code <= 599):
                time.sleep(_retry_delay(response.headers.get("Retry-After")))
                continue
            return response
    raise RuntimeError("Unreachable retry state")


def _payload(channel: str, target: dict, msg: Message) -> tuple[str, dict]:
    if channel == "discord":
        embed = {"title": msg.title[:256], "description": msg.body, "color": COLORS[msg.severity]}
        if msg.url:
            embed["url"] = msg.url
        return target["webhook_url"], {"json": {"embeds": [embed]}, "params": {"wait": "true"}}
    if channel == "slack":
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": msg.title[:150]}}]
        if msg.body:
            blocks.append({"type": "section", "text": {"type": "plain_text", "text": msg.body}})
        if msg.url:
            blocks.append({"type": "actions", "elements": [{
                "type": "button", "text": {"type": "plain_text", "text": "View details"}, "url": msg.url,
            }]})
        return target["webhook_url"], {"json": {"text": msg.title, "blocks": blocks}}
    if channel == "webhook":
        payload = {
            "title": msg.title, "body": msg.body, "url": msg.url, "severity": msg.severity,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if "secret" in target:
            headers["X-Leadtime-Signature"] = hmac.new(target["secret"].encode("utf-8"), raw, hashlib.sha256).hexdigest()
        return target["url"], {"content": raw, "headers": headers}
    if channel == "ntfy":
        title = msg.title
        if not title.isascii():
            title = "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode("ascii") + "?="
        headers = {
            "Title": title, "Priority": "3" if msg.severity == "info" else "4",
            "Tags": {"info": "information_source", "change": "warning", "flag": "triangular_flag_on_post"}[msg.severity],
            "Content-Type": "text/plain; charset=utf-8",
        }
        if msg.url:
            headers["Click"] = str(httpx.URL(msg.url))
        return target.get("server", "https://ntfy.sh").rstrip("/") + "/" + target["topic"], {
            "content": msg.body.encode("utf-8"), "headers": headers,
        }
    if channel == "telegram":
        text = f"<b>{escape(msg.title)}</b>\n\n{escape(msg.body)}"
        if msg.url:
            text += f'\n\n<a href="{escape(msg.url, quote=True)}">View details</a>'
        return f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage", {
            "json": {"chat_id": target["chat_id"], "text": text, "parse_mode": "HTML"},
        }
    # SMS preserves the supplied URL in full and truncates only the title.
    suffix = " " + msg.url if msg.url else ""
    room = 320 - len(suffix)
    if room < 1:
        raise ValueError("SMS URL is too long.")
    body = msg.title[:room] + suffix
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    return f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", {
        "data": {"To": target["phone"], "From": os.environ["TWILIO_FROM"], "Body": body},
        "auth": (sid, os.environ["TWILIO_AUTH_TOKEN"]),
    }


def _send_email(target: dict, msg: Message) -> Result:
    mail = EmailMessage()
    mail["Subject"] = msg.title
    mail["From"] = os.environ["SMTP_FROM"]
    mail["To"] = target["address"]
    mail["Message-ID"] = make_msgid()
    mail.set_content(msg.body + ("\n\n" + msg.url if msg.url else ""))
    body_html = escape(msg.body).replace("\n", "<br>\n")
    html = f"<html><body><h1>{escape(msg.title)}</h1><p>{body_html}</p>"
    if msg.url:
        html += f'<p><a href="{escape(msg.url, quote=True)}">View details</a></p>'
    mail.add_alternative(html + "</body></html>", subtype="html")
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=TIMEOUT) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        if os.environ.get("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
        refused = smtp.send_message(mail)
        if refused:
            return Result(False, "SMTP rejected the recipient.")
    return Result(True, provider_id=str(mail["Message-ID"]))


def _http_result(channel: str, response: httpx.Response) -> Result:
    if not 200 <= response.status_code < 300:
        return Result(False, f"{channel} returned HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if channel == "telegram":
        if data.get("ok") is not True or not isinstance(data.get("result"), dict):
            return Result(False, "Telegram did not confirm delivery.")
        provider_id = data["result"].get("message_id")
    else:
        if data.get("ok") is False:
            return Result(False, f"{channel} rejected the message.")
        provider_id = data.get("sid") if channel == "sms" else data.get("id")
    return Result(True, provider_id=str(provider_id) if isinstance(provider_id, (str, int)) else None)


def send(channel: str, target: dict, msg: Message) -> Result:
    """Return failures as data; never include provider bodies or exception text."""
    try:
        ok, error = validate_target(channel, target)
        if not ok:
            return Result(False, error)
        if error := _message_error(msg):
            return Result(False, error)
        status = channel_status()[channel]
        if not status["available"]:
            return Result(False, str(status["reason"]))
        with redact_transport_logs():
            if channel == "email":
                return _send_email(target, msg)
            url, kwargs = _payload(channel, target, msg)
            return _http_result(channel, _post(url, **kwargs))
    except httpx.TimeoutException:
        return Result(False, "Delivery timed out.")
    except httpx.RequestError:
        return Result(False, "Delivery failed due to a network error.")
    except smtplib.SMTPException:
        return Result(False, "Delivery failed due to an SMTP error.")
    except Exception:
        return Result(False, "Delivery failed; check message and provider configuration.")
