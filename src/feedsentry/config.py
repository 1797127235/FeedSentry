from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")
SECRET_KEYS = {"api_key", "password", "token", "secret"}


def parse_duration(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", value.strip())
    if not match:
        raise ValueError("interval must use s,m,h,d (example 10m)")

    amount = int(match.group(1))
    unit = match.group(2)
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


class FirecrawlConfig(BaseModel):
    base_url: HttpUrl
    api_key: str | None = None


class AppriseConfig(BaseModel):
    base_url: HttpUrl


class IntegrationsConfig(BaseModel):
    firecrawl: FirecrawlConfig
    apprise: AppriseConfig


class AIConfig(BaseModel):
    base_url: HttpUrl
    api_key: str
    model: str = Field(min_length=1)


class StorageConfig(BaseModel):
    path: Path


class DestinationConfig(BaseModel):
    apprise_key: str = Field(pattern=r"^[A-Za-z0-9._-]+$")


class MonitorConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    interval: str
    sources: list[HttpUrl] = Field(min_length=1)
    destination: DestinationConfig
    enabled: bool = True

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, value: str) -> str:
        parse_duration(value)
        return value

    @property
    def interval_seconds(self) -> int:
        return parse_duration(self.interval)


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    integrations: IntegrationsConfig
    ai: AIConfig
    storage: StorageConfig
    monitors: list[MonitorConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_monitor_ids(self) -> AppConfig:
        ids = [monitor.id for monitor in self.monitors]
        if len(ids) != len(set(ids)):
            raise ValueError("monitor ids must be unique")
        return self


def _expand_environment(content: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name, default = match.groups()
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ValueError(f"missing environment variable: {name}")

    return ENV_PATTERN.sub(replace, content)


def load_config(path: Path | str) -> AppConfig:
    content = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(_expand_environment(content))
    if not isinstance(data, Mapping):
        raise ValueError("configuration root must be a mapping")
    return AppConfig.model_validate(data)


def redact_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in SECRET_KEYS:
                redacted[key] = "***"
            else:
                redacted[key] = redact_mapping(item)
        return redacted
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    return value


class ConfigManager:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.current: AppConfig | None = None
        self.last_error: str | None = None
        self.mtime: int | None = None

    def load_initial(self) -> AppConfig:
        config = load_config(self.path)
        self.current = config
        self.last_error = None
        self.mtime = self.path.stat().st_mtime_ns
        return config

    def reload_if_changed(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            self.last_error = "configuration reload failed"
            return False

        if mtime == self.mtime:
            return False

        try:
            config = load_config(self.path)
        except Exception:
            self.last_error = "configuration reload failed"
            self.mtime = mtime
            return False

        self.current = config
        self.last_error = None
        self.mtime = mtime
        return True
