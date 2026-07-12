from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from feedsentry.domain import goal_hash
from feedsentry.feeds import FeedClient
from feedsentry.repository import Repository

_SOURCE_RETRY_MINUTES = (1, 5, 30, 120)
_MAX_ERROR_LENGTH = 1_000
_POLL_INTERVAL = timedelta(minutes=1)


class IngestionService:
    def __init__(self, repository: Repository, feed_client: FeedClient) -> None:
        self.repository = repository
        self.feed_client = feed_client

    async def poll_source(self, source_url: str, goal: str) -> int:
        state = await self.repository.get_feed_state(source_url)
        try:
            result = await self.feed_client.fetch(
                source_url,
                etag=state.etag if state else None,
                last_modified=state.last_modified if state else None,
            )
        except httpx.HTTPError as exc:
            now = datetime.now(UTC)
            failures = (state.consecutive_failures if state else 0) + 1
            delay = _SOURCE_RETRY_MINUTES[min(failures, len(_SOURCE_RETRY_MINUTES)) - 1]
            await self.repository.record_feed_failure(
                source_url,
                error=str(exc)[:_MAX_ERROR_LENGTH],
                checked_at=now,
                next_check_at=now + timedelta(minutes=delay),
            )
            return 0

        now = datetime.now(UTC)
        next_check_at = now + _POLL_INTERVAL
        if result.not_modified:
            await self.repository.record_feed_success(
                source_url,
                etag=state.etag if state else None,
                last_modified=state.last_modified if state else None,
                checked_at=now,
                next_check_at=next_check_at,
            )
            return 0

        records = [
            await self.repository.upsert_entry(**item.as_repository_kwargs())
            for item in result.entries
        ]
        if state is None or state.initialized_at is None:
            await self.repository.mark_feed_initialized(source_url, datetime.now(UTC))
            created = 0
        else:
            created = 0
            for record in records:
                if record.first_seen_at > state.initialized_at:
                    await self.repository.create_event(record.id, goal, goal_hash(goal))
                    created += 1

        await self.repository.record_feed_success(
            source_url,
            etag=result.etag,
            last_modified=result.last_modified,
            checked_at=now,
            next_check_at=next_check_at,
        )
        return created
