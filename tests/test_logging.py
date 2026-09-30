"""Structured JSON logs with the cohort's four-key floor."""

import json
import logging
import re
import sys

from processor.logging import JsonFormatter

TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def _format(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def _record(msg: str = "hello", **extra) -> logging.LogRecord:
    record = logging.LogRecord("processor.consumer", logging.WARNING, __file__, 1, msg, (), None)
    record.__dict__.update(extra)
    return record


def test_four_key_floor() -> None:
    out = _format(_record())
    assert TIMESTAMP.match(out["timestamp"])
    assert (out["level"], out["logger"], out["message"]) == (
        "WARNING",
        "processor.consumer",
        "hello",
    )


def test_extras_ride_along_and_unserializable_values_stringify() -> None:
    out = _format(_record(command_id="cmd-1", total_ms=12.5, obj=object()))
    assert (out["command_id"], out["total_ms"]) == ("cmd-1", 12.5)
    assert out["obj"].startswith("<object object")


def test_exceptions_are_captured() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    out = _format(record)
    assert "ValueError: boom" in out["exc_info"]
