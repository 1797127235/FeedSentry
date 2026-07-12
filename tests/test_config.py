from __future__ import annotations

import pytest
from pydantic import ValidationError

from feedsentry.config import ConfigManager, DestinationConfig, load_config, redact_mapping

VALID_CONFIG = """
integrations:
  firecrawl:
    base_url: ${FIRECRAWL_URL}
    api_key: ${FIRECRAWL_KEY:-}
  apprise:
    base_url: http://apprise:8000
ai:
  base_url: http://llm:8080/v1
  api_key: secret-ai-key
  model: test-model
storage:
  path: ./data/test.db
monitors:
  - id: releases
    name: Releases
    goal: Important releases only
    interval: 10m
    sources: [https://example.com/feed.xml]
    destination: {apprise_key: telegram}
"""


def write_config(path, content: str = VALID_CONFIG) -> None:
    path.write_text(content, encoding="utf-8")


def test_load_config_expands_environment_and_parses_interval(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(config_path)

    config = load_config(config_path)

    assert str(config.integrations.firecrawl.base_url) == "http://firecrawl:3002/"
    assert config.integrations.firecrawl.api_key is None
    assert config.monitors[0].interval_seconds == 600


def test_load_config_supports_native_telegram_destination(tmp_path, monkeypatch) -> None:
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
        ).replace("destination: {apprise_key: telegram}", "destination: {kind: telegram}"),
    )

    config = load_config(config_path)

    assert config.monitors[0].destination.kind == "telegram"
    assert config.integrations.telegram is not None
    assert config.integrations.telegram.chat_id == "123"


def test_load_config_keeps_apprise_destination_compatible(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(config_path)

    config = load_config(config_path)

    assert config.monitors[0].destination.kind == "apprise"
    assert config.monitors[0].destination.apprise_key == "telegram"


def test_load_config_rejects_telegram_destination_without_integration(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        VALID_CONFIG.replace(
            "destination: {apprise_key: telegram}", "destination: {kind: telegram}"
        ),
    )

    with pytest.raises(ValidationError, match="integrations.telegram"):
        load_config(config_path)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"kind": "apprise"}, "apprise_key"),
        ({"kind": "telegram", "apprise_key": "telegram"}, "apprise_key"),
    ],
)
def test_destination_config_rejects_invalid_kind_and_key_combinations(kwargs, match) -> None:
    with pytest.raises(ValidationError, match=match):
        DestinationConfig(**kwargs)


def test_destination_config_rejects_explicit_null_apprise_key_for_telegram() -> None:
    with pytest.raises(ValidationError, match="apprise_key"):
        DestinationConfig(kind="telegram", apprise_key=None)


def test_load_config_rejects_duplicate_monitor_ids(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        VALID_CONFIG
        + """
  - id: releases
    name: Duplicate
    goal: Duplicate monitor
    interval: 10m
    sources: [https://example.com/duplicate.xml]
    destination: {apprise_key: telegram}
""",
    )

    with pytest.raises((ValidationError, ValueError)):
        load_config(config_path)


def test_load_config_rejects_invalid_interval(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, VALID_CONFIG.replace("10m", "often"))

    with pytest.raises(ValidationError):
        load_config(config_path)


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
    write_config(config_path, "monitors: [")

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
