from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from feedsentry.domain import (
    DecisionAction,
    EventStatus,
    ScreeningDecision,
    assert_transition,
    goal_hash,
    next_retry_at,
)


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
