"""``CO_PROCESSOR_*`` settings: defaults, secrecy, and the reclaim/timeout invariant."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from processor.settings import Settings

URL = "redis://processor:hunter2@100.97.91.19:6379/0"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("CO_PROCESSOR_"):
            monkeypatch.delenv(key)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
    s = Settings()
    assert s.consumer_name == "co-processor"
    assert (s.input_bucket, s.input_prefix) == ("co-gcs-blobs", "blobs")
    assert (s.output_bucket, s.output_prefix) == ("co-gcs-processor", "blobs")
    assert s.store_backend == "gcs"
    assert s.extraction_timeout_s == 120
    assert s.rlimit_as_bytes == 3 * 1024**3
    assert s.reclaim_min_idle_ms == 600_000
    assert s.reclaim_interval_s == 60
    assert s.max_attempts == 3


def test_env_prefix_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
    monkeypatch.setenv("CO_PROCESSOR_EXTRACTION_TIMEOUT_S", "30")
    monkeypatch.setenv("CO_PROCESSOR_RECLAIM_MIN_IDLE_MS", "120000")
    s = Settings()
    assert (s.extraction_timeout_s, s.reclaim_min_idle_ms) == (30, 120_000)


def test_bus_url_is_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
    s = Settings()
    assert "hunter2" not in repr(s) and "hunter2" not in str(s.model_dump())
    assert s.bus_url.get_secret_value() == URL


def test_bus_url_is_required() -> None:
    with pytest.raises(ValidationError):
        Settings()


def test_reclaim_must_outlast_a_slow_command(monkeypatch: pytest.MonkeyPatch) -> None:
    # A command still inside its timeout (plus store + publish) must not be reclaimed.
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
    monkeypatch.setenv("CO_PROCESSOR_EXTRACTION_TIMEOUT_S", "120")
    monkeypatch.setenv("CO_PROCESSOR_RECLAIM_MIN_IDLE_MS", "150000")
    with pytest.raises(ValidationError, match="reclaim_min_idle_ms"):
        Settings()


def test_local_backend_needs_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
    monkeypatch.setenv("CO_PROCESSOR_STORE_BACKEND", "local")
    with pytest.raises(ValidationError, match="local_input_root"):
        Settings()
    monkeypatch.setenv("CO_PROCESSOR_LOCAL_INPUT_ROOT", str(tmp_path / "in"))
    monkeypatch.setenv("CO_PROCESSOR_LOCAL_OUTPUT_ROOT", str(tmp_path / "out"))
    assert Settings().local_output_root == tmp_path / "out"
