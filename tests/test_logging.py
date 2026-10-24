"""Structured JSON logs with the cohort's four-key floor."""

import json
import os
import logging
import re
import sys
import warnings

import pytest

from processor.logging import JsonFormatter, configure_logging

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


def test_extras_never_overwrite_the_floor() -> None:
    out = _format(_record(level="DEBUG", logger="spoof", timestamp="then", message="other"))
    assert (out["level"], out["logger"], out["message"]) == (
        "WARNING",
        "processor.consumer",
        "hello",
    )
    assert TIMESTAMP.match(out["timestamp"])


def test_stack_info_is_kept() -> None:
    record = _record()
    record.stack_info = "Stack (most recent call last):\n  File x"
    assert _format(record)["stack_info"].startswith("Stack (most recent call last)")


def test_warnings_are_json_records(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        configure_logging()
        warnings.warn("a library's warning", UserWarning, stacklevel=1)
    finally:
        logging.captureWarnings(False)
        root.handlers[:] = handlers
        root.setLevel(level)
    (line,) = capsys.readouterr().err.splitlines()
    record = json.loads(line)
    assert (record["logger"], record["level"]) == ("py.warnings", "WARNING")
    assert "a library's warning" in record["message"]


@pytest.fixture
def root_logger():
    """Restore the root logger and ``warnings`` capture after ``configure_logging``."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    logging.captureWarnings(False)
    root.handlers[:] = handlers
    root.setLevel(level)


def _dev_ino(stream) -> str:
    st = os.fstat(stream.fileno())
    return f"{st.st_dev}:{st.st_ino}"


@pytest.mark.parametrize(
    ("levelno", "prefix"),
    [
        (logging.CRITICAL, "<2>"),
        (logging.ERROR, "<3>"),
        (logging.WARNING + 5, "<4>"),
        (logging.WARNING, "<4>"),
        (logging.INFO, "<6>"),
        (logging.DEBUG, "<7>"),
        (logging.NOTSET, "<7>"),
    ],
)
def test_each_level_maps_to_its_syslog_priority(levelno: int, prefix: str) -> None:
    record = logging.LogRecord("x", levelno, __file__, 1, "m", (), None)
    line = JsonFormatter(journal=True).format(record)
    assert line.startswith(prefix)
    assert json.loads(line.removeprefix(prefix))["message"] == "m"


def test_on_the_journal_every_line_carries_its_priority(
    root_logger: logging.Logger, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unit's case: JOURNAL_STREAM names stderr's own device and inode.
    with open(tmp_path / "stderr", "w+") as stream:
        monkeypatch.setattr(sys, "stderr", stream)
        monkeypatch.setenv("JOURNAL_STREAM", _dev_ino(stream))
        configure_logging()
        logging.getLogger("processor.consumer").warning("backing off", extra={"attempt": 2})
        logging.getLogger("processor.consumer").error("gave up")
        logging.getLogger("processor.consumer").info("ack")
        stream.flush()
        lines = (tmp_path / "stderr").read_text().splitlines()
    assert [line[:3] for line in lines] == ["<4>", "<3>", "<6>"]
    record = json.loads(lines[0][3:])
    assert (record["level"], record["message"], record["attempt"]) == (
        "WARNING",
        "backing off",
        2,
    )


@pytest.mark.parametrize("journal_stream", [None, "", "garbage", "1:2:3", "0:0", "other"])
def test_off_the_journal_lines_are_pure_json(
    root_logger: logging.Logger, tmp_path, monkeypatch: pytest.MonkeyPatch, journal_stream
) -> None:
    # A terminal, a `systemd-run --pipe`, a shell that inherited another unit's
    # JOURNAL_STREAM: unset, malformed, or naming a stream that is not stderr.
    with open(tmp_path / "stderr", "w+") as stream:
        monkeypatch.setattr(sys, "stderr", stream)
        if journal_stream is None:
            monkeypatch.delenv("JOURNAL_STREAM", raising=False)
        elif journal_stream == "other":
            with open(tmp_path / "elsewhere", "w") as other:
                monkeypatch.setenv("JOURNAL_STREAM", _dev_ino(other))
        else:
            monkeypatch.setenv("JOURNAL_STREAM", journal_stream)
        configure_logging()
        logging.getLogger("x").warning("w")
        stream.flush()
        (line,) = (tmp_path / "stderr").read_text().splitlines()
    assert json.loads(line)["level"] == "WARNING"


def test_a_stream_without_a_descriptor_is_not_the_journal(
    root_logger: logging.Logger, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    # pytest's capsys: sys.stderr.fileno() raises. Never a prefix, never an error.
    monkeypatch.setenv("JOURNAL_STREAM", "9:12345")
    configure_logging()
    logging.getLogger("x").warning("w")
    (line,) = capsys.readouterr().err.splitlines()
    assert json.loads(line)["level"] == "WARNING"
