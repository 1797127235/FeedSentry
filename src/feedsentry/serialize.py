from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


def serialize_public(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {key: serialize_public(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): serialize_public(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_public(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value
