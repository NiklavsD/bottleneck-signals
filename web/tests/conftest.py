import httpx
import pytest

from notify import _delivery


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
            options.append(kwargs)
            return original_client(transport=httpx.MockTransport(handle), **kwargs)

        monkeypatch.setattr(_delivery.httpx, "Client", client)
        return requests, options

    return install
