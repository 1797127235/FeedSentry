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
    """源校验失败（URL 非法、抓取失败或内容不是合法 RSS/Atom）。"""


@dataclass(frozen=True)
class ValidatedFeed:
    """校验通过的源快照：规范化 URL、元信息和首批条目。"""

    canonical_url: str
    title: str
    version: str
    etag: str | None
    last_modified: str | None
    entries: tuple[NormalizedEntry, ...]


class FeedValidator:
    """新增源时的试抓取校验器：SSRF 防护 + 受限下载 + 格式校验。"""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        max_bytes: int = 5_000_000,
        allowed_private_hosts: set[str] | None = None,
    ) -> None:
        self.http = http
        self.max_bytes = max_bytes
        # 允许放行的私网主机（如本机 RSSHub），小写存储便于比较
        self.allowed_private_hosts = {host.lower() for host in (allowed_private_hosts or set())}

    async def validate(self, url: str) -> ValidatedFeed:
        current_url = url
        try:
            # 手动跟随重定向，每一跳都重新做 SSRF 检查，最多 5 跳
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
                    # 流式下载，超过大小上限立即中止
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

        # bozo 表示解析遇到问题；只有完全解析不出 feed/entries 才判定为非法
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
        """SSRF 检查：仅允许 http/https，且目标 IP 必须全部是公网地址。"""
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
        """把主机名解析为 IP 集合；本身就是 IP 字面量时直接使用。"""
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
        """非公网地址（内网、回环、链路本地等）一律禁止。"""
        address = ipaddress.ip_address(value)
        return not address.is_global
