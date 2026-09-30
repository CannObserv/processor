"""Structured JSON logs to stderr (journald), one record per line.

The cohort's four-key floor — ``timestamp`` (ISO 8601 UTC, microseconds, ``Z``),
``level``, ``logger``, ``message`` — the fields Observo adopted in observo#395/#407,
so a later observability plane ingests them unchanged. ``extra=`` fields ride
alongside and never overwrite the floor. ``warnings`` become records too, so stderr
carries nothing but JSON lines. Entry points call ``configure_logging`` once.
"""

import json
import logging
import sys
from datetime import UTC, datetime

_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """A ``LogRecord`` as one JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and key not in out and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(out, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Route every logger, and ``warnings``, through one JSON handler on stderr."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.captureWarnings(True)
