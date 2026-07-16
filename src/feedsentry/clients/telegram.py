from __future__ import annotations

from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit

import httpx

from feedsentry.core.domain import Notification

MAX_MESSAGE_UTF16_UNITS = 4096
ELLIPSIS = "…"


@dataclass(frozen=True)
class TelegramMessage:
    text: str
    reply_markup: dict[str, object]


def render_telegram_message(notification: Notification) -> TelegramMessage:
    source = urlsplit(notification.source_url)
    if not source.hostname:
        raise ValueError("source URL must include a hostname")

    article = urlsplit(notification.link)
    if article.scheme not in {"http", "https"} or not article.hostname:
        raise ValueError("link must be an HTTP(S) URL with a hostname")

    source_label = escape(source.hostname)
    title = escape(notification.title)
    summary = escape(notification.summary)
    prefix = f"<i>来源：{source_label}</i>\n\n<b>{title}</b>\n"

    if _utf16_units(prefix) > MAX_MESSAGE_UTF16_UNITS:
        raise ValueError("source and title exceed Telegram's message length limit")

    text = prefix + summary
    if _utf16_units(text) > MAX_MESSAGE_UTF16_UNITS:
        available_summary_units = MAX_MESSAGE_UTF16_UNITS - _utf16_units(prefix)
        if available_summary_units <= _utf16_units(ELLIPSIS):
            raise ValueError("source and title leave no room for a truncated summary")
        summary = _truncate_escaped_summary(
            notification.summary,
            available_summary_units - _utf16_units(ELLIPSIS),
        )
        text = prefix + summary + ELLIPSIS

    return TelegramMessage(
        text=text,
        reply_markup={"inline_keyboard": [[{"text": "阅读原文", "url": notification.link}]]},
    )


class TelegramNotifier:
    def __init__(self, http: httpx.AsyncClient, bot_token: str, chat_id: str) -> None:
        self.http = http
        self.bot_token = bot_token
        self.chat_id = chat_id

    async def notify(self, notification: Notification) -> str:
        message = render_telegram_message(notification)
        try:
            response = await self.http.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message.text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": message.reply_markup,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError:
            request_failed = True
        else:
            request_failed = False

        if request_failed:
            raise ValueError("Telegram API request failed")

        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ValueError("Telegram API response did not report success")

        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise ValueError("Telegram API response is missing a valid message_id")

        return f"telegram_message_id={message_id}"


def _utf16_units(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _truncate_escaped_summary(value: str, limit: int) -> str:
    units = 0
    parts: list[str] = []
    for character in value:
        escaped_character = escape(character)
        character_units = _utf16_units(escaped_character)
        if units + character_units > limit:
            break
        parts.append(escaped_character)
        units += character_units
    return "".join(parts)
