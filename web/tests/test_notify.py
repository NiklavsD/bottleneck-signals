import base64
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import hashlib
import hmac
import json
import logging
import smtplib
import ssl
import socket
import threading
from urllib.parse import parse_qs

import httpx
import pytest

from notify import Message, Result, channel_status, send, validate_target
from notify import _delivery
from notify import _validation
from notify._redaction import mask_url

REAL_HTTP_HANDLE = httpx.HTTPTransport.handle_request


TARGETS = {
    "discord": {"webhook_url": "https://discord.com/api/webhooks/123/discord-secret"},
    "slack": {"webhook_url": "https://hooks.slack.com/services/T123/B456/slack-secret"},
    "webhook": {"url": "https://example.com/hook?key=webhook-secret", "secret": "signing-secret"},
    "ntfy": {"topic": "leadtime_alerts"},
    "telegram": {"chat_id": -12345},
    "email": {"address": "user@example.com"},
    "sms": {"phone": "+14155550123"},
}
MSG = Message("News <&>", "Plain <script>alert('x')</script> & text\nNext line", "https://example.com/a?x=1&y=2", "flag")


@pytest.mark.parametrize("channel,target", list(TARGETS.items()) + [
    ("discord", {"webhook_url": "https://discordapp.com/api/webhooks/123/a_B-9"}),
    ("webhook", {"url": "https://hooks.example.com:8443/hook"}),
    ("ntfy", {"topic": "a" * 64, "server": "https://ntfy.example.com/base/"}),
    ("telegram", {"chat_id": "12345"}),
    ("telegram", {"chat_id": "@my_channel"}),
    ("email", {"address": "first.last+alerts@sub.example.com"}),
    ("sms", {"phone": "+" + "1" * 15}),
])
def test_valid_targets(channel, target):
    assert validate_target(channel, target) == (True, None)


@pytest.mark.parametrize("channel,target", [
    ("discord", {"webhook_url": "http://discord.com/api/webhooks/123/token"}),
    ("discord", {"webhook_url": "https://discord.com.evil.test/api/webhooks/123/token"}),
    ("discord", {"webhook_url": "https://discord.com/api/webhooks/x/token"}),
    ("discord", {"webhook_url": "https://discord.com/api/webhooks/123/token?wait=true"}),
    ("slack", {"webhook_url": "https://hooks.slack.com.evil.test/services/T/B/token"}),
    ("slack", {"webhook_url": "https://hooks.slack.com/services/T/B"}),
    ("slack", {"webhook_url": "http://hooks.slack.com/services/T/B/token"}),
    ("webhook", {"url": "http://example.com"}),
    ("webhook", {"url": "https://"}),
    ("webhook", {"url": "https://example.com:99999/hook"}),
    ("webhook", {"url": "https://user:secret@example.com/hook"}),
    ("webhook", {"url": "https://example.com/hook#secret"}),
    ("webhook", {"url": "https://example.com\n/hook"}),
    ("webhook", {"url": "https://example.com", "secret": 123}),
    ("webhook", {"url": "https://[invalid"}),
    ("webhook", {"url": "https://localhost:8443/hook"}),
    ("webhook", {"url": "https://[::1]/hook"}),
    ("webhook", {"url": "https://127.0.0.1/hook"}),
    ("webhook", {"url": "https://10.0.0.1/hook"}),
    ("webhook", {"url": "https://93.184.216.34/hook"}),
    ("webhook", {"url": "https://api.localhost/hook"}),
    ("webhook", {"url": "https://host.local/hook"}),
    ("ntfy", {"topic": "short"}),
    ("ntfy", {"topic": "a" * 65}),
    ("ntfy", {"topic": "topic/name"}),
    ("ntfy", {"topic": "通知通知通知"}),
    ("ntfy", {"topic": "valid_topic", "server": "http://ntfy.sh"}),
    ("ntfy", {"topic": "valid_topic", "server": "https://ntfy.sh?query=1"}),
    ("telegram", {"chat_id": True}),
    ("telegram", {"chat_id": 0}),
    ("telegram", {"chat_id": 1.2}),
    ("telegram", {"chat_id": "bad chat"}),
    ("email", {"address": "no-at-sign"}),
    ("email", {"address": "User <user@example.com>"}),
    ("email", {"address": "user@example.com\r\nBcc: other@example.com"}),
    ("email", {"address": ".user@example.com"}),
    ("email", {"address": "user..name@example.com"}),
    ("sms", {"phone": "14155550123"}),
    ("sms", {"phone": "+0123456789"}),
    ("sms", {"phone": "+1 415 555 0123"}),
    ("sms", {"phone": "+" + "1" * 16}),
    ("sms", {"phone": "+١٢٣٤٥٦٧٨٩"}),
    ("missing", {}),
    ([], {}),
    ("email", None),
])
def test_invalid_targets(channel, target):
    ok, error = validate_target(channel, target)
    assert not ok and error


@pytest.mark.parametrize("channel", TARGETS)
@pytest.mark.parametrize("target", [{}, None, {key: None for key in ("webhook_url", "url", "topic", "chat_id", "address", "phone")}])
def test_missing_target_values(channel, target):
    assert validate_target(channel, target)[0] is False


def test_channel_status_environment(monkeypatch):
    statuses = channel_status()
    assert set(statuses) == set(TARGETS)
    for channel in ("discord", "slack", "webhook", "ntfy"):
        assert statuses[channel] == {"available": True, "reason": "Available."}
    for channel in ("telegram", "email", "sms"):
        assert statuses[channel]["available"] is False
        assert "Missing environment variables:" in statuses[channel]["reason"]
    for name in ("TELEGRAM_BOT_TOKEN", "SMTP_HOST", "SMTP_FROM", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"):
        monkeypatch.setenv(name, "sensitive-value")
    assert all(status["available"] for status in channel_status().values())
    assert "sensitive-value" not in repr(channel_status())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  ")
    monkeypatch.delenv("SMTP_FROM")
    monkeypatch.delenv("TWILIO_FROM")
    for channel in ("telegram", "email", "sms"):
        assert channel_status()[channel]["available"] is False


@pytest.mark.parametrize("channel", ["telegram", "email", "sms"])
def test_unavailable_channel(channel):
    result = send(channel, TARGETS[channel], MSG)
    assert not result.ok and "Missing environment variables:" in result.error


@pytest.mark.parametrize("channel", ["discord", "slack", "webhook", "ntfy", "telegram", "sms"])
def test_http_payloads(channel, http_mock, credentials):
    replies = {"discord": {"id": "d1"}, "ntfy": {"id": "n1"}, "telegram": {"ok": True, "result": {"message_id": 42}}, "sms": {"sid": "SM1"}}
    requests, options = http_mock(lambda request: httpx.Response(200, json=replies.get(channel, {})))
    result = send(channel, TARGETS[channel], MSG)
    assert result.ok and result.error is None
    assert result.provider_id == {"discord": "d1", "ntfy": "n1", "telegram": "42", "sms": "SM1"}.get(channel)
    assert options == [{"timeout": 10.0, "follow_redirects": False, "trust_env": False}]
    request, = requests
    assert request.method == "POST"
    assert 0 < request.extensions["timeout"]["connect"] <= 10.0
    assert {request.extensions["timeout"][key] for key in ("read", "write", "pool")} == {10.0}
    if channel == "discord":
        assert json.loads(request.content) == {"embeds": [{"title": MSG.title, "description": MSG.body, "url": MSG.url, "color": 0x10B981}]}
        assert request.url.params["wait"] == "true"
    elif channel == "slack":
        payload = json.loads(request.content)
        assert payload["text"] == MSG.title
        assert payload["blocks"][0] == {"type": "header", "text": {"type": "plain_text", "text": MSG.title}}
        assert payload["blocks"][1]["text"] == {"type": "plain_text", "text": MSG.body}
        assert payload["blocks"][2]["elements"][0]["url"] == MSG.url
    elif channel == "webhook":
        payload = json.loads(request.content)
        assert set(payload) == {"title", "body", "url", "severity", "sent_at"}
        assert {key: payload[key] for key in ("title", "body", "url", "severity")} == {"title": MSG.title, "body": MSG.body, "url": MSG.url, "severity": MSG.severity}
        assert datetime.fromisoformat(payload["sent_at"]).utcoffset() == timedelta(0)
        assert request.headers["X-Leadtime-Signature"] == hmac.new(b"signing-secret", request.content, hashlib.sha256).hexdigest()
        assert request.headers["Content-Type"] == "application/json"
    elif channel == "ntfy":
        assert str(request.url) == "https://ntfy.sh/leadtime_alerts"
        assert request.content.decode() == MSG.body
        for key, value in {"Title": MSG.title, "Click": MSG.url, "Priority": "4", "Tags": "triangular_flag_on_post"}.items():
            assert request.headers[key] == value
    elif channel == "telegram":
        assert str(request.url) == "https://api.telegram.org/bot123:telegram-secret/sendMessage"
        payload = json.loads(request.content)
        assert payload["chat_id"] == -12345
        assert payload["parse_mode"] == "HTML"
        assert payload["text"].startswith("<b>News &lt;&amp;&gt;</b>")
        assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; text" in payload["text"]
        assert '<a href="https://example.com/a?x=1&amp;y=2">View details</a>' in payload["text"]
    else:
        assert str(request.url) == "https://api.twilio.com/2010-04-01/Accounts/ACexample/Messages.json"
        assert parse_qs(request.content.decode()) == {"To": ["+14155550123"], "From": ["+14155550100"], "Body": [MSG.title + " " + MSG.url]}
        assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"ACexample:twilio-secret").decode()


@pytest.mark.parametrize("severity,color,priority", [("info", 0x3B82F6, "3"), ("change", 0xF59E0B, "4"), ("flag", 0x10B981, "4")])
def test_severity_mapping(severity, color, priority, http_mock):
    requests, _ = http_mock(lambda request: httpx.Response(200, json={"id": "1"}))
    msg = Message("News", "Body", severity=severity)
    assert send("discord", TARGETS["discord"], msg).ok
    assert json.loads(requests[0].content)["embeds"][0]["color"] == color
    assert send("ntfy", TARGETS["ntfy"], msg).ok
    assert requests[1].headers["Priority"] == priority


@pytest.mark.parametrize("channel", ["discord", "slack", "webhook", "ntfy", "telegram", "sms"])
def test_optional_link(channel, http_mock, credentials):
    requests, _ = http_mock(lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
    assert send(channel, TARGETS[channel], Message("Title", "Body")).ok
    request = requests[0]
    if channel == "discord":
        assert "url" not in json.loads(request.content)["embeds"][0]
    elif channel == "slack":
        assert len(json.loads(request.content)["blocks"]) == 2
    elif channel == "webhook":
        assert json.loads(request.content)["url"] is None
    elif channel == "ntfy":
        assert "Click" not in request.headers
    elif channel == "telegram":
        assert "<a " not in json.loads(request.content)["text"]
    else:
        assert parse_qs(request.content.decode())["Body"] == ["Title"]


def test_unicode_ntfy_and_custom_server(http_mock):
    requests, _ = http_mock(lambda request: httpx.Response(200, json={}))
    assert send("ntfy", {"topic": "alerts_topic", "server": "https://ntfy.example.com/base/"}, Message("News 🚀", "Résumé")).ok
    assert str(requests[0].url) == "https://ntfy.example.com/base/alerts_topic"
    assert requests[0].headers["Title"] == "=?UTF-8?B?" + base64.b64encode("News 🚀".encode()).decode() + "?="
    assert requests[0].content == "Résumé".encode()


@pytest.mark.parametrize("secret", [None, "", "秘密"])
def test_optional_webhook_signature(secret, http_mock):
    requests, _ = http_mock(lambda request: httpx.Response(204))
    target = {"url": "https://example.com/hook"}
    if secret is not None:
        target["secret"] = secret
    assert send("webhook", target, Message("News", "Résumé")).ok
    if secret is None:
        assert "X-Leadtime-Signature" not in requests[0].headers
    else:
        assert requests[0].headers["X-Leadtime-Signature"] == hmac.new(secret.encode(), requests[0].content, hashlib.sha256).hexdigest()


def test_sms_length_and_url_preservation(http_mock, credentials):
    requests, _ = http_mock(lambda request: httpx.Response(201, json={"sid": "SM1"}))
    assert send("sms", TARGETS["sms"], Message("t" * 500, "ignored", "https://short.test/x")).ok
    body = parse_qs(requests[0].content.decode())["Body"][0]
    assert len(body) == 320 and body.endswith(" https://short.test/x")
    assert not send("sms", TARGETS["sms"], Message("Title", "", "https://example.com/" + "x" * 320)).ok
    assert len(requests) == 1


@pytest.fixture
def smtp_mock(monkeypatch):
    class SMTP:
        def __init__(self, *args, **kwargs):
            self.calls = [("connect", args, kwargs)]
            self.refused = {}
            self.error = None
            self.mail = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.calls.append("close")

        def ehlo(self):
            self.calls.append("ehlo")

        def starttls(self, *, context):
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            self.calls.append("starttls")

        def login(self, user, password):
            self.calls.append(("login", user, password))

        def send_message(self, mail):
            self.calls.append("send")
            self.mail = mail
            if self.error:
                raise self.error
            return self.refused

    smtp = SMTP("smtp.example.com", 587, timeout=10.0)

    def connect(*args, **kwargs):
        smtp.calls[0] = ("connect", args, kwargs)
        return smtp

    monkeypatch.setattr(_delivery.smtplib, "SMTP", connect)
    return smtp


def test_email_payload(credentials, smtp_mock):
    result = send("email", TARGETS["email"], MSG)
    assert result.ok and result.provider_id == smtp_mock.mail["Message-ID"]
    assert smtp_mock.calls == [("connect", ("smtp.example.com", 587), {"timeout": 10.0}), "ehlo", "starttls", "ehlo", ("login", "smtp-user", "smtp-secret"), "send", "close"]
    mail = smtp_mock.mail
    assert mail["Subject"] == MSG.title and mail["To"] == "user@example.com" and mail["From"] == "alerts@example.com"
    assert mail.get_content_type() == "multipart/alternative"
    plain, html = list(mail.iter_parts())
    assert plain.get_content_type() == "text/plain" and html.get_content_type() == "text/html"
    assert MSG.body in plain.get_content() and MSG.url in plain.get_content()
    assert "News &lt;&amp;&gt;" in html.get_content() and "&lt;script&gt;" in html.get_content()
    assert "<script>" not in html.get_content()
    assert 'href="https://example.com/a?x=1&amp;y=2"' in html.get_content()


def test_email_defaults_and_no_link(credentials, smtp_mock, monkeypatch):
    for name in ("SMTP_PORT", "SMTP_USER", "SMTP_PASS"):
        monkeypatch.delenv(name)
    assert send("email", TARGETS["email"], Message("Title", "Body")).ok
    assert smtp_mock.calls[0][1][1] == 587
    assert not any(isinstance(call, tuple) and call[0] == "login" for call in smtp_mock.calls)
    assert "<a " not in smtp_mock.mail.get_body(preferencelist=("html",)).get_content()


@pytest.mark.parametrize("failure", ["refused", "exception", "port"])
def test_email_failure(credentials, smtp_mock, monkeypatch, failure):
    if failure == "refused":
        smtp_mock.refused = {"user@example.com": (550, b"smtp-secret")}
    elif failure == "exception":
        smtp_mock.error = smtplib.SMTPAuthenticationError(535, b"smtp-secret")
    else:
        monkeypatch.setenv("SMTP_PORT", "invalid")
    result = send("email", TARGETS["email"], MSG)
    assert not result.ok and result.error and "smtp-secret" not in result.error


@pytest.mark.parametrize("status", [429, 500, 502, 503, 599])
def test_retry_once(status, http_mock, monkeypatch):
    responses = iter([httpx.Response(status, headers={"Retry-After": "2"}), httpx.Response(200, json={"id": "ok"})])
    requests, _ = http_mock(lambda request: next(responses))
    sleeps = []
    monkeypatch.setattr(_delivery.time, "sleep", sleeps.append)
    assert send("webhook", TARGETS["webhook"], MSG) == Result(True, provider_id="ok")
    assert len(requests) == 2 and sleeps == [2.0]
    assert requests[0].content == requests[1].content
    assert requests[0].headers["X-Leadtime-Signature"] == requests[1].headers["X-Leadtime-Signature"]


@pytest.mark.parametrize("header,delay", [("100", 5.0), ("0", 0.0), ("-5", 0.0), ("garbage", 1.0), (None, 1.0), ("nan", 1.0), ("inf", 1.0)])
def test_retry_after(header, delay, http_mock, monkeypatch):
    requests, _ = http_mock(lambda request: httpx.Response(429, headers={"Retry-After": header} if header is not None else {}))
    sleeps = []
    monkeypatch.setattr(_delivery.time, "sleep", sleeps.append)
    result = send("discord", TARGETS["discord"], MSG)
    assert not result.ok and "429" in result.error
    assert len(requests) == 2 and sleeps == [delay]


@pytest.mark.parametrize("delta,expected", [(60, 5.0), (-60, 0.0)])
def test_retry_after_http_date(delta, expected, http_mock, monkeypatch):
    header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=delta), usegmt=True)
    requests, _ = http_mock(lambda request: httpx.Response(503, headers={"Retry-After": header}))
    sleeps = []
    monkeypatch.setattr(_delivery.time, "sleep", sleeps.append)
    assert not send("slack", TARGETS["slack"], MSG).ok
    assert len(requests) == 2 and sleeps == [expected]


@pytest.mark.parametrize("status", [301, 307, 400, 401, 403, 404, 422])
def test_other_statuses_not_retried(status, http_mock):
    requests, _ = http_mock(lambda request: httpx.Response(status, headers={"Location": "https://other.test"}, text="discord-secret"))
    result = send("discord", TARGETS["discord"], MSG)
    assert not result.ok and str(status) in result.error and "discord-secret" not in result.error
    assert len(requests) == 1


@pytest.mark.parametrize("exception", [httpx.ReadTimeout("discord-secret"), httpx.ConnectError("discord-secret"), RuntimeError("discord-secret")])
def test_network_exceptions_do_not_escape_or_retry(exception, http_mock):
    def handler(request):
        raise exception
    requests, _ = http_mock(handler)
    result = send("discord", TARGETS["discord"], MSG)
    assert not result.ok and result.error and "discord-secret" not in result.error
    assert len(requests) == 1


@pytest.mark.parametrize("msg", [None, {}, Message("", "Body"), Message("Title\r\nBcc: x", "Body"), Message("Title", "x" * 1501), Message("Title", 123), Message("Title", "Body", severity="bad"), Message("Title", "Body", severity=[]), Message("Title", "Body", "javascript:alert(1)")])
def test_invalid_message_returns_result(msg):
    result = send("webhook", TARGETS["webhook"], msg)
    assert not result.ok and result.error


def test_maximum_body_length(http_mock):
    http_mock(lambda request: httpx.Response(204))
    assert send("webhook", TARGETS["webhook"], Message("Title", "x" * 1500)).ok


@pytest.mark.parametrize("payload", [{"ok": False, "description": "telegram-secret"}, {}, [], {"ok": True, "result": None}])
def test_telegram_rejection_and_malformed_response(payload, credentials, http_mock):
    http_mock(lambda request: httpx.Response(200, json=payload))
    result = send("telegram", TARGETS["telegram"], MSG)
    assert not result.ok and "telegram-secret" not in result.error


def test_slack_plain_ok_response(http_mock):
    http_mock(lambda request: httpx.Response(200, text="ok"))
    assert send("slack", TARGETS["slack"], MSG) == Result(True)


@pytest.mark.parametrize("url,secret", [
    (TARGETS["discord"]["webhook_url"], "discord-secret"),
    ("https://discordapp.com/api/webhooks/123/legacy-secret", "legacy-secret"),
    (TARGETS["slack"]["webhook_url"], "slack-secret"),
    ("https://api.telegram.org/bot123:telegram-secret/sendMessage", "123:telegram-secret"),
])
def test_masking(url, secret):
    masked = mask_url(url)
    assert secret not in masked and "[REDACTED]" in masked
    assert masked.startswith("https://")


@pytest.mark.parametrize("channel", ["discord", "slack", "webhook", "ntfy", "telegram", "sms"])
def test_transport_logs_are_redacted(channel, credentials, http_mock, caplog):
    def handler(request):
        logging.getLogger("httpcore.http11").debug("headers: %s", "Bearer super-secret " + str(request.url))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    http_mock(handler)
    with caplog.at_level(logging.DEBUG):
        assert send(channel, TARGETS[channel], MSG).ok
    assert "HTTP Request: POST" in caplog.text and "[REDACTED" in caplog.text
    for secret in ("discord-secret", "slack-secret", "telegram-secret", "webhook-secret", "signing-secret", "twilio-secret", "super-secret", "ACexample"):
        assert secret not in caplog.text


def test_logging_context_is_restored(http_mock, caplog):
    def failure(request):
        raise httpx.ConnectError("secret")
    http_mock(failure)
    with caplog.at_level(logging.DEBUG):
        assert not send("slack", TARGETS["slack"], MSG).ok
        logging.getLogger("httpcore.http11").debug("outside delivery")
    assert "outside delivery" in caplog.text


def dns_answers(*addresses, port=443):
    return [(socket.AF_INET6 if ":" in address else socket.AF_INET,
             socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))
            for address in addresses]


@pytest.mark.parametrize("addresses", [
    ("127.0.0.1",), ("10.0.0.1",), ("169.254.169.254",), ("192.168.1.1",),
    ("172.16.0.1",), ("100.64.0.1",), ("0.0.0.0",), ("224.0.0.1",),
    ("192.0.2.1",), ("240.0.0.1",), ("::1",), ("fc00::1",), ("fe80::1",),
    ("2001:db8::1",), ("ff02::1",), ("93.184.216.34", "127.0.0.1"),
])
@pytest.mark.parametrize("channel,target", [
    ("webhook", {"url": "https://hooks.example.test/hook"}),
    ("ntfy", {"topic": "public_topic", "server": "https://ntfy.example.test"}),
])
def test_private_dns_rejected_before_transport(addresses, channel, target, http_mock, monkeypatch):
    requests, _ = http_mock(lambda request: httpx.Response(204))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: dns_answers(*addresses))
    result = send(channel, target, MSG)
    assert not result.ok and "public addresses" in result.error
    assert requests == []


def test_dns_failure_returns_safe_error(http_mock, monkeypatch):
    requests, _ = http_mock(lambda request: httpx.Response(204))
    def failed_dns(*args, **kwargs):
        raise socket.gaierror("secret-host.example.test")
    monkeypatch.setattr(socket, "getaddrinfo", failed_dns)
    result = send("webhook", TARGETS["webhook"], MSG)
    assert result.error == "Could not resolve endpoint."
    assert requests == []


def test_retry_rechecks_dns_and_blocks_rebinding(http_mock, monkeypatch):
    requests, _ = http_mock(lambda request: httpx.Response(503))
    answers = iter([dns_answers("93.184.216.34"), dns_answers("127.0.0.1")])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(_delivery.time, "sleep", lambda seconds: None)
    result = send("webhook", TARGETS["webhook"], MSG)
    assert not result.ok and "public addresses" in result.error
    assert len(requests) == 1


def test_custom_endpoint_redirect_is_not_followed(http_mock):
    requests, options = http_mock(lambda request: httpx.Response(
        307, headers={"Location": "https://127.0.0.1/admin"}))
    assert not send("webhook", TARGETS["webhook"], MSG).ok
    assert len(requests) == 1 and options[0]["follow_redirects"] is False


def test_provider_response_size_is_bounded(http_mock):
    requests, _ = http_mock(lambda request: httpx.Response(
        200, content=b"x" * (_delivery.MAX_RESPONSE_BYTES + 1)))
    result = send("webhook", TARGETS["webhook"], MSG)
    assert not result.ok and result.error == "Delivery failed due to a network error."
    assert len(requests) == 1


def test_provider_response_total_read_time_is_bounded(http_mock, monkeypatch):
    clock = [0.0]
    def response(request):
        clock[0] = 2 * _delivery.TIMEOUT + 1
        return httpx.Response(200, content=b"ok")
    requests, _ = http_mock(response)
    monkeypatch.setattr(_delivery.time, "monotonic", lambda: clock[0])
    result = send("webhook", TARGETS["webhook"], MSG)
    assert not result.ok and result.error == "Delivery timed out."
    assert len(requests) == 1


def test_public_address_failover_preserves_host_tls_and_total_budget(monkeypatch):
    from httpcore import ConnectError
    lookups, connections, tls_hosts, writes = [], [], [], []
    clock = [0.0]
    def resolve(host, port, **kwargs):
        lookups.append(host)
        return dns_answers("2001:4860:4860::8888", "2001:4860:4860::8844", "93.184.216.34")
    class Stream:
        def read(self, max_bytes, timeout=None):
            return b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
        def write(self, buffer, timeout=None):
            writes.append(buffer)
        def close(self):
            pass
        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            assert ssl_context.check_hostname and ssl_context.verify_mode == ssl.CERT_REQUIRED
            tls_hosts.append(server_hostname)
            return self
        def get_extra_info(self, name):
            return None
    class Backend:
        def connect_tcp(self, host, port, timeout=None, **kwargs):
            connections.append((host, timeout))
            if ":" in host:
                clock[0] += 2
                raise ConnectError("IPv6 route unavailable")
            return Stream()
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", REAL_HTTP_HANDLE)
    monkeypatch.setattr(_delivery.time, "monotonic", lambda: clock[0])
    transport = _delivery.PublicHTTPTransport(trust_env=False)
    transport._pool._network_backend = Backend()
    with httpx.Client(transport=transport, trust_env=False, timeout=_delivery.TIMEOUT) as client:
        assert client.post("https://hooks.example.test/hook", content=b"payload").status_code == 204
    assert lookups == ["hooks.example.test"]
    assert [host for host, timeout in connections] == ["2001:4860:4860::8888", "93.184.216.34"]
    assert [timeout for host, timeout in connections] == pytest.approx([10.0 / 3, 4.0])
    assert tls_hosts == ["hooks.example.test"]
    assert b"Host: hooks.example.test" in b"".join(writes)


def test_address_failover_does_not_repeat_a_post_after_read_failure(http_mock, monkeypatch):
    requests, _ = http_mock(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("accepted but no response")))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: dns_answers("93.184.216.34", "1.1.1.1"))
    assert not send("webhook", TARGETS["webhook"], MSG).ok
    assert len(requests) == 1


def test_dns_lookup_wait_is_bounded(http_mock, monkeypatch):
    requests, _ = http_mock(lambda request: httpx.Response(204))
    release, entered = threading.Event(), threading.Event()
    def slow_dns(*args, **kwargs):
        entered.set()
        release.wait(5)
        return dns_answers("93.184.216.34")
    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    monkeypatch.setattr(_validation, "DNS_TIMEOUT_SECONDS", 0.0)
    try:
        result = send("webhook", TARGETS["webhook"], MSG)
        assert result.error == "Endpoint DNS lookup timed out."
        assert entered.wait(1) and requests == []
    finally:
        release.set()


def test_effective_connection_is_pinned_and_tls_host_preserved(monkeypatch):
    # Exercise real httpx/httpcore connection setup with an in-memory socket
    # backend. No network is opened, and a second lookup would return loopback.
    lookups, connections, tls_hosts, writes = [], [], [], []
    def resolve(host, port, **kwargs):
        lookups.append((host, port))
        return dns_answers("93.184.216.34" if len(lookups) == 1 else "127.0.0.1", port=port)
    class Stream:
        def read(self, max_bytes, timeout=None):
            return b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
        def write(self, buffer, timeout=None):
            writes.append(buffer)
        def close(self):
            pass
        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            assert ssl_context.check_hostname and ssl_context.verify_mode == ssl.CERT_REQUIRED
            tls_hosts.append(server_hostname)
            return self
        def get_extra_info(self, name):
            return None
    class Backend:
        def connect_tcp(self, host, port, **kwargs):
            connections.append((host, port))
            return Stream()
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", REAL_HTTP_HANDLE)
    transport = _delivery.PublicHTTPTransport(trust_env=False)
    transport._pool._network_backend = Backend()
    with httpx.Client(transport=transport, trust_env=False, follow_redirects=False) as client:
        assert client.post("https://hooks.example.test:8443/hook", json={"event": "test"}).status_code == 204
    assert lookups == [("hooks.example.test", 8443)]
    assert connections == [("93.184.216.34", 8443)]
    assert tls_hosts == ["hooks.example.test"]
    assert b"Host: hooks.example.test:8443" in b"".join(writes)
