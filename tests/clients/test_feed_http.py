from __future__ import annotations

import httpx
import pytest
import respx

from feedsentry.clients.feed_http import FeedTransferError, SafeFeedHTTP


async def _return_public(hostname: str, port: int) -> set[str]:
    del hostname, port
    return {"93.184.216.34"}


async def _return_private(hostname: str, port: int) -> set[str]:
    del hostname, port
    return {"10.0.0.1"}


async def _return_empty(hostname: str, port: int) -> set[str]:
    del hostname, port
    return set()


async def test_origin_rejects_non_http_scheme() -> None:
    with pytest.raises(ValueError, match="HTTP"):
        SafeFeedHTTP(httpx.AsyncClient(), allowed_private_origins={"ftp://example.com"})


async def test_check_url_rejects_non_http_scheme() -> None:
    http = httpx.AsyncClient()
    transport = SafeFeedHTTP(http, resolver=_return_public)
    with pytest.raises(FeedTransferError, match="not allowed"):
        await transport.check_url("ftp://example.com/feed")


async def test_check_url_rejects_missing_hostname() -> None:
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_public)
    with pytest.raises(FeedTransferError, match="not allowed"):
        await transport.check_url("https:///feed")


async def test_check_url_rejects_invalid_port() -> None:
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_public)
    with pytest.raises(FeedTransferError, match="not allowed"):
        await transport.check_url("https://example.com:abc/feed")


async def test_check_url_accepts_global_literal_ip() -> None:
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_private)
    await transport.check_url("https://93.184.216.34/feed")


async def test_check_url_rejects_loopback_literal_ip() -> None:
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_public)
    with pytest.raises(FeedTransferError, match="not allowed"):
        await transport.check_url("http://127.0.0.1/feed")


async def test_check_url_rejects_private_network_literal_ip() -> None:
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_public)
    with pytest.raises(FeedTransferError, match="not allowed"):
        await transport.check_url("http://10.0.0.1/feed")


async def test_check_url_uses_resolver_and_accepts_global_addresses() -> None:
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_public)
    await transport.check_url("https://example.com/feed")


async def test_check_url_rejects_when_resolver_returns_private() -> None:
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_private)
    with pytest.raises(FeedTransferError, match="not allowed"):
        await transport.check_url("https://example.com/feed")


async def test_check_url_rejects_when_resolver_returns_empty() -> None:
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_empty)
    with pytest.raises(FeedTransferError, match="not allowed"):
        await transport.check_url("https://example.com/feed")


async def test_check_url_rejects_mixed_global_and_private_addresses() -> None:
    async def mixed(hostname: str, port: int) -> set[str]:
        del hostname, port
        return {"93.184.216.34", "10.0.0.1"}

    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=mixed)
    with pytest.raises(FeedTransferError, match="not allowed"):
        await transport.check_url("https://example.com/feed")


async def test_allowed_private_origin_matches_scheme_host_port() -> None:
    transport = SafeFeedHTTP(
        httpx.AsyncClient(),
        allowed_private_origins={"http://rsshub.internal:1200"},
        resolver=_return_private,
    )
    assert transport.allowed_private_origins == {("http", "rsshub.internal", 1200)}
    await transport.check_url("http://rsshub.internal:1200/route")


async def test_allowed_private_origin_does_not_allow_other_scheme() -> None:
    transport = SafeFeedHTTP(
        httpx.AsyncClient(),
        allowed_private_origins={"http://rsshub.internal:1200"},
        resolver=_return_private,
    )
    with pytest.raises(FeedTransferError, match="not allowed"):
        await transport.check_url("https://rsshub.internal:1200/route")


async def test_allowed_private_origin_does_not_allow_other_port() -> None:
    transport = SafeFeedHTTP(
        httpx.AsyncClient(),
        allowed_private_origins={"http://rsshub.internal:1200"},
        resolver=_return_private,
    )
    with pytest.raises(FeedTransferError, match="not allowed"):
        await transport.check_url("http://rsshub.internal:2375/containers/json")


async def test_allowed_private_origin_normalizes_default_port() -> None:
    transport = SafeFeedHTTP(
        httpx.AsyncClient(),
        allowed_private_origins={"http://rsshub.internal"},
        resolver=_return_private,
    )
    await transport.check_url("http://rsshub.internal:80/route")
    with pytest.raises(FeedTransferError, match="not allowed"):
        await transport.check_url("http://rsshub.internal:8080/route")


@respx.mock
async def test_stream_rejects_redirect_to_loopback_mid_chain() -> None:
    first = respx.get("https://example.com/redirect").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/feed"})
    )
    private = respx.get("http://127.0.0.1/feed").mock(return_value=httpx.Response(200))
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_public)
    with pytest.raises(FeedTransferError, match="not allowed"):
        async with transport.stream("https://example.com/redirect") as _:
            pass
    assert first.called
    assert not private.called


@respx.mock
async def test_stream_yields_redirect_response_without_location() -> None:
    respx.get("https://example.com/feed").mock(return_value=httpx.Response(302, headers={}))
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_public)
    async with transport.stream("https://example.com/feed") as response:
        assert response.status_code == 302


@respx.mock
async def test_stream_follows_up_to_five_redirects() -> None:
    for hop in range(5):
        respx.get(f"https://example.com/hop{hop}").mock(
            return_value=httpx.Response(
                302, headers={"location": f"https://example.com/hop{hop + 1}"}
            )
        )
    respx.get("https://example.com/hop5").mock(return_value=httpx.Response(200, content=b"ok"))
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_public)
    async with transport.stream("https://example.com/hop0") as response:
        body = await transport.read_limited(response)
    assert body == b"ok"


@respx.mock
async def test_stream_rejects_sixth_redirect() -> None:
    for hop in range(6):
        respx.get(f"https://example.com/hop{hop}").mock(
            return_value=httpx.Response(
                302, headers={"location": f"https://example.com/hop{hop + 1}"}
            )
        )
    respx.get("https://example.com/hop6").mock(return_value=httpx.Response(200, content=b"ok"))
    transport = SafeFeedHTTP(httpx.AsyncClient(), resolver=_return_public)
    with pytest.raises(FeedTransferError, match="too many times"):
        async with transport.stream("https://example.com/hop0") as _:
            pass


@respx.mock
async def test_read_limited_returns_content_under_limit() -> None:
    respx.get("https://example.com/feed").mock(return_value=httpx.Response(200, content=b"abcdef"))
    transport = SafeFeedHTTP(httpx.AsyncClient(), max_bytes=10, resolver=_return_public)
    async with transport.stream("https://example.com/feed") as response:
        assert await transport.read_limited(response) == b"abcdef"


@respx.mock
async def test_read_limited_raises_when_exceeding_limit() -> None:
    respx.get("https://example.com/feed").mock(return_value=httpx.Response(200, content=b"abcdef"))
    transport = SafeFeedHTTP(httpx.AsyncClient(), max_bytes=5, resolver=_return_public)
    async with transport.stream("https://example.com/feed") as response:
        with pytest.raises(FeedTransferError, match="too large"):
            await transport.read_limited(response)
