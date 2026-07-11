from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import struct_time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import feedparser
import httpx


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


@dataclass(frozen=True)
class FeedFetchResult:
    not_modified: bool
    etag: str | None
    last_modified: str | None
    entries: tuple[NormalizedEntry, ...]


def normalize_feed(content: bytes, source_url: str) -> tuple[NormalizedEntry, ...]:
    parsed = feedparser.parse(content)
    return tuple(_normalize_entry(entry, source_url) for entry in parsed.entries)


def _normalize_entry(entry: Any, source_url: str) -> NormalizedEntry:
    title = _normalize_text(entry.get("title"))
    summary = _normalize_text(entry.get("summary", entry.get("description")))
    link = _normalize_url(entry.get("link"))
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


def _normalize_url(value: object | None) -> str:
    raw_url = _normalize_text(value)
    if not raw_url:
        return ""

    parsed = urlsplit(raw_url)
    if not parsed.scheme or not parsed.hostname:
        return raw_url

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return raw_url
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
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def fetch(
        self, source_url: str, etag: str | None = None, last_modified: str | None = None
    ) -> FeedFetchResult:
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}
        if etag is not None:
            headers["If-None-Match"] = etag
        if last_modified is not None:
            headers["If-Modified-Since"] = last_modified

        response = await self._http.get(source_url, headers=headers, follow_redirects=True)
        if response.status_code == httpx.codes.NOT_MODIFIED:
            return FeedFetchResult(True, etag, last_modified, ())

        response.raise_for_status()
        return FeedFetchResult(
            not_modified=False,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            entries=normalize_feed(response.content, source_url),
        )
