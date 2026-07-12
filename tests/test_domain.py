from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from feedsentry.domain import (
    DecisionAction,
    EventStatus,
    Notification,
    ScreeningDecision,
    assert_transition,
    goal_hash,
    next_retry_at,
)


def test_notification_can_be_constructed() -> None:
    notification = Notification(
        title="Release announced",
        summary="Version 1.0 is available.",
        source_url="https://example.com/feed.xml",
        link="https://example.com/releases/1.0",
    )

    assert notification.title == "Release announced"


def test_notification_is_immutable() -> None:
    notification = Notification(
        title="Release announced",
        summary="Version 1.0 is available.",
        source_url="https://example.com/feed.xml",
        link="https://example.com/releases/1.0",
    )

    with pytest.raises(FrozenInstanceError):
        notification.title = "Changed title"


def test_accept_decision_requires_summary() -> None:
    with pytest.raises(ValueError):
        ScreeningDecision(action=DecisionAction.ACCEPT, reason="relevant")


def test_assert_transition_rejects_invalid_event_transition() -> None:
    with pytest.raises(ValueError, match="invalid event transition"):
        assert_transition(EventStatus.DISCOVERED, EventStatus.DELIVERED)


def test_next_retry_at_uses_configured_backoff_and_stops_after_four_attempts() -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)

    assert next_retry_at(now, 1) == now + timedelta(minutes=1)
    assert next_retry_at(now, 4) == now + timedelta(hours=2)
    assert next_retry_at(now, 5) is None


def test_goal_hash_normalizes_whitespace() -> None:
    assert goal_hash("  Important releases\n") == goal_hash("Important releases")
