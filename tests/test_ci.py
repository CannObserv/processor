"""The CI workflow's load-bearing choices (#12), pinned so they cannot drift quietly."""

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
WIF_SA = "co-pypi-reader@co-gcs.iam.gserviceaccount.com"
BROKER_REDIS = "redis:7.2"  # the broker's version (broker docs/RESTART-WINDOW.md)


@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps(job: dict) -> list[dict]:
    return job["steps"]


def _runs(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in _steps(job))


def test_runs_on_pushes_and_prs_to_main(ci: dict) -> None:
    on = ci[True]  # YAML 1.1 reads the bare key `on` as a boolean
    assert on["push"]["branches"] == ["main"]
    assert on["pull_request"]["branches"] == ["main"]


def test_cloud_access_is_job_scoped(ci: dict) -> None:
    assert ci["permissions"] == {"contents": "read"}
    for name, job in ci["jobs"].items():
        assert job["permissions"] == {"contents": "read", "id-token": "write"}, name


@pytest.mark.parametrize("name", ["lint", "test"])
def test_wheelhouse_comes_keyless_from_the_read_only_identity(ci: dict, name: str) -> None:
    job = ci["jobs"][name]
    (auth,) = [s for s in _steps(job) if s.get("uses", "").startswith("google-github-actions/auth")]
    assert auth["with"]["service_account"] == WIF_SA
    assert auth["with"]["workload_identity_provider"] == "${{ vars.GCP_WIF_PROVIDER }}"
    runs = _runs(job)
    assert runs.index("scripts/sync_wheelhouse.py") < runs.index("uv sync")


@pytest.mark.parametrize("name", ["lint", "test"])
def test_checkout_brings_the_skill_submodules(ci: dict, name: str) -> None:
    (checkout,) = [
        s for s in _steps(ci["jobs"][name]) if s.get("uses", "").startswith("actions/checkout")
    ]
    assert checkout["with"]["submodules"] is True


def test_lint_gates(ci: dict) -> None:
    runs = _runs(ci["jobs"]["lint"])
    for gate in ("uv run ruff check .", "uv run ruff format --check .", "uv lock --locked"):
        assert gate in runs


def test_the_whole_suite_runs_against_the_brokers_redis(ci: dict) -> None:
    job = ci["jobs"]["test"]
    redis = job["services"]["redis"]
    assert redis["image"] == BROKER_REDIS
    assert "6379:6379" in redis["ports"]
    (pytest_run,) = [line for line in _runs(job).splitlines() if "pytest" in line]
    assert " -m " not in pytest_run, "a marker filter would drop the integration tests"
