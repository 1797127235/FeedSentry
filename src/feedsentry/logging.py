from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

_SECRET_PATTERN = re.compile(r"(?i)\b(api_key|token|password|secret)=([^\s,;&]+)")
_CONTEXT_FIELDS = ("source_url", "entry_id", "event_id", "stage", "attempt")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = _SECRET_PATTERN.sub(r"\1=***", record.getMessage())[:4000]
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
