"""``deploy/``: the units run a release as its own user, and every file is accounted for (#2).

The cohort's release standard (broker#22; CannObserv/status#9's R1, R5, R11) and
#2's containment, held on the tracked copy. ``tests/test_deploy.py`` covers how the
deploy installs it.
"""

from pathlib import Path

import pytest

from processor.settings import DriftSettings

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SERVICE = DEPLOY / "processor.service"
DRIFT = DEPLOY / "processor-drift.service"
DRIFT_TIMER = DEPLOY / "processor-drift.timer"
SERVICES = (SERVICE, DRIFT)


def _directives(path: Path) -> list[tuple[str, str]]:
    return [
        tuple(line.strip().split("=", 1))
        for line in path.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]


def _one(key: str, path: Path = SERVICE) -> str:
    (value,) = [v for k, v in _directives(path) if k == key]
    return value


def _all(key: str, path: Path) -> list[str]:
    return [v for k, v in _directives(path) if k == key]


@pytest.mark.parametrize("path", SERVICES, ids=lambda p: p.name)
def test_no_unit_reads_or_runs_a_home_directory(path: Path) -> None:
    # The service user cannot read /home/exedev (0750), and ProtectHome hides it.
    for key, value in _directives(path):
        assert "/home" not in value, f"{key}={value}"


@pytest.mark.parametrize("path", SERVICES, ids=lambda p: p.name)
def test_no_unit_syncs_or_runs_uv(path: Path) -> None:
    # Syncing is a build step, never a start (R5). The venv's entry point needs no uv.
    assert all("uv " not in v and not v.startswith("uv") for _, v in _directives(path))


def test_the_unit_runs_the_live_release_as_its_own_user() -> None:
    assert (_one("User"), _one("Group")) == ("processor", "processor")
    assert _one("WorkingDirectory") == "/srv/processor/live"
    assert _one("ExecStart") == "/srv/processor/live/.venv/bin/processor run"


def test_secrets_come_from_etc_processor_and_the_key_as_a_credential() -> None:
    assert _one("EnvironmentFile") == "/etc/processor/.env"
    credentials = dict(c.partition(":")[::2] for c in _all("LoadCredential", SERVICE))
    assert credentials["gcs-writer-key"] == "/etc/processor/co-gcs-processor-writer.json"
    assert "GOOGLE_APPLICATION_CREDENTIALS=%d/gcs-writer-key" in _all("Environment", SERVICE)


def test_the_status_key_is_a_credential_with_an_empty_fallback() -> None:
    # The liveness check-in's (#39): the drift check's key, the same file. A missing
    # file starts the service with liveness off, logged, never a failed start.
    credentials = dict(c.partition(":")[::2] for c in _all("LoadCredential", SERVICE))
    assert credentials["status-checkin-key"] == "/etc/processor/status-checkin.key"
    assert _all("SetCredential", SERVICE) == ["status-checkin-key:\\n"]  # as the drift unit


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NoNewPrivileges", "yes"),
        ("PrivateTmp", "yes"),
        ("ProtectSystem", "strict"),
        ("ProtectHome", "yes"),
        ("KillMode", "mixed"),
    ],
)
def test_hardening(key: str, value: str) -> None:
    assert _one(key) == value


class TestTheDriftUnits:
    """``processor-drift.{service,timer}`` (#35): hourly, apart from ``processor run``."""

    def test_a_oneshot_of_the_live_releases_entry_point_as_the_service_user(self) -> None:
        assert _one("Type", DRIFT) == "oneshot"
        assert (_one("User", DRIFT), _one("Group", DRIFT)) == ("processor", "processor")
        assert _one("WorkingDirectory", DRIFT) == "/srv/processor/live"
        assert _one("ExecStart", DRIFT) == "/srv/processor/live/.venv/bin/processor drift"

    def test_it_names_co_processor_drift(self) -> None:
        # The monitor's id, posted on CannObserv/status#24 (2026-10-07): not a secret.
        # DriftSettings must accept it, or `processor drift` exits 2 every hour.
        (env,) = _all("Environment", DRIFT)
        name, _, value = env.partition("=")
        assert (name, value) == ("CO_PROCESSOR_DRIFT_MONITOR_ID", "01M4BHMGGTXWQ16J3G5WWDQRYQ")
        assert DriftSettings(drift_monitor_id=value).drift_monitor_id == value

    def test_it_never_reads_the_broker_credential(self) -> None:
        # The env file holds CO_PROCESSOR_BUS_URL: a drift check has no use for it.
        assert _all("EnvironmentFile", DRIFT) == []

    def test_the_status_key_is_a_credential_with_an_empty_fallback(self) -> None:
        # A missing key file runs the check, which logs it and exits 1, rather than
        # failing the start with a less useful message (status's SetCredential=).
        name, _, source = _one("LoadCredential", DRIFT).partition(":")
        assert (name, source) == ("status-checkin-key", "/etc/processor/status-checkin.key")
        # A lone newline: systemd 255 refuses an empty value ("Invalid syntax") and
        # drops the line, and the start then fails on the missing file.
        assert _one("SetCredential", DRIFT) == "status-checkin-key:\\n"

    def test_bounded_and_never_restarted(self) -> None:
        # GitHub (60 s for every call) and one check-in (10 s), with margin. A failed run
        # waits for the next hour: no Restart=, no retry loop (#35 trap 4).
        assert _one("TimeoutStartSec", DRIFT) == "90"
        assert _all("Restart", DRIFT) == []

    def test_it_is_started_by_its_timer_alone(self) -> None:
        assert "[Install]" not in DRIFT.read_text()

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("NoNewPrivileges", "yes"),
            ("PrivateTmp", "yes"),
            ("ProtectSystem", "strict"),
            ("ProtectHome", "yes"),
        ],
    )
    def test_hardening(self, key: str, value: str) -> None:
        assert _one(key, DRIFT) == value

    def test_the_timer_runs_it_hourly_from_boot(self) -> None:
        # Status's monitor expects a check-in every 3600 s (CannObserv/status#24).
        assert _one("Unit", DRIFT_TIMER) == "processor-drift.service"
        assert _one("OnBootSec", DRIFT_TIMER) == "5min"
        assert _one("OnUnitActiveSec", DRIFT_TIMER) == "1h"
        assert _one("WantedBy", DRIFT_TIMER) == "timers.target"


# What deploy.sh does with each file under deploy/: installs a unit, compares a host
# config (HOST_CONFIGS in scripts/deploy.sh), or nothing (a script run by hand).
UNITS = {"processor.service", "processor-drift.service", "processor-drift.timer"}
HOST_CONFIGS = {
    "tailscaled.service.d/90-processor-oom.conf",
    "apt/nodesource.sources",
    "apt/nodesource.pref",
}
SCRIPTS = {"nodesource.sh"}


def test_every_file_under_deploy_is_accounted_for() -> None:
    tracked = {
        str(p.relative_to(DEPLOY))
        for p in DEPLOY.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    assert tracked == UNITS | HOST_CONFIGS | SCRIPTS


def test_deploy_sh_compares_every_host_config() -> None:
    script = (DEPLOY.parent / "scripts" / "deploy.sh").read_text()
    for rel in HOST_CONFIGS:
        assert f'"{rel}=' in script, rel


def test_the_install_never_globs_inside_etc_processor() -> None:
    # CR 12: the operator's shell expands a glob as exedev, after the chown has made
    # /etc/processor root's 700, so it matches nothing; and `*` never matches .env.
    runbook = (DEPLOY.parent / "docs" / "DEPLOYMENT.md").read_text()
    assert "/etc/processor/*" not in runbook
    assert (
        "sudo chmod 600 /etc/processor/.env /etc/processor/co-gcs-processor-writer.json" in runbook
    )
