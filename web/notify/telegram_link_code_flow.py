"""Pure helpers; the caller owns code generation, expiry and one-time use."""

import re

_BOT = r"[A-Za-z][A-Za-z0-9_]{4,31}"
_CODE = r"[A-Za-z0-9_-]{1,64}"


def make_start_link(bot_username: str, code: str) -> str:
    username = bot_username.removeprefix("@") if isinstance(bot_username, str) else ""
    if not re.fullmatch(_BOT, username):
        raise ValueError("Invalid bot username.")
    if not isinstance(code, str) or not re.fullmatch(_CODE, code):
        raise ValueError("Code must contain 1–64 base64url characters.")
    return f"https://t.me/{username}?start={code}"


def parse_update_for_code(update_json: dict) -> tuple[str, int] | None:
    if not isinstance(update_json, dict):
        return None
    message = update_json.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        return None
    match = re.fullmatch(rf"/start(?:@{_BOT})?\s+({_CODE})\s*", message["text"])
    chat = message.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if not match or type(chat_id) is not int or chat_id == 0:
        return None
    return match[1], chat_id
