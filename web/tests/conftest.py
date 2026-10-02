import socket

import httpx
import pytest

from notify import _delivery, _netguard

ORIGINAL_HANDLE_REQUEST = httpx.HTTPTransport.handle_request
PUBLIC_IP = "93.184.216.34"


def fake_dns(answers):
    """Stub resolver: answers maps hostname -> list of IPs (default: one public IPv4)."""
    calls = []

    def getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        ips = answers.get(host, [PUBLIC_IP]) if isinstance(answers, dict) else answers(host)
        if not ips:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in ips]

    getaddrinfo.calls = calls
    return getaddrinfo


ENV = (
    "TELEGRAM_BOT_TOKEN", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
    "SMTP_FROM", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    # A forgotten network mock must fail instead of contacting a real service.
    def blocked(*args, **kwargs):
        raise AssertionError("Unmocked network access")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(_delivery.smtplib, "SMTP", blocked)
    # No real DNS in tests: every hostname resolves to one public address unless a test says otherwise.
    monkeypatch.setattr(_netguard, "_getaddrinfo", fake_dns({}))


@pytest.fixture
def credentials(monkeypatch):
    values = {
        "TELEGRAM_BOT_TOKEN": "123:telegram-secret",
        "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
        "SMTP_USER": "smtp-user", "SMTP_PASS": "smtp-secret", "SMTP_FROM": "alerts@example.com",
        "TWILIO_ACCOUNT_SID": "ACexample", "TWILIO_AUTH_TOKEN": "twilio-secret", "TWILIO_FROM": "+14155550100",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.fixture
def http_mock(monkeypatch):
    original_client = httpx.Client

    def install(handler):
        requests = []
        options = []

        def handle(request):
            requests.append(request)
            return handler(request)

        def client(**kwargs):
            # The real code passes an SSRF-pinned transport; swap it for the mock.
            assert isinstance(kwargs.pop("transport"), _netguard.PinnedTransport)
            options.append(kwargs)
            return original_client(transport=httpx.MockTransport(handle), **kwargs)

        monkeypatch.setattr(_delivery.httpx, "Client", client)
        return requests, options

    return install
