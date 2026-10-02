"""SSRF guard for user-supplied delivery URLs. DNS is stubbed; nothing touches the network."""

import httpcore
import httpx
import pytest

from notify import Message, send, validate_target
from notify import _netguard
from notify._netguard import _PinnedBackend, is_public_ip, public_hostname, resolve_public, UnsafeDestination

from conftest import ORIGINAL_HANDLE_REQUEST, PUBLIC_IP, fake_dns

MSG = Message("Title", "Body")
HOOK = {"url": "https://hooks.example.com/in"}


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.8.9.10", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254", "100.64.0.1",
    "100.127.255.254", "0.0.0.0", "0.1.2.3", "224.0.0.1", "240.0.0.1", "255.255.255.255", "198.18.0.1", "192.0.0.1",
    "::", "::1", "fe80::1", "fe80::1%eth0", "fc00::1", "fd12:3456::1", "fec0::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::ffff:100.64.0.1", "::127.0.0.1",
    "64:ff9b::a00:1", "2002:a00:1::", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "not-an-ip",
])
def test_non_public_addresses_rejected(ip):
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", [PUBLIC_IP, "1.1.1.1", "8.8.8.8", "2606:4700::1111", "::ffff:93.184.216.34"])
def test_public_addresses_allowed(ip):
    assert is_public_ip(ip)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/hook", "https://10.0.0.1/hook", "https://169.254.169.254/latest", "https://[::1]/hook",
    "https://[::ffff:127.0.0.1]/hook", "https://[fe80::1]/hook", "https://93.184.216.34/hook",  # any IP literal
    "https://2130706433/hook", "https://0x7f000001/hook", "https://127.1/hook", "https://0177.0.0.1/hook",
    "https://localhost/hook", "https://api.localhost/hook", "https://printer.local/hook", "https://db.internal/hook",
    "https://intranet/hook",
])
def test_webhook_validation_rejects_ip_literals_and_local_names(url):
    ok, error = validate_target("webhook", {"url": url})
    assert not ok and error


def test_ntfy_custom_server_needs_public_hostname():
    assert not validate_target("ntfy", {"topic": "valid_topic", "server": "https://10.1.2.3"})[0]
    assert validate_target("ntfy", {"topic": "valid_topic", "server": "https://ntfy.example.com"})[0]


def test_public_hostname_allowed():
    assert public_hostname("hooks.example.com") and public_hostname("discord.com.")


@pytest.mark.parametrize("answers", [
    ["10.0.0.5"], ["127.0.0.1"], ["100.64.1.1"], ["169.254.169.254"], ["::1"], ["fe80::1"], ["fd00::1"],
    ["::ffff:192.168.1.1"], ["224.0.0.1"], ["0.0.0.0"],
    [PUBLIC_IP, "10.0.0.1"],  # ANY non-public answer rejects the host
    [],  # NXDOMAIN
])
@pytest.mark.parametrize("channel,target", [
    ("webhook", HOOK),
    ("discord", {"webhook_url": "https://discord.com/api/webhooks/123/tok"}),
    ("slack", {"webhook_url": "https://hooks.slack.com/services/T/B/tok"}),
    ("ntfy", {"topic": "leadtime_alerts"}),
])
def test_send_refuses_hosts_resolving_to_non_public(answers, channel, target, monkeypatch, http_mock):
    requests, _ = http_mock(lambda request: httpx.Response(200, json={}))
    monkeypatch.setattr(_netguard, "_getaddrinfo", fake_dns(lambda host: answers))
    result = send(channel, target, MSG)
    assert result == type(result)(False, "Delivery failed.")
    assert requests == []


def test_send_allows_public_host(http_mock):
    requests, _ = http_mock(lambda request: httpx.Response(204))
    assert send("webhook", HOOK, MSG).ok
    assert len(requests) == 1 and requests[0].url.host == "hooks.example.com"


def test_http_failure_is_generic_with_status(http_mock):
    http_mock(lambda request: httpx.Response(403, text="internal detail"))
    result = send("webhook", HOOK, MSG)
    assert result.error == "Delivery failed (HTTP 403)." and result.http_status == 403


def test_connection_is_pinned_to_vetted_ip(monkeypatch):
    """DNS rebinding: the first lookup says public, later ones say internal. The socket must go to the
    first (vetted) address and DNS must be consulted exactly once."""
    answers = iter([[PUBLIC_IP], ["127.0.0.1"], ["127.0.0.1"]])
    dns = fake_dns(lambda host: next(answers))
    monkeypatch.setattr(_netguard, "_getaddrinfo", dns)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", ORIGINAL_HANDLE_REQUEST)
    connects = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        connects.append((host, port))
        raise httpcore.ConnectError("stop before any real I/O")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect_tcp)
    result = send("webhook", {"url": "https://hooks.example.com:8443/in"}, MSG)
    assert not result.ok and result.error == "Delivery failed."
    assert connects == [(PUBLIC_IP, 8443)]
    assert dns.calls == ["hooks.example.com"]


def test_pinned_backend_refuses_other_hosts():
    backend = _PinnedBackend("hooks.example.com", PUBLIC_IP)
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("evil.example.com", 443)
    with pytest.raises(httpcore.ConnectError):
        backend.connect_unix_socket("/var/run/docker.sock")


def test_resolve_public_rejects_literals_without_dns(monkeypatch):
    monkeypatch.setattr(_netguard, "_getaddrinfo", lambda *a, **k: pytest.fail("DNS must not be consulted"))
    for host in ("127.0.0.1", "::1", "93.184.216.34", "localhost"):
        with pytest.raises(UnsafeDestination):
            resolve_public(host, 443)
