import json

import httpx
import pytest
import respx

from feedsentry.apprise import AppriseClient
from feedsentry.firecrawl import FirecrawlClient


@respx.mock
async def test_firecrawl_omits_auth_when_key_is_empty() -> None:
    route = respx.post("http://firecrawl:3002/v1/scrape").mock(
        return_value=httpx.Response(200, json={"data": {"markdown": "Body"}})
    )

    async with httpx.AsyncClient() as http:
        body = await FirecrawlClient(http, "http://firecrawl:3002/", "").scrape(
            "https://example.com/post"
        )

    assert body == "Body"
    assert "authorization" not in route.calls[0].request.headers
    assert json.loads(route.calls[0].request.content) == {
        "url": "https://example.com/post",
        "formats": ["markdown"],
        "onlyMainContent": True,
    }


@respx.mock
async def test_firecrawl_uses_bearer_auth_when_configured() -> None:
    route = respx.post("http://firecrawl:3002/v1/scrape").mock(
        return_value=httpx.Response(200, json={"data": {"markdown": "Body"}})
    )

    async with httpx.AsyncClient() as http:
        await FirecrawlClient(http, "http://firecrawl:3002", "token").scrape(
            "https://example.com/post"
        )

    assert route.calls[0].request.headers["authorization"] == "Bearer token"


@respx.mock
async def test_firecrawl_rejects_missing_or_blank_markdown() -> None:
    respx.post("http://firecrawl:3002/v1/scrape").mock(
        return_value=httpx.Response(200, json={"data": {"markdown": "  "}})
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="markdown"):
            await FirecrawlClient(http, "http://firecrawl:3002", None).scrape(
                "https://example.com/post"
            )


@respx.mock
async def test_apprise_sends_expected_json_message_and_returns_bounded_text() -> None:
    route = respx.post("http://apprise:8000/notify/telegram").mock(
        return_value=httpx.Response(200, text="ok")
    )

    async with httpx.AsyncClient() as http:
        result = await AppriseClient(http, "http://apprise:8000/").notify(
            "telegram", "Release V2", "Summary\n\nWhy: relevant\n\nhttps://example.com/v2"
        )

    assert result == "ok"
    assert json.loads(route.calls[0].request.content) == {
        "title": "Release V2",
        "body": "Summary\n\nWhy: relevant\n\nhttps://example.com/v2",
        "type": "info",
    }
