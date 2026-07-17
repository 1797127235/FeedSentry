from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from feedsentry.core.domain import Notification


@dataclass(frozen=True)
class QQMessage:
    segments: list[dict[str, object]]
    action: str
    id_field: str
    target_id: str


def render_qq_message(notification: Notification, target_type: str, target_id: str) -> QQMessage:
    if target_type not in {"private", "group"}:
        raise ValueError(f"unsupported qq target_type: {target_type}")

    source = urlsplit(notification.source_url)
    if not source.hostname:
        raise ValueError("source URL must include a hostname")

    article = urlsplit(notification.link)
    if article.scheme not in {"http", "https"} or not article.hostname:
        raise ValueError("link must be an HTTP(S) URL with a hostname")

    source_label = notification.source_title or source.hostname
    parts = [
        f"来源：{source_label}",
        notification.title,
        notification.summary,
        notification.link,
    ]
    text = "\n\n".join(part for part in parts if part)
    segments = [{"type": "text", "data": {"text": text}}]

    if target_type == "private":
        action = "send_private_msg"
        id_field = "user_id"
    else:
        action = "send_group_msg"
        id_field = "group_id"

    return QQMessage(
        segments=segments,
        action=action,
        id_field=id_field,
        target_id=target_id,
    )


class QQNotifier:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        access_token: str | None,
        target_type: str,
        target_id: str,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.target_type = target_type
        self.target_id = target_id
        self.destination_key = f"qq:{target_type}:{target_id}"

    async def notify(self, notification: Notification) -> str:
        message = render_qq_message(notification, self.target_type, self.target_id)
        headers: dict[str, str] = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            response = await self.http.post(
                f"{self.base_url}/{message.action}",
                json={message.id_field: message.target_id, "message": message.segments},
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            request_failed = True
        else:
            request_failed = False

        if request_failed:
            raise ValueError("QQ/OneBot API request failed")

        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise ValueError("QQ/OneBot API response did not report success")

        data = payload.get("data")
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise ValueError("QQ/OneBot API response is missing a valid message_id")

        return f"qq_message_id={message_id}"
