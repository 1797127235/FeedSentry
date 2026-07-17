from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


@dataclass(frozen=True)
class Notification:
    title: str
    summary: str
    source_url: str
    link: str
    source_title: str | None = None


class DecisionAction(StrEnum):
    DISCARD = "discard"
    ACCEPT = "accept"
    FETCH = "fetch"


class EventStatus(StrEnum):
    DISCOVERED = "discovered"
    SCREENING = "screening"
    FILTERED = "filtered"
    FETCHING = "fetching"
    SUMMARIZING = "summarizing"
    DELIVERY_PENDING = "delivery_pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"


class ScreeningDecision(BaseModel):
    action: DecisionAction
    reason: str = ""
    title: str | None = None
    summary: str | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> ScreeningDecision:
        if self.action is DecisionAction.ACCEPT and not self.summary:
            raise ValueError("accept decisions require a summary")
        if self.action is DecisionAction.FETCH and self.summary is not None:
            raise ValueError("fetch decisions must not include a summary")
        return self


ALLOWED_TRANSITIONS: dict[EventStatus, frozenset[EventStatus]] = {
    EventStatus.DISCOVERED: frozenset({EventStatus.SCREENING}),
    EventStatus.SCREENING: frozenset(
        {
            EventStatus.FILTERED,
            EventStatus.FETCHING,
            EventStatus.DELIVERY_PENDING,
            EventStatus.RETRY_WAIT,
        }
    ),
    EventStatus.FETCHING: frozenset({EventStatus.SUMMARIZING, EventStatus.RETRY_WAIT}),
    EventStatus.SUMMARIZING: frozenset(
        {EventStatus.FILTERED, EventStatus.DELIVERY_PENDING, EventStatus.RETRY_WAIT}
    ),
    EventStatus.DELIVERY_PENDING: frozenset({EventStatus.DELIVERING}),
    EventStatus.DELIVERING: frozenset({EventStatus.DELIVERED, EventStatus.RETRY_WAIT}),
    EventStatus.RETRY_WAIT: frozenset(
        {
            EventStatus.SCREENING,
            EventStatus.FETCHING,
            EventStatus.SUMMARIZING,
            EventStatus.DELIVERING,
            EventStatus.FAILED,
        }
    ),
}


def assert_transition(current: EventStatus, target: EventStatus) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"invalid event transition: {current} -> {target}")


RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
)


def next_retry_at(now: datetime, attempt: int) -> datetime | None:
    if not 1 <= attempt <= len(RETRY_DELAYS):
        return None
    return now + RETRY_DELAYS[attempt - 1]


def goal_hash(goal: str) -> str:
    normalized_goal = " ".join(goal.split())
    return hashlib.sha256(normalized_goal.encode("utf-8")).hexdigest()
