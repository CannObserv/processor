"""The CI workflow's load-bearing choices (#12), pinned so they cannot drift quietly."""

import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
WIF_SA = "co-pypi-reader@co-gcs.iam.gserviceaccount.com"
# The broker's exact version (broker deploy/redis-acl.conf: "this broker is 7.0.15").
# Not 7.2: redis-py sends CLIENT SETINFO there, which the broker's ACL cannot grant
# until it upgrades, so a 7.2 container reports denials the broker never sees.
# 7.0 is upstream EOL: broker#85 decides the next version, and this pin moves with it.
# Docker Hub's official image via Google's mirror (#43): see test_image_comes_from_the_mirror.
BROKER_REDIS = "mirror.gcr.io/library/redis:7.0.15"
MIRROR = "mirror.gcr.io/"
# Node 24 majors: setup-uv@v5 and auth@v2 target Node 20, which GitHub deprecated
# (run 36962791709 warned it was forcing them onto Node 24).
ACTIONS = {
    "actions/checkout": "v5",
    "astral-sh/setup-uv": "v7",
    "google-github-actions/auth": "v3",
}


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


@pytest.mark.parametrize("name", ["lint", "test"])
def test_actions_are_on_node_24_majors(ci: dict, name: str) -> None:
    for step in _steps(ci["jobs"][name]):
        if "uses" in step:
            action, version = step["uses"].split("@")
            assert ACTIONS[action] == version, step["uses"]


@pytest.mark.parametrize("name", ["lint", "test"])
def test_jobs_are_time_bounded(ci: dict, name: str) -> None:
    # The integration suite blocks on Redis reads: a hang must not bill GitHub's 360.
    assert 0 < ci["jobs"][name]["timeout-minutes"] <= 15


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


def _image(spec: object) -> str | None:
    """The image a ``services.<id>`` or ``container`` value names."""
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        return spec.get("image")
    return None


def _images(workflows: Path = WORKFLOW.parent) -> list[tuple[str, str]]:
    """``(workflow:job[:service|:step n], image)`` for every container image a job pulls."""
    found = []
    for path in sorted(workflows.glob("*.y*ml")):
        jobs = yaml.safe_load(path.read_text()).get("jobs", {})
        for name, job in jobs.items():
            if (image := _image(job.get("container"))) is not None:
                found.append((f"{path.name}:{name}", image))
            for service, spec in (job.get("services") or {}).items():
                if (image := _image(spec)) is not None:
                    found.append((f"{path.name}:{name}:{service}", image))
            for n, step in enumerate(job.get("steps") or [], start=1):
                if (uses := step.get("uses", "")).startswith("docker://"):
                    found.append((f"{path.name}:{name}:step {n}", uses.removeprefix("docker://")))
    return found


IMAGES = _images()


def test_the_image_sweep_finds_docker_step_images(tmp_path: Path) -> None:
    # `uses: docker://<image>` runs a step in a container: a pull like a service's.
    (tmp_path / "x.yml").write_text(
        "jobs:\n  j:\n    steps:\n      - uses: actions/checkout@v5\n"
        "      - uses: docker://redis:7\n"
    )
    assert _images(tmp_path) == [("x.yml:j:step 2", "redis:7")]


def test_the_image_sweep_finds_the_redis_service() -> None:
    # Guard the guard: a sweep that finds nothing passes vacuously.
    assert "ci.yml:test:redis" in {site for site, _ in IMAGES}


@pytest.mark.parametrize(("site", "image"), IMAGES, ids=[s for s, _ in IMAGES])
def test_image_comes_from_the_mirror(site: str, image: str) -> None:
    # An image named without a registry is an anonymous Docker Hub pull, whose limit
    # GitHub's shared runners exhaust: on 2026-10-09 the redis service failed to pull
    # twice, and since #34 a red main run blocks scripts/deploy.sh (#43). The mirror
    # serves the same official images (same index digest) with no limit or credential.
    assert image.startswith(MIRROR), (
        f"{site} pulls {image!r}, which is not on {MIRROR}: an image with no registry, "
        f"or on docker.io, is an anonymous Docker Hub pull. "
        f"Use {MIRROR}library/<image> for an official image."
    )


# The one spelling of the sync (#79, as broker#113). `--only-group` installs uv.lock's
# wheelhouse group without the project, whose co-core is what the sync fetches.
# `--no-config` stands in for broker's tracked .wheelhouse/.gitkeep: without it uv
# reads [tool.uv] find-links, and a fresh checkout has no ./.wheelhouse yet.
LOCKED_SYNC = "--frozen --no-config --only-group wheelhouse"


def _sync_commands(text: str) -> list[str]:
    """The flags of each `uv run … python scripts/sync_wheelhouse.py`, `\\`-continued
    lines joined."""
    logical = re.sub(r"\s*\\+\n\s*", " ", text)
    return re.findall(r"uv run (.*?) python scripts/sync_wheelhouse\.py", logical)


def test_sync_commands_join_continued_lines() -> None:
    doc = "    uv run --no-project --with 'x' \\\\\n        python scripts/sync_wheelhouse.py\n"
    assert _sync_commands(doc) == ["--no-project --with 'x'"]


def test_every_spelling_of_the_sync_runs_from_the_lock() -> None:
    """#79: `--no-project --with` resolved the sync's env afresh from PyPI on every
    run, under the reader credential, outside uv.lock and the audit. Every tracked
    file outside tests/ that spells the sync spells the locked form."""
    root = WORKFLOW.parent.parent.parent
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    spellings: dict[str, set[str]] = {}
    for name in tracked:
        path = root / name
        if name.startswith("tests/") or path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for flags in _sync_commands(text):
            spellings.setdefault(name, set()).add(flags)
    assert set(spellings) >= {".github/workflows/ci.yml", "scripts/sync_wheelhouse.py", "AGENTS.md"}
    assert {n: s for n, s in spellings.items() if s != {LOCKED_SYNC}} == {}


def test_the_wheelhouse_group_is_locked_and_needs_no_wheelhouse() -> None:
    """The group installs before .wheelhouse exists: nothing in it may come from there."""
    root = WORKFLOW.parent.parent.parent
    group = tomllib.loads((root / "pyproject.toml").read_text())["dependency-groups"]["wheelhouse"]
    assert group == ["google-cloud-storage>=3,<4"], "the project's own spec, one package"
    lock = tomllib.loads((root / "uv.lock").read_text())
    (project,) = [p for p in lock["package"] if p["name"] == "processor"]
    assert [d["name"] for d in project["dev-dependencies"]["wheelhouse"]] == [
        "google-cloud-storage"
    ]
    from_wheelhouse = {
        p["name"] for p in lock["package"] if "wheelhouse" in p["source"].get("registry", "")
    }
    by_name = {p["name"]: p for p in lock["package"]}
    closure, todo = set(), ["google-cloud-storage"]
    while todo:
        name = todo.pop()
        if name not in closure:
            closure.add(name)
            todo += [d["name"] for d in by_name[name].get("dependencies", [])]
    assert from_wheelhouse and not closure & from_wheelhouse
