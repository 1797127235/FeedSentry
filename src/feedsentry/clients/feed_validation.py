from __future__ import annotations

from dataclasses import dataclass

import feedparser
import httpx

from feedsentry.clients.feed_http import AddressResolver, FeedTransferError, SafeFeedHTTP
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
        allowed_private_origins: set[str] | None = None,
        resolver: AddressResolver | None = None,
    ) -> None:
        self.transport = SafeFeedHTTP(
            http,
            max_bytes=max_bytes,
            allowed_private_origins=allowed_private_origins,
            resolver=resolver,
        )

    async def validate(self, url: str) -> ValidatedFeed:
        try:
            async with self.transport.stream(url) as response:
                response.raise_for_status()
                canonical_url = str(response.url)
                content = await self.transport.read_limited(response)
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")
        except FeedTransferError as exc:
            raise FeedValidationError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise FeedValidationError("feed request failed") from exc

        # bozo 表示解析遇到问题；只有完全解析不出 feed/entries 才判定为非法
        parsed = feedparser.parse(content)
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
