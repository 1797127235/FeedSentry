import json
import logging

from feedsentry.logging import JsonFormatter


def test_json_formatter_redacts_secrets() -> None:
    record = logging.LogRecord(
        "feedsentry", logging.INFO, __file__, 1, "request api_key=abc token=xyz", (), None
    )

    payload = json.loads(JsonFormatter().format(record))

    assert "abc" not in payload["message"]
    assert "xyz" not in payload["message"]


def test_json_formatter_includes_source_id() -> None:
    record = logging.LogRecord("feedsentry", logging.INFO, __file__, 1, "poll failed", (), None)
    record.source_id = "example"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["source_id"] == "example"
