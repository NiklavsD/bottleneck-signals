"""Mask transport diagnostics only while this package is delivering a message."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import re

_active = ContextVar("notify_log_redaction", default=False)


def mask_url(value: str) -> str:
    """Retain a provider's location while hiding credential-bearing path parts."""
    value = re.sub(
        r"(https://(?:discord\.com|discordapp\.com)/api/webhooks/[0-9]+/)[^\s\"'<>]+",
        r"\1[REDACTED]", value,
    )
    value = re.sub(
        r"(https://hooks\.slack\.com/services/)[^\s\"'<>]+",
        r"\1[REDACTED]", value,
    )
    return re.sub(r"(https://api\.telegram\.org/bot)[^/\s]+", r"\1[REDACTED]", value)


class _TransportFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not _active.get():
            return True
        # httpcore's debug events include arbitrary headers and exception text.
        if record.name.startswith("httpcore"):
            return False
        # Do not format arbitrary URLs: custom webhook paths and query strings
        # can contain credentials too. Preserve the status, never the endpoint.
        if record.msg == 'HTTP Request: %s %s "%s %d %s"' and isinstance(record.args, tuple):
            method, url, version, status, reason = record.args
            masked = mask_url(str(url))
            if masked == str(url):
                masked = "[REDACTED endpoint]"
            record.msg = 'HTTP Request: %s %s "%s %d"'
            record.args = (method, masked, version, status)
            return True
        return False


_filter = _TransportFilter()


@contextmanager
def redact_transport_logs():
    # Ancestor filters do not run on propagated records. Include transport
    # loggers that httpx may import lazily on the first real connection.
    names = [
        "httpx", "httpcore", "httpcore.connection", "httpcore.http11",
        "httpcore.http2", "httpcore.proxy", "httpcore.socks",
        *(name for name in tuple(logging.Logger.manager.loggerDict) if name.startswith("httpcore")),
    ]
    for name in names:
        logging.getLogger(name).addFilter(_filter)
    token = _active.set(True)
    try:
        yield
    finally:
        _active.reset(token)
