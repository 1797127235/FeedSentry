from __future__ import annotations

import json

import httpx
import pytest
import respx

from feedsentry.domain import Notification
from feedsentry.qq import QQNotifier, render_qq_message


def make_notification(**changes: str) -> Notification:
    values = {
        "title": "Release available",
        "summary": "Version 1.0 is ready.",
        "source_url": "https://example.com/feed.xml",
        "link": "https://example.com/releases/1.0",
    }
    values.update(changes)
    return Notification(**values)


def test_render_qq_message_for_private_target() -> None:
    message = render_qq_message(make_notification(), "private", "10001234")

    assert message.action == "send_private_msg"
    assert message.id_field == "user_id"
    assert message.target_id == "10001234"
    assert message.segments == [
        {
            "type": "text",
            "data": {
                "text": (
                    "来源：example.com\n\n"
                    "Release available\n\n"
                    "Version 1.0 is ready.\n\n"
                    "https://example.com/releases/1.0"
                )
            },
        }
    ]


def test_render_qq_message_for_group_target() -> None:
    message = render_qq_message(make_notification(), "group", "987654321")

    assert message.action == "send_group_msg"
    assert message.id_field == "group_id"
    assert message.target_id == "987654321"


@pytest.mark.parametrize("link", ["javascript:alert(1)", "mailto:news@example.com", "https:///"])
def test_render_qq_message_rejects_invalid_article_link(link: str) -> None:
    with pytest.raises(ValueError, match="link"):
        render_qq_message(make_notification(link=link), "private", "1")


def test_render_qq_message_rejects_source_without_hostname() -> None:
    with pytest.raises(ValueError, match="source"):
        render_qq_message(make_notification(source_url="https:///feed.xml"), "private", "1")


def test_qq_notifier_destination_key_distinguishes_target_types() -> None:
    private = QQNotifier(httpx.AsyncClient(), "http://napcat:3000", None, "private", "100")
    group = QQNotifier(httpx.AsyncClient(), "http://napcat:3000", None, "group", "200")

    assert private.destination_key == "qq:private:100"
    assert group.destination_key == "qq:group:200"


@respx.mock
async def test_qq_notifier_sends_private_msg_with_bearer_token_and_returns_message_id() -> None:
    route = respx.post("http://napcat:3000/send_private_msg").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 42}}
        )
    )

    async with httpx.AsyncClient() as http:
        result = await QQNotifier(
            http, "http://napcat:3000/", "secret-token", "private", "10001234"
        ).notify(make_notification())

    assert result == "qq_message_id=42"
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer secret-token"
    payload = json.loads(request.content)
    assert payload == {
        "user_id": "10001234",
        "message": render_qq_message(make_notification(), "private", "10001234").segments,
    }


@respx.mock
async def test_qq_notifier_sends_group_msg_without_authorization_when_no_token() -> None:
    route = respx.post("http://napcat:3000/send_group_msg").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 7}}
        )
    )

    async with httpx.AsyncClient() as http:
        result = await QQNotifier(http, "http://napcat:3000", None, "group", "987").notify(
            make_notification()
        )

    assert result == "qq_message_id=7"
    assert "authorization" not in route.calls[0].request.headers
    payload = json.loads(route.calls[0].request.content)
    assert payload["group_id"] == "987"


@respx.mock
async def test_qq_notifier_hides_token_for_http_errors() -> None:
    respx.post("http://napcat:3000/send_group_msg").mock(return_value=httpx.Response(502))

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="QQ/OneBot API request failed") as error:
            await QQNotifier(http, "http://napcat:3000", "secret-token", "group", "1").notify(
                make_notification()
            )

    assert "secret-token" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert not hasattr(error.value, "request")
    assert not hasattr(error.value, "response")


@respx.mock
async def test_qq_notifier_hides_token_for_transport_errors() -> None:
    request = httpx.Request("POST", "http://napcat:3000/send_group_msg")
    respx.post("http://napcat:3000/send_group_msg").mock(
        side_effect=httpx.ConnectError("connection failed", request=request)
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="QQ/OneBot API request failed") as error:
            await QQNotifier(http, "http://napcat:3000", "secret-token", "group", "1").notify(
                make_notification()
            )

    assert "secret-token" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@respx.mock
async def test_qq_notifier_rejects_failed_status() -> None:
    respx.post("http://napcat:3000/send_private_msg").mock(
        return_value=httpx.Response(
            200, json={"status": "failed", "retcode": 10003, "msg": "user not found"}
        )
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="did not report success"):
            await QQNotifier(http, "http://napcat:3000", None, "private", "1").notify(
                make_notification()
            )


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok", "retcode": 0},
        {"status": "ok", "retcode": 0, "data": "not a dict"},
        {"status": "ok", "retcode": 0, "data": {}},
        {"status": "ok", "retcode": 0, "data": {"message_id": "42"}},
        {"status": "ok", "retcode": 0, "data": {"message_id": True}},
    ],
)
@respx.mock
async def test_qq_notifier_rejects_missing_or_invalid_message_id(payload: object) -> None:
    respx.post("http://napcat:3000/send_private_msg").mock(
        return_value=httpx.Response(200, json=payload)
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="message_id"):
            await QQNotifier(http, "http://napcat:3000", None, "private", "1").notify(
                make_notification()
            )
