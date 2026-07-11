from __future__ import annotations

import pytest
from pydantic import ValidationError

from feedsentry.config import ConfigManager, load_config, redact_mapping

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
