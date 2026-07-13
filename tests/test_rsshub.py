from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from feedsentry.rsshub import CandidateCodec, RadarMatcher, RSSHubClient, RSSHubUnavailable

RULES = {
    "bilibili.com": {
        "_name": "Bilibili",
        "space": [
            {
                "title": "UP 主视频",
                "source": ["/:uid"],
                "target": "/bilibili/user/video/:uid",
            },
            {
                "title": "UP 主动态",
                "source": ["/:uid"],
                "target": "/bilibili/user/dynamic/:uid",
            },
        ],
    }
}


def test_radar_matcher_discovers_deduplicated_candidates() -> None:
    candidates = RadarMatcher().discover(
        "https://space.bilibili.com/946974",
        RULES,
        "https://rsshub.antest.cc.cd",
    )

    assert [(item.title, item.route) for item in candidates] == [
        ("UP 主视频", "/bilibili/user/video/946974"),
        ("UP 主动态", "/bilibili/user/dynamic/946974"),
    ]
    assert candidates[0].feed_url == ("https://rsshub.antest.cc.cd/bilibili/user/video/946974")


def test_radar_matcher_returns_empty_for_unknown_page() -> None:
    assert (
        RadarMatcher().discover(
            "https://example.com/channel/1", RULES, "https://rsshub.antest.cc.cd"
        )
        == []
    )


def test_candidate_codec_round_trips_and_rejects_tampering() -> None:
    now = datetime(2026, 7, 13, tzinfo=UTC)
    candidate = RadarMatcher().discover(
        "https://space.bilibili.com/946974", RULES, "https://rsshub.antest.cc.cd"
    )[0]
    codec = CandidateCodec(b"secret", clock=lambda: now, ttl=timedelta(minutes=5))

    token = codec.encode(candidate)

    assert codec.decode(token) == candidate
    with pytest.raises(ValueError, match="invalid candidate"):
        codec.decode(token[:-1] + ("a" if token[-1] != "a" else "b"))


def test_candidate_codec_rejects_expired_candidate() -> None:
    current = datetime(2026, 7, 13, tzinfo=UTC)
    codec = CandidateCodec(b"secret", clock=lambda: current, ttl=timedelta(seconds=1))
    candidate = RadarMatcher().discover(
        "https://space.bilibili.com/946974", RULES, "https://rsshub.antest.cc.cd"
    )[0]
    token = codec.encode(candidate)
    current += timedelta(seconds=2)

    with pytest.raises(ValueError, match="expired"):
        codec.decode(token)


@respx.mock
async def test_rsshub_client_caches_rules_and_uses_stale_cache_on_error() -> None:
    route = respx.get("https://rsshub.antest.cc.cd/api/radar/rules").mock(
        return_value=httpx.Response(200, json=RULES)
    )
    now = datetime(2026, 7, 13, tzinfo=UTC)
    async with httpx.AsyncClient() as http:
        client = RSSHubClient(
            http,
            "https://rsshub.antest.cc.cd",
            clock=lambda: now,
            cache_ttl=timedelta(minutes=5),
        )
        assert await client.rules() == RULES
        assert await client.rules() == RULES
        now += timedelta(minutes=6)
        route.mock(side_effect=httpx.ConnectError("offline"))
        assert await client.rules() == RULES

    assert route.call_count == 2


@respx.mock
async def test_rsshub_client_fails_without_valid_cache() -> None:
    respx.get("https://rsshub.antest.cc.cd/api/radar/rules").mock(
        side_effect=httpx.ConnectError("offline")
    )
    async with httpx.AsyncClient() as http:
        client = RSSHubClient(http, "https://rsshub.antest.cc.cd")
        with pytest.raises(RSSHubUnavailable):
            await client.rules()
