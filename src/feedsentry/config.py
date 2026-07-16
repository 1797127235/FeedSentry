from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")
SECRET_KEYS = {"api_key", "password", "token", "secret"}


class FirecrawlConfig(BaseModel):
    base_url: HttpUrl
    api_key: str | None = None


class AppriseConfig(BaseModel):
    base_url: HttpUrl


class TelegramConfig(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)
    bot_token: str
    chat_id: str


class QQConfig(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)
    base_url: HttpUrl
    access_token: str | None = None
    target_type: Literal["private", "group"]
    target_id: str


class IntegrationsConfig(BaseModel):
    firecrawl: FirecrawlConfig
    apprise: AppriseConfig
    rsshub: RSSHubConfig | None = None
    telegram: TelegramConfig | None = None
    qq: QQConfig | None = None


class AIConfig(BaseModel):
    base_url: HttpUrl
    api_key: str
    model: str = Field(min_length=1)


class StorageConfig(BaseModel):
    path: Path


class DestinationConfig(BaseModel):
    kind: Literal["apprise", "telegram", "qq"] = "apprise"
    apprise_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]+$")

    @model_validator(mode="after")
    def validate_kind_fields(self) -> DestinationConfig:
        if self.kind == "apprise" and self.apprise_key is None:
            raise ValueError("apprise destinations require apprise_key")
        if self.kind in {"telegram", "qq"} and "apprise_key" in self.model_fields_set:
            raise ValueError(f"{self.kind} destinations must not include apprise_key")
        return self


class FilterConfig(BaseModel):
    goal: str = Field(min_length=1)


class RSSHubConfig(BaseModel):
    base_url: HttpUrl


class SourceBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    enabled: bool = True


class DirectSourceConfig(SourceBase):
    kind: Literal["feed"]
    url: HttpUrl

    def feed_url(self, rsshub: RSSHubConfig | None) -> str:
        del rsshub
        return str(self.url)


class RSSHubSourceConfig(SourceBase):
    kind: Literal["rsshub"]
    page_url: HttpUrl
    route: str = Field(pattern=r"^/[^?#]*$")

    def feed_url(self, rsshub: RSSHubConfig | None) -> str:
        if rsshub is None:
            raise ValueError("RSSHub source requires integrations.rsshub")
        base = urlsplit(str(rsshub.base_url))
        path = posixpath.join(base.path.rstrip("/"), self.route.lstrip("/"))
        return urlunsplit((base.scheme, base.netloc, f"/{path.lstrip('/')}", "", ""))


SourceConfig = Annotated[
    DirectSourceConfig | RSSHubSourceConfig,
    Field(discriminator="kind"),
]


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    integrations: IntegrationsConfig
    ai: AIConfig
    storage: StorageConfig
    filter: FilterConfig
    sources: list[SourceConfig]
    destination: DestinationConfig

    @model_validator(mode="after")
    def validate_pipeline(self) -> AppConfig:
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source IDs must be unique")
        if any(source.kind == "rsshub" for source in self.sources):
            if self.integrations.rsshub is None:
                raise ValueError("RSSHub sources require integrations.rsshub")
        urls = [source.feed_url(self.integrations.rsshub) for source in self.sources]
        if len(urls) != len(set(urls)):
            raise ValueError("source URLs must be unique")
        if self.destination.kind == "telegram" and self.integrations.telegram is None:
            raise ValueError("telegram destinations require integrations.telegram")
        if self.destination.kind == "qq" and self.integrations.qq is None:
            raise ValueError("qq destinations require integrations.qq")
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
