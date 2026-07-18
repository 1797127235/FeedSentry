from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import struct_time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import httpx

from feedsentry.clients.feed_http import AddressResolver, SafeFeedHTTP


@dataclass(frozen=True)
class NormalizedEntry:
    source_url: str
    external_id: str
    title: str
    summary: str
    link: str
    author: str | None
    published_at: datetime | None
    content_hash: str
    raw_json: str

    def as_repository_kwargs(self) -> dict[str, object]:
        return {
            "source_url": self.source_url,
            "external_id": self.external_id,
            "title": self.title,
            "summary": self.summary,
            "link": self.link,
            "author": self.author,
            "published_at": self.published_at,
            "content_hash": self.content_hash,
            "raw_json": self.raw_json,
        }


@dataclass(frozen=True)
class FeedFetchResult:
    not_modified: bool
    etag: str | None
    last_modified: str | None
    entries: tuple[NormalizedEntry, ...]
    title: str | None = None


def normalize_feed(content: bytes, source_url: str) -> tuple[NormalizedEntry, ...]:
    parsed = feedparser.parse(content)
    return normalize_parsed_feed(parsed, source_url)


def normalize_parsed_feed_with_title(
    parsed: Any, source_url: str, link_base_url: str | None = None
) -> tuple[str | None, tuple[NormalizedEntry, ...]]:
    title = _normalize_text(parsed.feed.get("title")) or None
    return title, normalize_parsed_feed(parsed, source_url, link_base_url)


def normalize_parsed_feed(
    parsed: Any, source_url: str, link_base_url: str | None = None
) -> tuple[NormalizedEntry, ...]:
    base_url = link_base_url or source_url
    return tuple(_normalize_entry(entry, source_url, base_url) for entry in parsed.entries)


def _normalize_entry(entry: Any, source_url: str, link_base_url: str) -> NormalizedEntry:
    title = _normalize_text(entry.get("title"))
    summary = _normalize_text(entry.get("summary", entry.get("description")))
    link = _normalize_url(entry.get("link"), link_base_url)
    author_value = _normalize_text(entry.get("author"))
    author = author_value or None
    published_at = _published_at(entry)
    published = (
        published_at.isoformat()
        if published_at is not None
        else _normalize_text(entry.get("published", entry.get("updated")))
    )
    external_id = _normalize_text(entry.get("id")) or link or _digest(title, summary, published)
    content_hash = _digest(title, summary, link, author or "", published)

    return NormalizedEntry(
        source_url=source_url,
        external_id=external_id,
        title=title,
        summary=summary,
        link=link,
        author=author,
        published_at=published_at,
        content_hash=content_hash,
        raw_json=json.dumps(entry, default=str, ensure_ascii=False, sort_keys=True),
    )


def _normalize_text(value: object | None) -> str:
    return " ".join(str(value or "").split())


def _normalize_url(value: object | None, base_url: str | None = None) -> str:
    raw_url = _normalize_text(value)
    if not raw_url:
        return ""
    if base_url is not None:
        raw_url = urljoin(base_url, raw_url)

    parsed = urlsplit(raw_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    userinfo = ""
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo = f"{userinfo}:{parsed.password}"
        userinfo = f"{userinfo}@"

    return urlunsplit((scheme, f"{userinfo}{host}", parsed.path or "/", parsed.query, ""))


def _published_at(entry: Any) -> datetime | None:
    value = entry.get("published_parsed") or entry.get("updated_parsed")
    if not isinstance(value, struct_time):
        return None
    try:
        return datetime(*value[:6], tzinfo=UTC)
    except ValueError:
        return None


def _digest(*values: str) -> str:
    normalized = "\x1f".join(values)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class FeedClient:
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

    async def fetch(
        self, source_url: str, etag: str | None = None, last_modified: str | None = None
    ) -> FeedFetchResult:
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}
        if etag is not None:
            headers["If-None-Match"] = etag
        if last_modified is not None:
            headers["If-Modified-Since"] = last_modified

        async with self.transport.stream(source_url, headers=headers) as response:
            if response.status_code == httpx.codes.NOT_MODIFIED:
                return FeedFetchResult(True, etag, last_modified, ())
            response.raise_for_status()
            content = await self.transport.read_limited(response)
            title, entries = normalize_parsed_feed_with_title(
                feedparser.parse(content), source_url, str(response.url)
            )
            return FeedFetchResult(
                not_modified=False,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                entries=entries,
                title=title,
            )
