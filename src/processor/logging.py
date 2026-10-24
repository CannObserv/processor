"""Structured JSON logs to stderr (journald), one record per line.

The cohort's four-key floor — ``timestamp`` (ISO 8601 UTC, microseconds, ``Z``),
``level``, ``logger``, ``message`` — the fields Observo adopted in observo#395/#407,
so a later observability plane ingests them unchanged. ``extra=`` fields ride
alongside and never overwrite the floor. ``warnings`` become records too, so stderr
carries nothing but JSON lines. Entry points call ``configure_logging`` once.

When stderr is the journal, each line also carries its level as an sd-daemon
``<N>`` prefix, so ``journalctl -p warning`` finds WARNING and ERROR (#76).
journald strips it (``SyslogLevelPrefix=yes``, the default): ``-o cat`` and
``MESSAGE`` stay pure JSON. Anywhere else (a terminal, ``systemd-run --pipe``, a
test's capture) there is no prefix.
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import TextIO

_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


# syslog priorities by floor, as Python filters levels; the standard levels map as
# python-systemd's JournalHandler maps them.
_PRIORITIES = ((logging.CRITICAL, 2), (logging.ERROR, 3), (logging.WARNING, 4), (logging.INFO, 6))


def _priority(levelno: int) -> int:
    """The syslog priority of a level: crit 2, err 3, warning 4, info 6, else debug 7."""
    return next((p for floor, p in _PRIORITIES if levelno >= floor), 7)


def _is_journal(stream: TextIO) -> bool:
    """Whether ``stream`` is the journal stream systemd named in ``JOURNAL_STREAM``.

    systemd sets ``JOURNAL_STREAM=<dev>:<ino>`` for a unit whose stdout or stderr is
    the journal; children inherit it whatever their stderr is, so only a match with
    the stream's own descriptor counts. Unset, malformed, no descriptor: not the
    journal. ``JOURNAL_STREAM`` is systemd's, not a setting, and this runs before
    ``Settings`` exists, so it is read from ``os.environ`` directly.
    """
    try:
        dev, ino = (int(part) for part in os.environ["JOURNAL_STREAM"].split(":"))
        st = os.fstat(stream.fileno())
    except (KeyError, ValueError, AttributeError, OSError):
        return False  # io.UnsupportedOperation is an OSError
    return (st.st_dev, st.st_ino) == (dev, ino)


class JsonFormatter(logging.Formatter):
    """A ``LogRecord`` as one JSON object; ``journal=True`` prefixes its ``<N>`` priority."""

    def __init__(self, *, journal: bool = False) -> None:
        super().__init__()
        self.journal = journal

    def format(self, record: logging.LogRecord) -> str:
        line = self._json(record)
        return f"<{_priority(record.levelno)}>{line}" if self.journal else line

    def _json(self, record: logging.LogRecord) -> str:
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
    """Route every logger, and ``warnings``, through one JSON handler on stderr.

    The ``<N>`` prefix is decided here, once, from the handler's own stream.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(journal=_is_journal(handler.stream)))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.captureWarnings(True)
