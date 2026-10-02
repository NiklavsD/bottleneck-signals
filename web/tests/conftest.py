import httpx
import pytest
import socket

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
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


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

        def handle(transport, request):
            # Exercise the production public transport first. Only the final
            # socket boundary is mocked; payload assertions retain the Host URL.
            logical = httpx.Request(
                request.method, request.url.copy_with(host=request.extensions["sni_hostname"]),
                headers=request.headers, content=request.read(), extensions=request.extensions,
            )
            requests.append(logical)
            return handler(logical)

        def client(**kwargs):
            assert isinstance(kwargs["transport"], _delivery.PublicHTTPTransport)
            options.append({key: value for key, value in kwargs.items() if key != "transport"})
            return original_client(**kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
        ])
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
        monkeypatch.setattr(_delivery.httpx, "Client", client)
        return requests, options

    return install
