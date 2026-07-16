from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from feedsentry.clients.feeds import NormalizedEntry, normalize_parsed_feed


class FeedValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedFeed:
    canonical_url: str
    title: str
    version: str
    etag: str | None
    last_modified: str | None
    entries: tuple[NormalizedEntry, ...]


class FeedValidator:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        max_bytes: int = 5_000_000,
        allowed_private_hosts: set[str] | None = None,
    ) -> None:
        self.http = http
        self.max_bytes = max_bytes
        self.allowed_private_hosts = {host.lower() for host in (allowed_private_hosts or set())}

    async def validate(self, url: str) -> ValidatedFeed:
        current_url = url
        try:
            for redirect_count in range(6):
                await self._check_url(current_url)
                async with self.http.stream("GET", current_url, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if location is None or redirect_count == 5:
                            raise FeedValidationError("feed redirected too many times")
                        current_url = urljoin(str(response.url), location)
                        continue
                    response.raise_for_status()
                    canonical_url = str(response.url)
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self.max_bytes:
                            raise FeedValidationError("feed response is too large")
                    etag = response.headers.get("etag")
                    last_modified = response.headers.get("last-modified")
                    break
            else:  # pragma: no cover
                raise FeedValidationError("feed redirected too many times")
        except FeedValidationError:
            raise
        except httpx.HTTPError as exc:
            raise FeedValidationError("feed request failed") from exc

        parsed = feedparser.parse(bytes(content))
        if not parsed.version or (parsed.bozo and not parsed.feed and not parsed.entries):
            raise FeedValidationError("response is not valid RSS or Atom")
        return ValidatedFeed(
            canonical_url=canonical_url,
            title=" ".join(str(parsed.feed.get("title") or "").split()),
            version=str(parsed.version),
            etag=etag,
            last_modified=last_modified,
            entries=normalize_parsed_feed(parsed, canonical_url),
        )

    async def _check_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise FeedValidationError("feed URL is not allowed")
        hostname = parsed.hostname.lower()
        if hostname in self.allowed_private_hosts:
            return
        addresses = await self._resolve(
            hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
        if not addresses or any(self._is_forbidden(address) for address in addresses):
            raise FeedValidationError("feed URL is not allowed")

    @staticmethod
    async def _resolve(hostname: str, port: int) -> set[str]:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            loop = asyncio.get_running_loop()
            try:
                results = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise FeedValidationError("feed host could not be resolved") from exc
            return {result[4][0] for result in results}
        return {hostname}

    @staticmethod
    def _is_forbidden(value: str) -> bool:
        address = ipaddress.ip_address(value)
        return not address.is_global
