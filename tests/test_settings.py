"""``CO_PROCESSOR_*`` settings: defaults, secrecy, and the reclaim/timeout invariant."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from processor.settings import DriftSettings, Settings

URL = "redis://processor:hunter2@broker:6379/0"


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
    assert s.child_containment == "required"  # production fails closed (#2)


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


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("READ_BLOCK_MS", "0"),  # XREADGROUP BLOCK 0 blocks forever
        ("EXTRACTION_TIMEOUT_S", "0"),
        ("MAX_ATTEMPTS", "0"),
        ("RECLAIM_INTERVAL_S", "-1"),
        ("RLIMIT_AS_BYTES", "-1"),
    ],
)
def test_knobs_out_of_range_are_refused(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
    monkeypatch.setenv(f"CO_PROCESSOR_{name}", value)
    with pytest.raises(ValidationError, match=name.lower()):
        Settings()


def test_child_containment_is_required_or_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
    monkeypatch.setenv("CO_PROCESSOR_CHILD_CONTAINMENT", "off")
    assert Settings().child_containment == "off"
    monkeypatch.setenv("CO_PROCESSOR_CHILD_CONTAINMENT", "best_effort")
    with pytest.raises(ValidationError):
        Settings()


class TestLiveness:
    """``processor run``'s check-in to ``co-processor-live`` (#39)."""

    def test_off_by_default_every_five_minutes_bounded_at_ten_seconds(self, monkeypatch) -> None:
        monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
        s = Settings()
        assert s.live_monitor_id == ""  # off: dev and CI
        assert (s.live_interval_s, s.live_checkin_timeout_s) == (300, 10)
        assert s.status_url == "http://status:9000"
        assert s.credentials_directory is None

    def test_the_monitor_id_is_a_ulid(self, monkeypatch) -> None:
        monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
        monkeypatch.setenv("CO_PROCESSOR_LIVE_MONITOR_ID", "01M4BHMGGTXWQ16J3G5WWDQRYQ")
        assert Settings().live_monitor_id == "01M4BHMGGTXWQ16J3G5WWDQRYQ"
        monkeypatch.setenv("CO_PROCESSOR_LIVE_MONITOR_ID", "../../admin")
        with pytest.raises(ValidationError, match="live_monitor_id"):
            Settings()

    def test_the_credentials_directory_is_systemds(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        assert Settings().credentials_directory == tmp_path

    @pytest.mark.parametrize(("interval", "timeout"), [("10", "10"), ("10", "11")])
    def test_a_check_in_ends_before_the_next_tick(self, monkeypatch, interval, timeout) -> None:
        monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
        monkeypatch.setenv("CO_PROCESSOR_LIVE_INTERVAL_S", interval)
        monkeypatch.setenv("CO_PROCESSOR_LIVE_CHECKIN_TIMEOUT_S", timeout)
        with pytest.raises(ValidationError, match="live_checkin_timeout_s"):
            Settings()

    @pytest.mark.parametrize("name", ["LIVE_INTERVAL_S", "LIVE_CHECKIN_TIMEOUT_S"])
    def test_out_of_range_is_refused(self, monkeypatch, name) -> None:
        monkeypatch.setenv("CO_PROCESSOR_BUS_URL", URL)
        monkeypatch.setenv(f"CO_PROCESSOR_{name}", "0")
        with pytest.raises(ValidationError, match=name.lower()):
            Settings()

    def test_the_drift_check_still_needs_no_broker(self, tmp_path, monkeypatch) -> None:
        # Apart on purpose (#35): the drift unit reads no .env, so no bus URL.
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
        s = DriftSettings()
        assert (s.status_url, s.credentials_directory) == ("http://status:9000", tmp_path)
        assert not hasattr(s, "bus_url") and not hasattr(s, "live_monitor_id")
