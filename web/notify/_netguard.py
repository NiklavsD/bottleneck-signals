"""SSRF guard: outbound requests only reach public addresses, pinned to the vetted IP.

A hostname is resolved once, every resolved address must be public, and the
connection is then made to that exact address (TLS still verifies the original
hostname via SNI). A second DNS answer can therefore never redirect the socket
to an internal address (DNS rebinding).
"""

import ipaddress
import socket

import httpcore
import httpx

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# IPv6 ranges that embed an IPv4 address (NAT64, 6to4, Teredo): a public-looking
# v6 address could tunnel to an internal v4 one, so these are refused outright.
_EMBEDS_V4 = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32"))
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp", ".localdomain")

# Indirection so tests can stub DNS without touching the socket module globally.
_getaddrinfo = socket.getaddrinfo


class UnsafeDestination(Exception):
    """The destination is not a public internet address."""


def is_public_ip(value: object) -> bool:
    try:
        ip = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _EMBEDS_V4):
            return False
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or (ip.version == 4 and ip in _CGNAT) or (ip.version == 6 and ip.is_site_local)):
        return False
    return ip.is_global


def is_ip_literal(host: str) -> bool:
    h = host.strip("[]").split("%", 1)[0]
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(h)  # legacy spellings such as 127.1, 0x7f000001, 2130706433
        return True
    except OSError:
        return False


def public_hostname(host: object) -> bool:
    """Syntactic check used at validation time: a DNS name, not an IP literal or a local name."""
    if not isinstance(host, str):
        return False
    h = host.lower().rstrip(".")
    return bool(h) and "." in h and not is_ip_literal(h) and h != "localhost" and not h.endswith(_LOCAL_SUFFIXES)


def resolve_public(host: str, port: int) -> str:
    """Resolve host and return one vetted address; raise if ANY address is non-public."""
    if not public_hostname(host):
        raise UnsafeDestination
    try:
        infos = _getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeDestination from exc
    addrs = [info[4][0] for info in infos]
    if not addrs or not all(is_public_ip(a) for a in addrs):
        raise UnsafeDestination
    return addrs[0]


class _PinnedBackend(httpcore.NetworkBackend):
    """Connects only to the vetted address, and only for the host it was vetted for."""

    def __init__(self, host: str, ip: str):
        self._host, self._ip = host, ip
        self._inner = httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self._host:
            raise httpcore.ConnectError("Unexpected host.")
        return self._inner.connect_tcp(self._ip, port, timeout=timeout, local_address=local_address,
                                       socket_options=socket_options)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix sockets are not allowed.")

    def sleep(self, seconds):
        self._inner.sleep(seconds)


class PinnedTransport(httpx.HTTPTransport):
    def __init__(self, host: str, ip: str):
        super().__init__()
        # httpx does not expose network_backend; the pool reads it on every new connection.
        self._pool._network_backend = _PinnedBackend(host, ip)
        self.host, self.ip = host, ip


def pinned_transport(url: str) -> PinnedTransport:
    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeDestination
    host = parsed.raw_host.decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return PinnedTransport(host, resolve_public(host, port))
