from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import httpx
import pytest
import respx

from feedsentry.clients.telegram import TelegramMessage, TelegramNotifier, render_telegram_message
from feedsentry.core.domain import Notification


def make_notification(**changes: str) -> Notification:
    values = {
        "title": "Release available",
        "summary": "Version 1.0 is ready.",
        "source_url": "https://example.com/feed.xml",
        "link": "https://example.com/releases/1.0",
    }
    values.update(changes)
    return Notification(**values)


def test_render_telegram_message_escapes_html_and_adds_read_button() -> None:
    notification = make_notification(
        title='Release <1.0> & "ready"',
        summary='Use <carefully> & keep "quotes".',
    )

    message = render_telegram_message(notification)

    assert message.text == (
        "<i>来源：example.com</i>\n\n"
        "<b>Release &lt;1.0&gt; &amp; &quot;ready&quot;</b>\n"
        "Use &lt;carefully&gt; &amp; keep &quot;quotes&quot;."
    )
    assert "Reason" not in message.text
    assert message.reply_markup == {
        "inline_keyboard": [[{"text": "阅读原文", "url": "https://example.com/releases/1.0"}]]
    }


@pytest.mark.parametrize("link", ["javascript:alert(1)", "mailto:news@example.com", "https:///"])
def test_render_telegram_message_rejects_invalid_article_link(link: str) -> None:
    with pytest.raises(ValueError, match="link"):
        render_telegram_message(make_notification(link=link))


def test_render_telegram_message_rejects_source_without_hostname() -> None:
    with pytest.raises(ValueError, match="source"):
        render_telegram_message(make_notification(source_url="https:///feed.xml"))


def test_render_telegram_message_prefers_feed_title_over_hostname() -> None:
    notification = Notification(
        title="Release available",
        summary="Version 1.0 is ready.",
        source_url="http://host.docker.internal:1200/feed",
        link="https://example.com/releases/1.0",
        source_title="示例 <订阅>",
    )

    message = render_telegram_message(notification)

    assert message.text.startswith("<i>来源：示例 &lt;订阅&gt;</i>\n\n")
    assert "host.docker.internal" not in message.text


def test_render_telegram_message_falls_back_to_hostname_without_feed_title() -> None:
    message = render_telegram_message(make_notification())

    assert message.text.startswith("<i>来源：example.com</i>\n\n")


def test_render_telegram_message_truncates_long_summary_by_utf16_units() -> None:
    notification = make_notification(title="中文通知", summary="中文🙂" * 2000)

    message = render_telegram_message(notification)

    assert message.text.startswith("<i>来源：example.com</i>\n\n<b>中文通知</b>\n")
    assert message.text.endswith("…")
    assert len(message.text.encode("utf-16-le")) // 2 <= 4096


def test_render_telegram_message_does_not_split_html_entities_when_truncating() -> None:
    message = render_telegram_message(make_notification(summary="&" * 2000))

    assert message.text.endswith("…")
    assert message.text.removesuffix("…").endswith(";")


def test_render_telegram_message_keeps_complete_escaped_source_and_title_when_truncated() -> None:
    notification = make_notification(
        source_url="https://source&name.example/feed.xml",
        title='<Title & "quoted">',
        summary="long summary " * 1000,
    )

    message = render_telegram_message(notification)

    assert "<i>来源：source&amp;name.example</i>" in message.text
    assert "<b>&lt;Title &amp; &quot;quoted&quot;&gt;</b>" in message.text
    assert message.text.endswith("…")


def test_telegram_message_is_immutable() -> None:
    message = TelegramMessage(text="message", reply_markup={})

    with pytest.raises(FrozenInstanceError):
        message.text = "changed"


def test_render_telegram_message_rejects_source_and_title_that_exceed_limit() -> None:
    with pytest.raises(ValueError, match="source and title"):
        render_telegram_message(make_notification(title="🙂" * 2050))


@respx.mock
async def test_telegram_notifier_sends_expected_json_and_returns_message_id() -> None:
    route = respx.post("https://api.telegram.org/botsecret-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    )
    notification = make_notification()

    async with httpx.AsyncClient() as http:
        result = await TelegramNotifier(http, "secret-token", "-100123").notify(notification)

    assert result == "telegram_message_id=42"
    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "chat_id": "-100123",
        "text": render_telegram_message(notification).text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": render_telegram_message(notification).reply_markup,
    }


@respx.mock
async def test_telegram_notifier_hides_token_for_http_errors() -> None:
    respx.post("https://api.telegram.org/botsecret-token/sendMessage").mock(
        return_value=httpx.Response(502)
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="Telegram API request failed") as error:
            await TelegramNotifier(http, "secret-token", "-100123").notify(make_notification())

    assert "secret-token" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert not hasattr(error.value, "request")
    assert not hasattr(error.value, "response")


@respx.mock
async def test_telegram_notifier_hides_token_for_rate_limit_errors() -> None:
    respx.post("https://api.telegram.org/botsecret-token/sendMessage").mock(
        return_value=httpx.Response(429)
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="Telegram API request failed") as error:
            await TelegramNotifier(http, "secret-token", "-100123").notify(make_notification())

    assert "secret-token" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert not hasattr(error.value, "request")
    assert not hasattr(error.value, "response")


@respx.mock
async def test_telegram_notifier_hides_token_for_transport_errors() -> None:
    request = httpx.Request("POST", "https://api.telegram.org/botsecret-token/sendMessage")
    respx.post("https://api.telegram.org/botsecret-token/sendMessage").mock(
        side_effect=httpx.ConnectError("connection failed", request=request)
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="Telegram API request failed") as error:
            await TelegramNotifier(http, "secret-token", "-100123").notify(make_notification())

    assert "secret-token" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert not hasattr(error.value, "request")
    assert not hasattr(error.value, "response")


@respx.mock
async def test_telegram_notifier_hides_token_for_timeout_errors() -> None:
    request = httpx.Request("POST", "https://api.telegram.org/botsecret-token/sendMessage")
    respx.post("https://api.telegram.org/botsecret-token/sendMessage").mock(
        side_effect=httpx.ReadTimeout("request timed out", request=request)
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="Telegram API request failed") as error:
            await TelegramNotifier(http, "secret-token", "-100123").notify(make_notification())

    assert "secret-token" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert not hasattr(error.value, "request")
    assert not hasattr(error.value, "response")


@respx.mock
async def test_telegram_notifier_rejects_api_response_with_ok_false() -> None:
    respx.post("https://api.telegram.org/botsecret-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "bad request"})
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="Telegram API"):
            await TelegramNotifier(http, "secret-token", "-100123").notify(make_notification())


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True},
        {"ok": True, "result": "not a dictionary"},
        {"ok": True, "result": {}},
        {"ok": True, "result": {"message_id": "42"}},
        {"ok": True, "result": {"message_id": True}},
    ],
)
@respx.mock
async def test_telegram_notifier_rejects_missing_or_invalid_message_id(payload: object) -> None:
    respx.post("https://api.telegram.org/botsecret-token/sendMessage").mock(
        return_value=httpx.Response(200, json=payload)
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="message_id"):
            await TelegramNotifier(http, "secret-token", "-100123").notify(make_notification())
