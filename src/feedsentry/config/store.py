from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from feedsentry.config.models import ConfigManager, SourceConfig, load_config


class ConfigStore:
    def __init__(self, manager: ConfigManager) -> None:
        self.manager = manager
        self._lock = asyncio.Lock()

    async def add_source(self, source: SourceConfig) -> bool:
        def mutate(data: dict[str, Any]) -> bool:
            sources = data.setdefault("sources", [])
            if any(item.get("id") == source.id for item in sources):
                return False
            sources.append(source.model_dump(mode="json", exclude_none=True))
            return True

        return await self._update(mutate)

    async def set_source_enabled(self, source_id: str, enabled: bool) -> bool:
        def mutate(data: dict[str, Any]) -> bool:
            for source in data.get("sources", []):
                if source.get("id") == source_id:
                    if source.get("enabled", True) == enabled:
                        return False
                    source["enabled"] = enabled
                    return True
            raise LookupError(f"source not found: {source_id}")

        return await self._update(mutate)

    async def remove_source(self, source_id: str) -> bool:
        def mutate(data: dict[str, Any]) -> bool:
            sources = data.get("sources", [])
            retained = [source for source in sources if source.get("id") != source_id]
            if len(retained) == len(sources):
                return False
            data["sources"] = retained
            return True

        return await self._update(mutate)

    async def set_filter_goal(self, goal: str) -> bool:
        def mutate(data: dict[str, Any]) -> bool:
            current = data.setdefault("filter", {}).get("goal")
            if current == goal:
                return False
            data["filter"]["goal"] = goal
            return True

        return await self._update(mutate)

    async def append_filter_goal(self, text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            raise ValueError("appended filter goal must not be empty")

        def mutate(data: dict[str, Any]) -> bool:
            current = data.setdefault("filter", {}).get("goal") or ""
            existing = [line.strip() for line in current.splitlines() if line.strip()]
            if normalized in existing:
                return False
            data["filter"]["goal"] = f"{current}\n{normalized}" if current else normalized
            return True

        return await self._update(mutate)

    async def _update(self, mutate: Callable[[dict[str, Any]], bool]) -> bool:
        async with self._lock:
            data = self._read_mapping()
            changed = mutate(data)
            if not changed:
                return False
            original = self.manager.path.read_text(encoding="utf-8")
            content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
            self._validate_content(content)
            self._replace(content)
            if not self.manager.reload_now():
                error = self.manager.last_error or "configuration reload failed"
                self._replace(original)
                if not self.manager.reload_now():
                    raise RuntimeError("configuration rollback failed")
                raise RuntimeError(error)
            return True

    def _read_mapping(self) -> dict[str, Any]:
        data = yaml.safe_load(self.manager.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("configuration root must be a mapping")
        return data

    def _validate_content(self, content: str) -> None:
        path = self.manager.path
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".validate",
            delete=False,
        )
        validation_path = Path(handle.name)
        try:
            with handle:
                handle.write(content)
            candidate = load_config(validation_path)
            self.manager.validate_reload_candidate(candidate)
        finally:
            validation_path.unlink(missing_ok=True)

    def _replace(self, content: str) -> None:
        path = self.manager.path
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
