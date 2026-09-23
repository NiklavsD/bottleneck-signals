"""Validation errors deliberately contain no supplied values."""

import re
from urllib.parse import urlsplit

CHANNELS = ("discord", "slack", "webhook", "ntfy", "telegram", "email", "sms")
DISCORD = r"https://(?:discord\.com|discordapp\.com)/api/webhooks/[0-9]+/[A-Za-z0-9_-]+"
SLACK = r"https://hooks\.slack\.com/services/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+"
TOPIC = r"[A-Za-z0-9_-]{6,64}"
PHONE = r"\+[1-9][0-9]{1,14}"
CHAT = r"(?:-?[1-9][0-9]*|@[A-Za-z][A-Za-z0-9_]{4,31})"
ADDRESS = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"


def matches(pattern: str, value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def valid_url(value: object, *, https_only: bool = True) -> bool:
    if not isinstance(value, str) or not value or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\"
        for char in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme in (("https",) if https_only else ("http", "https"))
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        return False


def validate_target(channel: str, target: dict) -> tuple[bool, str | None]:
    """Validate saved input independently of provider credentials."""
    if not isinstance(channel, str) or channel not in CHANNELS:
        return False, "Unknown channel."
    if not isinstance(target, dict):
        return False, "Target must be a dictionary."
    if channel in ("discord", "slack"):
        pattern = DISCORD if channel == "discord" else SLACK
        if not matches(pattern, target.get("webhook_url")):
            return False, f"Invalid {channel} webhook URL."
    elif channel == "webhook":
        if not valid_url(target.get("url")) or urlsplit(target["url"]).fragment:
            return False, "Webhook URL must be HTTPS without credentials or a fragment."
        if "secret" in target and not isinstance(target["secret"], str):
            return False, "Webhook secret must be a string."
    elif channel == "ntfy":
        if not matches(TOPIC, target.get("topic")):
            return False, "Topic must contain 6–64 ASCII letters, digits, underscores or hyphens."
        server = target.get("server", "https://ntfy.sh")
        if not valid_url(server) or urlsplit(server).query or urlsplit(server).fragment:
            return False, "ntfy server must be HTTPS without credentials, query or fragment."
    elif channel == "telegram":
        chat = target.get("chat_id")
        if isinstance(chat, bool) or not isinstance(chat, (str, int)) or not matches(CHAT, str(chat)):
            return False, "chat_id must be a nonzero integer or an @channel username."
    elif channel == "email":
        address = target.get("address")
        if not matches(ADDRESS, address) or len(address) > 254:
            return False, "Invalid email address."
        local = address.split("@", 1)[0]
        if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
            return False, "Invalid email address."
    elif channel == "sms" and not matches(PHONE, target.get("phone")):
        return False, "Phone must be in E.164 format."
    return True, None
