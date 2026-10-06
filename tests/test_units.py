"""``deploy/``: the unit runs a release as its own user, and every file is accounted for (#2).

The cohort's release standard (broker#22; CannObserv/status#9's R1, R5, R11) and
#2's containment, held on the tracked copy. ``tests/test_deploy.py`` covers how the
deploy installs it.
"""

from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SERVICE = DEPLOY / "processor.service"


def _directives(path: Path) -> list[tuple[str, str]]:
    return [
        tuple(line.strip().split("=", 1))
        for line in path.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]


def _one(key: str) -> str:
    (value,) = [v for k, v in _directives(SERVICE) if k == key]
    return value


def test_no_unit_reads_or_runs_a_home_directory() -> None:
    # The service user cannot read /home/exedev (0750), and ProtectHome hides it.
    for key, value in _directives(SERVICE):
        assert "/home" not in value, f"{key}={value}"


def test_no_unit_syncs_or_runs_uv() -> None:
    # Syncing is a build step, never a start (R5). The venv's entry point needs no uv.
    assert all("uv " not in v and not v.startswith("uv") for _, v in _directives(SERVICE))


def test_the_unit_runs_the_live_release_as_its_own_user() -> None:
    assert (_one("User"), _one("Group")) == ("processor", "processor")
    assert _one("WorkingDirectory") == "/srv/processor/live"
    assert _one("ExecStart") == "/srv/processor/live/.venv/bin/processor run"


def test_secrets_come_from_etc_processor_and_the_key_as_a_credential() -> None:
    assert _one("EnvironmentFile") == "/etc/processor/.env"
    name, _, source = _one("LoadCredential").partition(":")
    assert source == "/etc/processor/co-gcs-processor-writer.json"
    assert _one("Environment") == f"GOOGLE_APPLICATION_CREDENTIALS=%d/{name}"


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


# What deploy.sh does with each file under deploy/: installs a unit, compares a host
# config (HOST_CONFIGS in scripts/deploy.sh), or nothing (a script run by hand).
UNITS = {"processor.service"}
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
