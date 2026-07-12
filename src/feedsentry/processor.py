from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from feedsentry.config import DestinationConfig
from feedsentry.domain import DecisionAction, EventStatus, Notification, ScreeningDecision
from feedsentry.repository import EventBundle, Repository


class AI(Protocol):
    async def screen(self, goal: str, title: str, feed_summary: str) -> ScreeningDecision: ...

    async def summarize(self, goal: str, title: str, markdown: str) -> ScreeningDecision: ...


class Firecrawl(Protocol):
    async def scrape(self, url: str) -> str: ...


class Apprise(Protocol):
    async def notify(self, key: str, title: str, body: str) -> str: ...


class DestinationProvider(Protocol):
    def __call__(self) -> str | DestinationConfig: ...


class Telegram(Protocol):
    chat_id: str

    async def notify(self, notification: Notification) -> str: ...


class EventProcessor:
    def __init__(
        self,
        repository: Repository,
        ai: AI,
        firecrawl: Firecrawl,
        apprise: Apprise,
        destination: str | DestinationConfig | DestinationProvider,
        telegram: Telegram | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.ai = ai
        self.firecrawl = firecrawl
        self.apprise = apprise
        self.destination = destination
        self.telegram = telegram
        self.clock = clock or (lambda: datetime.now(UTC))

    async def process_event(self, event_id: int) -> None:
        while True:
            bundle = await self.repository.get_event_bundle(event_id)
            status = bundle.event.status
            if status in {EventStatus.FILTERED, EventStatus.DELIVERED, EventStatus.FAILED}:
                return
            if status is EventStatus.RETRY_WAIT:
                if (
                    bundle.event.next_attempt_at is not None
                    and bundle.event.next_attempt_at > self.clock()
                ):
                    return
                await self.repository.resume_event(event_id)
                continue
            try:
                if status is EventStatus.DISCOVERED:
                    await self.repository.transition_event(
                        event_id, EventStatus.DISCOVERED, EventStatus.SCREENING
                    )
                elif status is EventStatus.SCREENING:
                    await self._screen(bundle)
                elif status is EventStatus.FETCHING:
                    await self._fetch(bundle)
                elif status is EventStatus.SUMMARIZING:
                    await self._summarize(bundle)
                elif status is EventStatus.DELIVERY_PENDING:
                    await self.repository.transition_event(
                        event_id, EventStatus.DELIVERY_PENDING, EventStatus.DELIVERING
                    )
                elif status is EventStatus.DELIVERING:
                    await self._deliver(bundle)
                else:
                    raise RuntimeError(f"unsupported event status: {status}")
            except Exception as exc:
                await self.repository.schedule_event_retry(event_id, status, str(exc)[:1000])
                return

    async def _screen(self, bundle: EventBundle) -> None:
        decision = await self.ai.screen(
            bundle.event.goal_snapshot, bundle.entry.title, bundle.entry.summary
        )
        if decision.action is DecisionAction.DISCARD:
            await self.repository.transition_event(
                bundle.event.id,
                EventStatus.SCREENING,
                EventStatus.FILTERED,
                decision_reason=decision.reason,
            )
        elif decision.action is DecisionAction.FETCH:
            await self.repository.transition_event(
                bundle.event.id,
                EventStatus.SCREENING,
                EventStatus.FETCHING,
                decision_reason=decision.reason,
            )
        else:
            await self._accept(bundle, EventStatus.SCREENING, decision)

    async def _fetch(self, bundle: EventBundle) -> None:
        scrape = bundle.scrape
        if scrape is None:
            markdown = await self.firecrawl.scrape(bundle.entry.link)
            await self.repository.save_scrape(
                bundle.entry.link,
                markdown,
                hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                self.clock(),
            )
        await self.repository.transition_event(
            bundle.event.id, EventStatus.FETCHING, EventStatus.SUMMARIZING
        )

    async def _summarize(self, bundle: EventBundle) -> None:
        if bundle.scrape is None:
            raise RuntimeError("scrape cache is missing")
        decision = await self.ai.summarize(
            bundle.event.goal_snapshot, bundle.entry.title, bundle.scrape.markdown
        )
        if decision.action is DecisionAction.DISCARD:
            await self.repository.transition_event(
                bundle.event.id,
                EventStatus.SUMMARIZING,
                EventStatus.FILTERED,
                decision_reason=decision.reason,
            )
            return
        if decision.action is not DecisionAction.ACCEPT:
            raise ValueError("final decision must accept or discard")
        await self._accept(bundle, EventStatus.SUMMARIZING, decision)

    async def _accept(
        self, bundle: EventBundle, current: EventStatus, decision: ScreeningDecision
    ) -> None:
        await self.repository.transition_event(
            bundle.event.id,
            current,
            EventStatus.DELIVERY_PENDING,
            decision_reason=decision.reason,
            output_title=decision.title or bundle.entry.title,
            output_summary=decision.summary,
        )

    async def _deliver(self, bundle: EventBundle) -> None:
        destination = self.destination() if callable(self.destination) else self.destination
        title = bundle.event.output_title or bundle.entry.title
        summary = bundle.event.output_summary or bundle.entry.summary
        if isinstance(destination, DestinationConfig) and destination.kind == "telegram":
            if self.telegram is None:
                raise RuntimeError("telegram destination is not configured")
            delivery = await self.repository.create_delivery(
                bundle.event.id, f"telegram:{self.telegram.chat_id}"
            )
            response = await self.telegram.notify(
                Notification(title, summary, bundle.entry.source_url, bundle.entry.link)
            )
            await self.repository.mark_delivery_success(delivery.id, response)
            await self.repository.transition_event(
                bundle.event.id, EventStatus.DELIVERING, EventStatus.DELIVERED
            )
            return
        apprise_key = (
            destination.apprise_key if isinstance(destination, DestinationConfig) else destination
        )
        if apprise_key is None:
            raise RuntimeError("apprise destination is not configured")
        delivery = await self.repository.create_delivery(bundle.event.id, apprise_key)
        reason = bundle.event.decision_reason or "Relevant to the monitoring goal"
        body = f"{summary}\n\nReason: {reason}\n\n{bundle.entry.link}"
        response = await self.apprise.notify(apprise_key, title, body)
        await self.repository.mark_delivery_success(delivery.id, response)
        await self.repository.transition_event(
            bundle.event.id, EventStatus.DELIVERING, EventStatus.DELIVERED
        )
