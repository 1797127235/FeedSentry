import httpx
import pytest
import respx

from feedsentry.clients.feeds import FeedClient, normalize_feed

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><guid>one</guid><title>Release 1</title><link>https://example.com/one</link>
<description>Details</description></item></channel></rss>"""


async def public_resolver(hostname: str, port: int) -> set[str]:
    del hostname, port
    return {"93.184.216.34"}


def test_normalize_feed_uses_guid_and_stable_hash() -> None:
    first = normalize_feed(RSS, "https://example.com/feed.xml")[0]
    second = normalize_feed(RSS, "https://example.com/feed.xml")[0]

    assert first.external_id == "one"
    assert first.content_hash == second.content_hash


def test_normalize_feed_drops_non_http_entry_links() -> None:
    rss = RSS.replace(b"https://example.com/one", b"javascript:alert(1)")

    assert normalize_feed(rss, "https://example.com/feed.xml")[0].link == ""


def test_normalize_feed_resolves_relative_entry_links() -> None:
    rss = RSS.replace(b"https://example.com/one", b"/one")

    assert normalize_feed(rss, "https://example.com/feed.xml")[0].link == (
        "https://example.com/one"
    )


@respx.mock
async def test_fetch_sends_conditional_headers() -> None:
    route = respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(304))

    async with httpx.AsyncClient() as http:
        result = await FeedClient(http, resolver=public_resolver).fetch(
            "https://example.com/feed.xml", etag='"abc"', last_modified="yesterday"
        )

    assert result.not_modified is True
    assert route.calls[0].request.headers["if-none-match"] == '"abc"'
    assert route.calls[0].request.headers["if-modified-since"] == "yesterday"


@respx.mock
async def test_fetch_returns_feed_title() -> None:
    respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, content=RSS))

    async with httpx.AsyncClient() as http:
        result = await FeedClient(http, resolver=public_resolver).fetch(
            "https://example.com/feed.xml"
        )

    assert result.not_modified is False
    assert result.title == "T"


async def test_fetch_rejects_loopback_url_without_request() -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPError, match="not allowed"):
            await FeedClient(http).fetch("http://127.0.0.1/feed")


@respx.mock
async def test_fetch_rejects_redirect_to_loopback_before_following() -> None:
    first = respx.get("https://example.com/redirect").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/feed"})
    )
    private = respx.get("http://127.0.0.1/feed").mock(return_value=httpx.Response(200, content=RSS))
    async with httpx.AsyncClient(follow_redirects=True) as http:
        with pytest.raises(httpx.HTTPError, match="not allowed"):
            await FeedClient(http, resolver=public_resolver).fetch("https://example.com/redirect")

    assert first.called
    assert not private.called


@respx.mock
async def test_fetch_resolves_relative_links_against_redirect_target() -> None:
    redirected_rss = RSS.replace(b"https://example.com/one", b"article")
    respx.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://feeds.example.net/rss/current.xml"}
        )
    )
    respx.get("https://feeds.example.net/rss/current.xml").mock(
        return_value=httpx.Response(200, content=redirected_rss)
    )

    async with httpx.AsyncClient() as http:
        result = await FeedClient(http, resolver=public_resolver).fetch(
            "https://example.com/feed.xml"
        )

    assert result.entries[0].source_url == "https://example.com/feed.xml"
    assert result.entries[0].link == "https://feeds.example.net/rss/article"


@respx.mock
async def test_fetch_rejects_oversized_response() -> None:
    respx.get("https://example.com/large.xml").mock(
        return_value=httpx.Response(200, content=RSS + b" ")
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPError, match="too large"):
            await FeedClient(http, max_bytes=len(RSS), resolver=public_resolver).fetch(
                "https://example.com/large.xml"
            )
