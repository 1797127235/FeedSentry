from __future__ import annotations

import pytest
from conftest import VALID_CONFIG
from pydantic import ValidationError

from feedsentry.config.models import ConfigManager, DestinationConfig, load_config, redact_mapping


def write_config(path, content: str = VALID_CONFIG) -> None:
    path.write_text(content, encoding="utf-8")


def test_load_config_expands_environment_and_loads_global_pipeline(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(config_path)

    config = load_config(config_path)

    assert str(config.integrations.firecrawl.base_url) == "http://firecrawl:3002/"
    assert config.integrations.firecrawl.api_key is None
    assert config.filter.goal == "Important releases only"
    assert config.sources[0].id == "example"
    assert config.sources[0].kind == "feed"
    assert str(config.sources[0].url) == "https://example.com/feed.xml"
    assert config.sources[0].feed_url(config.integrations.rsshub) == "https://example.com/feed.xml"
    assert config.sources[0].enabled is True
    assert config.destination.apprise_key == "telegram"


def test_load_config_supports_global_telegram_destination(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        VALID_CONFIG.replace(
            "  apprise:\n    base_url: http://apprise:8000",
            "  apprise:\n    base_url: http://apprise:8000\n"
            "  telegram:\n"
            "    bot_token: ${TELEGRAM_BOT_TOKEN}\n"
            "    chat_id: ${TELEGRAM_CHAT_ID}",
        ).replace("destination:\n  apprise_key: telegram", "destination:\n  kind: telegram"),
    )

    config = load_config(config_path)

    assert config.destination.kind == "telegram"
    assert config.integrations.telegram is not None
    assert config.integrations.telegram.chat_id == "123"


def test_load_config_rejects_duplicate_source_urls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        VALID_CONFIG.replace(
            "destination:\n",
            "  - id: duplicate\n"
            "    kind: feed\n"
            "    url: https://example.com/feed.xml\n"
            "    enabled: false\n"
            "destination:\n",
        ),
    )

    with pytest.raises(ValidationError, match="source URLs must be unique"):
        load_config(config_path)


def test_load_config_rejects_old_monitor_shape(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        VALID_CONFIG.replace(
            "filter:\n  goal: Important releases only\nsources:\n"
            "  - id: example\n    kind: feed\n"
            "    url: https://example.com/feed.xml\n    enabled: true\ndestination:\n"
            "  apprise_key: telegram",
            "monitors: []",
        ),
    )

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_resolves_rsshub_source(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        VALID_CONFIG.replace(
            "  - id: example\n    kind: feed\n    url: https://example.com/feed.xml",
            "  - id: bilibili-video\n"
            "    kind: rsshub\n"
            "    page_url: https://space.bilibili.com/946974\n"
            "    route: /bilibili/user/video/946974",
        ),
    )

    config = load_config(config_path)

    source = config.sources[0]
    assert source.kind == "rsshub"
    assert source.feed_url(config.integrations.rsshub) == (
        "https://rsshub.antest.cc.cd/bilibili/user/video/946974"
    )


@pytest.mark.parametrize(
    ("replacement", "match"),
    [
        ("id: Bad_ID", "string_pattern_mismatch"),
        ("id: example", "source IDs must be unique"),
    ],
)
def test_load_config_rejects_invalid_or_duplicate_source_ids(
    tmp_path, monkeypatch, replacement, match
) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    if replacement == "id: example":
        addition = "  - id: example\n    kind: feed\n    url: https://example.com/other.xml\n"
        content = VALID_CONFIG.replace("destination:\n", addition + "destination:\n")
    else:
        content = VALID_CONFIG.replace("id: example", replacement)
    write_config(config_path, content)

    with pytest.raises(ValidationError, match=match):
        load_config(config_path)


@pytest.mark.parametrize(
    "source_yaml",
    [
        "id: direct\n    kind: feed",
        "id: hub\n    kind: rsshub\n    route: /bilibili/user/video/1",
        "id: hub\n    kind: rsshub\n    page_url: https://space.bilibili.com/1",
        "id: hub\n    kind: rsshub\n    page_url: https://space.bilibili.com/1\n"
        "    route: bilibili/user/video/1",
    ],
)
def test_load_config_rejects_incomplete_sources(tmp_path, monkeypatch, source_yaml) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    content = VALID_CONFIG.replace(
        "id: example\n    kind: feed\n    url: https://example.com/feed.xml", source_yaml
    )
    write_config(config_path, content)

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_rejects_telegram_destination_without_integration(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        VALID_CONFIG.replace(
            "destination:\n  apprise_key: telegram", "destination:\n  kind: telegram"
        ),
    )

    with pytest.raises(ValidationError, match="integrations.telegram"):
        load_config(config_path)


def test_load_config_supports_global_qq_destination(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    monkeypatch.setenv("NAPCAT_BASE_URL", "http://napcat:3000")
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        VALID_CONFIG.replace(
            "  apprise:\n    base_url: http://apprise:8000",
            "  apprise:\n    base_url: http://apprise:8000\n"
            "  qq:\n"
            "    base_url: ${NAPCAT_BASE_URL}\n"
            "    access_token: ${NAPCAT_TOKEN:-}\n"
            "    target_type: group\n"
            "    target_id: 987654321",
        ).replace("destination:\n  apprise_key: telegram", "destination:\n  kind: qq"),
    )

    config = load_config(config_path)

    assert config.destination.kind == "qq"
    assert config.integrations.qq is not None
    assert str(config.integrations.qq.base_url) == "http://napcat:3000/"
    assert config.integrations.qq.access_token is None
    assert config.integrations.qq.target_type == "group"
    assert config.integrations.qq.target_id == "987654321"


def test_load_config_rejects_qq_destination_without_integration(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        VALID_CONFIG.replace("destination:\n  apprise_key: telegram", "destination:\n  kind: qq"),
    )

    with pytest.raises(ValidationError, match="integrations.qq"):
        load_config(config_path)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"kind": "apprise"}, "apprise_key"),
        ({"kind": "telegram", "apprise_key": "telegram"}, "apprise_key"),
        ({"kind": "qq", "apprise_key": "telegram"}, "apprise_key"),
    ],
)
def test_destination_config_rejects_invalid_kind_and_key_combinations(kwargs, match) -> None:
    with pytest.raises(ValidationError, match=match):
        DestinationConfig(**kwargs)


def test_redact_mapping_masks_nested_secrets() -> None:
    redacted = redact_mapping({"api_key": "abc", "nested": {"password": "xyz", "model": "m"}})

    assert redacted == {"api_key": "***", "nested": {"password": "***", "model": "m"}}


def test_config_manager_keeps_last_known_good_config_on_invalid_reload(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(config_path)
    manager = ConfigManager(config_path)

    original = manager.load_initial()
    write_config(config_path, "sources: [")

    assert manager.reload_if_changed() is False
    assert manager.current is original
    assert manager.last_error is not None


def test_config_manager_keeps_current_when_config_file_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(config_path)
    manager = ConfigManager(config_path)
    original = manager.load_initial()
    original_mtime = manager.mtime
    config_path.unlink()

    assert manager.reload_if_changed() is False
    assert manager.current is original
    assert manager.mtime == original_mtime
    assert manager.last_error is not None

    write_config(config_path)

    assert manager.reload_if_changed() is True
    assert manager.current is not original
    assert manager.last_error is None


def test_config_manager_does_not_expose_secrets_in_reload_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(config_path)
    manager = ConfigManager(config_path)
    original = manager.load_initial()
    write_config(
        config_path,
        VALID_CONFIG.replace("api_key: ${FIRECRAWL_KEY:-}", "api_key: [real-api-secret]"),
    )

    assert manager.reload_if_changed() is False
    assert manager.current is original
    assert manager.last_error is not None
    assert "real-api-secret" not in manager.last_error
