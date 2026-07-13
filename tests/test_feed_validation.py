from __future__ import annotations

import httpx
import pytest
import respx

from feedsentry.feed_validation import FeedValidationError, FeedValidator

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example</title>
<item><guid>one</guid><title>One</title><link>https://example.com/one</link></item>
</channel></rss>"""
ATOM_EMPTY = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Empty</title><id>empty</id></feed>"""


@respx.mock
async def test_validator_accepts_rss_and_returns_metadata() -> None:
    respx.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(200, content=RSS, headers={"etag": '"one"'})
    )
    async with httpx.AsyncClient() as http:
        result = await FeedValidator(http).validate("https://example.com/feed.xml")

    assert result.canonical_url == "https://example.com/feed.xml"
    assert result.title == "Example"
    assert result.version == "rss20"
    assert result.etag == '"one"'
    assert len(result.entries) == 1


@respx.mock
async def test_validator_accepts_empty_atom_feed() -> None:
    respx.get("https://example.com/empty.xml").mock(
        return_value=httpx.Response(200, content=ATOM_EMPTY)
    )
    async with httpx.AsyncClient() as http:
        result = await FeedValidator(http).validate("https://example.com/empty.xml")
    assert result.version == "atom10"
    assert result.entries == ()


@pytest.mark.parametrize("content", [b"<html>not a feed</html>", b"<rss><broken>"])
@respx.mock
async def test_validator_rejects_non_feed_content(content: bytes) -> None:
    respx.get("https://example.com/not-feed").mock(
        return_value=httpx.Response(200, content=content)
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(FeedValidationError, match="valid RSS or Atom"):
            await FeedValidator(http).validate("https://example.com/not-feed")


@respx.mock
async def test_validator_rejects_oversized_feed() -> None:
    respx.get("https://example.com/large.xml").mock(
        return_value=httpx.Response(200, content=RSS + b" " * 100)
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(FeedValidationError, match="too large"):
            await FeedValidator(http, max_bytes=len(RSS)).validate("https://example.com/large.xml")


async def test_validator_rejects_loopback_url_without_request() -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(FeedValidationError, match="not allowed"):
            await FeedValidator(http).validate("http://127.0.0.1/feed")


@respx.mock
async def test_validator_allows_configured_rsshub_host() -> None:
    respx.get("http://rsshub.internal:1200/route").mock(
        return_value=httpx.Response(200, content=ATOM_EMPTY)
    )
    async with httpx.AsyncClient() as http:
        result = await FeedValidator(http, allowed_private_hosts={"rsshub.internal"}).validate(
            "http://rsshub.internal:1200/route"
        )
    assert result.version == "atom10"
