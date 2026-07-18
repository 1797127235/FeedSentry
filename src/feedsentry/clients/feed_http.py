from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit

import httpx

AddressResolver = Callable[[str, int], Awaitable[set[str]]]
Origin = tuple[str, str, int]


class FeedTransferError(httpx.RequestError):
    """A feed request rejected before or during its bounded transfer."""


class SafeFeedHTTP:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        max_bytes: int = 5_000_000,
        allowed_private_origins: set[str] | None = None,
        resolver: AddressResolver | None = None,
    ) -> None:
        self.http = http
        self.max_bytes = max_bytes
        self.allowed_private_origins: set[Origin] = {
            self._origin(origin) for origin in (allowed_private_origins or set())
        }
        self._resolver = resolver or self._resolve

    @asynccontextmanager
    async def stream(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> AsyncIterator[httpx.Response]:
        current_url = url
        for redirect_count in range(6):
            await self.check_url(current_url)
            async with self.http.stream(
                "GET", current_url, headers=headers, follow_redirects=False
            ) as response:
                if response.has_redirect_location:
                    location = response.headers.get("location")
                    if location is None or redirect_count == 5:
                        raise FeedTransferError("feed redirected too many times")
                    current_url = urljoin(str(response.url), location)
                    continue
                yield response
                return
        raise FeedTransferError("feed redirected too many times")  # pragma: no cover

    async def read_limited(self, response: httpx.Response) -> bytes:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > self.max_bytes:
                raise FeedTransferError("feed response is too large", request=response.request)
            content.extend(chunk)
        return bytes(content)

    async def check_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise FeedTransferError("feed URL is not allowed")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise FeedTransferError("feed URL is not allowed") from exc
        hostname = parsed.hostname.lower()
        if (parsed.scheme, hostname, port) in self.allowed_private_origins:
            return
        try:
            literal_address = ipaddress.ip_address(hostname)
        except ValueError:
            addresses = await self._resolver(hostname, port)
        else:
            addresses = {str(literal_address)}
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise FeedTransferError("feed URL is not allowed")

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("allowed private origin must be an HTTP(S) URL")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname.lower(), port

    @staticmethod
    async def _resolve(hostname: str, port: int) -> set[str]:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            loop = asyncio.get_running_loop()
            try:
                results = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise FeedTransferError("feed host could not be resolved") from exc
            return {result[4][0] for result in results}
        return {hostname}
