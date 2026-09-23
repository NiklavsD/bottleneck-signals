import pytest

from notify import make_start_link, parse_update_for_code
from notify import telegram_link_code_flow


def test_start_link():
    assert make_start_link("leadtime_bot", "Abc_123-x") == "https://t.me/leadtime_bot?start=Abc_123-x"
    assert make_start_link("@leadtime_bot", "code") == "https://t.me/leadtime_bot?start=code"
    assert telegram_link_code_flow.make_start_link is make_start_link


@pytest.mark.parametrize("bot,code", [("bad/bot", "code"), (None, "code"), ("leadtime_bot", ""), ("leadtime_bot", "a" * 65), ("leadtime_bot", "x&evil=1"), ("leadtime_bot", "two words"), ("leadtime_bot", None)])
def test_invalid_start_links(bot, code):
    with pytest.raises(ValueError):
        make_start_link(bot, code)


@pytest.mark.parametrize("text", ["/start Abc_123-x", "/start@leadtime_bot Abc_123-x", "/start Abc_123-x\n"])
def test_parse_start(text):
    assert parse_update_for_code({"message": {"text": text, "chat": {"id": -123}}}) == ("Abc_123-x", -123)


@pytest.mark.parametrize("update", [None, [], {}, {"message": None}, {"message": []}, {"message": {"text": "/start code"}}, {"message": {"text": "/start code", "chat": None}}, {"message": {"text": "/start code", "chat": {"id": True}}}, {"message": {"text": "/start code", "chat": {"id": 0}}}, {"edited_message": {"text": "/start code", "chat": {"id": 123}}}])
def test_malformed_update(update):
    assert parse_update_for_code(update) is None


@pytest.mark.parametrize("text", [None, 1, "/start", "/start ", "hello /start code", "/start code extra", "/restart code", "/start code&bad", "/start " + "a" * 65])
def test_other_messages(text):
    assert parse_update_for_code({"message": {"text": text, "chat": {"id": 123}}}) is None
