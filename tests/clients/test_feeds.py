import httpx
import respx

from feedsentry.clients.feeds import FeedClient, normalize_feed

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><guid>one</guid><title>Release 1</title><link>https://example.com/one</link>
<description>Details</description></item></channel></rss>"""


def test_normalize_feed_uses_guid_and_stable_hash() -> None:
    first = normalize_feed(RSS, "https://example.com/feed.xml")[0]
    second = normalize_feed(RSS, "https://example.com/feed.xml")[0]

    assert first.external_id == "one"
    assert first.content_hash == second.content_hash


@respx.mock
async def test_fetch_sends_conditional_headers() -> None:
    route = respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(304))

    async with httpx.AsyncClient() as http:
        result = await FeedClient(http).fetch(
            "https://example.com/feed.xml", etag='"abc"', last_modified="yesterday"
        )

    assert result.not_modified is True
    assert route.calls[0].request.headers["if-none-match"] == '"abc"'
    assert route.calls[0].request.headers["if-modified-since"] == "yesterday"
