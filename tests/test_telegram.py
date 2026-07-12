from __future__ import annotations

import json

import httpx
import pytest
import respx

from feedsentry.domain import Notification
from feedsentry.telegram import TelegramNotifier, render_telegram_message


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
        "<i>Source: example.com</i>\n\n"
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


def test_render_telegram_message_truncates_long_summary_by_utf16_units() -> None:
    notification = make_notification(title="中文通知", summary="中文🙂" * 2000)

    message = render_telegram_message(notification)

    assert message.text.startswith("<i>Source: example.com</i>\n\n<b>中文通知</b>\n")
    assert message.text.endswith("…")
    assert len(message.text.encode("utf-16-le")) // 2 <= 4096


def test_render_telegram_message_does_not_split_html_entities_when_truncating() -> None:
    message = render_telegram_message(make_notification(summary="&" * 2000))

    assert message.text.endswith("…")
    assert message.text.removesuffix("…").endswith(";")


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
async def test_telegram_notifier_raises_sanitized_http_error() -> None:
    respx.post("https://api.telegram.org/botsecret-token/sendMessage").mock(
        return_value=httpx.Response(502)
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await TelegramNotifier(http, "secret-token", "-100123").notify(make_notification())

    assert "secret-token" not in str(error.value)


@respx.mock
async def test_telegram_notifier_rejects_api_response_with_ok_false() -> None:
    respx.post("https://api.telegram.org/botsecret-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "bad request"})
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="Telegram API"):
            await TelegramNotifier(http, "secret-token", "-100123").notify(make_notification())


@pytest.mark.parametrize("result", [{}, {"message_id": "42"}, {"message_id": True}])
@respx.mock
async def test_telegram_notifier_rejects_missing_or_invalid_message_id(result: object) -> None:
    respx.post("https://api.telegram.org/botsecret-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": result})
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="message_id"):
            await TelegramNotifier(http, "secret-token", "-100123").notify(make_notification())
