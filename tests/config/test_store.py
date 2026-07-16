from __future__ import annotations

import asyncio

import pytest
import yaml
from test_config import VALID_CONFIG

from feedsentry.config import ConfigManager, DirectSourceConfig
from feedsentry.config_store import ConfigStore


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIG.replace("api_key: secret-ai-key", "api_key: ${AI_API_KEY}"),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_API_KEY", "real-secret")
    return path


@pytest.fixture
def store(config_path) -> ConfigStore:
    manager = ConfigManager(config_path)
    manager.load_initial()
    return ConfigStore(manager)


async def test_add_source_preserves_environment_placeholders(store, config_path) -> None:
    created = await store.add_source(
        DirectSourceConfig(
            id="other", kind="feed", url="https://example.com/other.xml", enabled=True
        )
    )

    assert created is True
    content = config_path.read_text(encoding="utf-8")
    assert "${AI_API_KEY}" in content
    assert "real-secret" not in content
    assert [source.id for source in store.manager.current.sources] == ["example", "other"]


async def test_add_source_is_idempotent(store) -> None:
    existing = store.manager.current.sources[0]
    assert await store.add_source(existing) is False


async def test_enable_remove_and_set_goal(store) -> None:
    assert await store.set_source_enabled("example", False) is True
    assert store.manager.current.sources[0].enabled is False
    assert await store.set_filter_goal("Only security releases") is True
    assert store.manager.current.filter.goal == "Only security releases"
    assert await store.remove_source("example") is True
    assert store.manager.current.sources == []


async def test_concurrent_mutations_do_not_overwrite_each_other(store) -> None:
    first = DirectSourceConfig(id="first", kind="feed", url="https://example.com/first")
    second = DirectSourceConfig(id="second", kind="feed", url="https://example.com/second")

    await asyncio.gather(store.add_source(first), store.add_source(second))

    assert {source.id for source in store.manager.current.sources} == {"example", "first", "second"}


async def test_invalid_mutation_keeps_original_bytes(store, config_path) -> None:
    original = config_path.read_bytes()

    with pytest.raises(ValueError):
        await store.set_filter_goal("")

    assert config_path.read_bytes() == original


async def test_replace_failure_keeps_original_file(store, config_path, monkeypatch) -> None:
    original = config_path.read_bytes()

    def fail_replace(source, target) -> None:
        del source, target
        raise OSError("disk failure")

    monkeypatch.setattr("feedsentry.config_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        await store.set_filter_goal("New goal")

    assert config_path.read_bytes() == original
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["filter"]["goal"] == (
        "Important releases only"
    )
