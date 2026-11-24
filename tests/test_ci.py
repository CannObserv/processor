"""The CI workflow's load-bearing choices (#12), pinned so they cannot drift quietly."""

import re
import socket
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
AUDIT_WORKFLOW = WORKFLOW.parent / "audit.yml"
# co-processor's image: every job runs on it, not ubuntu-latest (#81, as broker#88).
NODE = "co-processor"
NODE_IMAGE = "ubuntu-24.04"
OS_RELEASE = Path("/etc/os-release")
# Each action's floor major: what #80 pinned (broker's majors, operator 2026-10-29).
# A floor, not an exact major, so a Dependabot major PR passes and a downgrade fails.
# Below them lie Node 20 majors: setup-uv@v5 and auth@v2 target Node 20, which GitHub
# deprecated (run 36962791709 warned it was forcing them onto Node 24).
ACTIONS = {
    "actions/checkout": 7,
    "astral-sh/setup-uv": 10,
    "google-github-actions/auth": 3,
}
# YAML drops comments, so the pin's `# vX.Y.Z` is read from the text.
PIN = re.compile(r"^[ \t]+(?:- )?uses: ([^@\s]+)@\S+ # v(\d+)\.\d+\.\d+$", re.MULTILINE)
# A `uses:` key, not the word in a comment.
USES_KEY = re.compile(r"^[ \t]+(?:- )?uses:", re.MULTILINE)


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
    """A job's ``permissions`` replace the workflow's, so ``statuses: read`` sits on
    ``test``, whose attest step reads the combined status (#147); at workflow level
    it would be ignored."""
    assert ci["permissions"] == {"contents": "read"}
    cloud = {"contents": "read", "id-token": "write"}
    assert {name: job["permissions"] for name, job in ci["jobs"].items()} == {
        "lint": cloud,
        "test": {**cloud, "statuses": "read"},
    }


PAIR = {
    "UV_INDEX_CO_PYPI_USERNAME": "oauth2accesstoken",
    "UV_INDEX_CO_PYPI_PASSWORD": "${{ steps.auth.outputs.access_token }}",
}


@pytest.mark.parametrize("name", ["lint", "test"])
def test_co_core_comes_keyless_from_the_read_only_identity(ci: dict, name: str) -> None:
    """#121: the WIF step mints an access token for co-pypi-reader, the index's
    reader; the action masks the output."""
    job = ci["jobs"][name]
    (auth,) = [s for s in _steps(job) if s.get("uses", "").startswith("google-github-actions/auth")]
    assert auth["id"] == "auth"
    assert auth["with"]["service_account"] == WIF_SA
    assert auth["with"]["workload_identity_provider"] == "${{ vars.GCP_WIF_PROVIDER }}"
    assert auth["with"]["token_format"] == "access_token"
    steps = _steps(job)
    assert steps.index(auth) < steps.index(_sync_step(job))


def _sync_step(job: dict) -> dict:
    (step,) = [s for s in _steps(job) if s.get("run", "").startswith("uv sync")]
    return step


@pytest.mark.parametrize("name", ["lint", "test"])
def test_only_the_sync_gets_the_token(ci: dict, name: str) -> None:
    """The one step that downloads co-core takes the pair; every later `uv run`
    finds the venv synced. `uv lock --locked` resolves from the lock alone
    (measured offline from an empty cache, #121), so it needs none."""
    job = ci["jobs"][name]
    assert _sync_step(job)["env"] == PAIR
    others = [s for s in _steps(job) if s is not _sync_step(job)]
    assert not [s for s in others if "UV_INDEX" in str(s)]
    assert "UV_INDEX" not in str(ci.get("env", {})) + str(job.get("env", {}))


def test_no_wheelhouse_step_is_left(ci: dict) -> None:
    assert "wheelhouse" not in WORKFLOW.read_text().lower()


@pytest.mark.parametrize("name", ["lint", "test"])
def test_checkout_brings_the_skill_submodules(ci: dict, name: str) -> None:
    (checkout,) = [
        s for s in _steps(ci["jobs"][name]) if s.get("uses", "").startswith("actions/checkout")
    ]
    assert checkout["with"]["submodules"] is True


@pytest.mark.parametrize("workflow", ["ci.yml", "audit.yml"])
def test_actions_are_at_or_above_their_floor(workflow: str) -> None:
    """The form itself (`@<sha> # vX.Y.Z` on every `uses:`) is test_dependabot.py's."""
    text = (WORKFLOW.parent / workflow).read_text()
    pins = PIN.findall(text)
    assert len(pins) == len(USES_KEY.findall(text)), "a `uses:` without its tag comment"
    for action, major in pins:
        assert action in ACTIONS, f"{action}: an action with no floor here"
        assert int(major) >= ACTIONS[action], f"{action} v{major} < v{ACTIONS[action]}"


def test_the_uses_count_skips_comments() -> None:
    text = "      # uses: is mentioned here\n      - uses: a/b@c # v1.0.0\n        uses: d/e@f\n"
    assert len(USES_KEY.findall(text)) == 2
    assert PIN.findall(text) == [("a/b", "1")]


@pytest.mark.parametrize("name", ["lint", "test"])
def test_ci_prunes_the_cache_it_saves(ci: dict, name: str) -> None:
    """The repo is public, and a fork's PR can restore a cache saved on `main`.
    Unpruned, that cache holds co-core from the private co-pypi index (#121).
    setup-uv v9 flipped `prune-cache` to false, so it is set, not defaulted (#80,
    as broker#88)."""
    (setup,) = [
        s for s in _steps(ci["jobs"][name]) if s.get("uses", "").startswith("astral-sh/setup-uv")
    ]
    assert setup["with"]["enable-cache"] is True
    assert setup["with"]["prune-cache"] is True


@pytest.mark.parametrize("name", ["lint", "test"])
def test_jobs_are_time_bounded(ci: dict, name: str) -> None:
    # The integration suite blocks on Redis reads: a hang must not bill GitHub's 360.
    assert 0 < ci["jobs"][name]["timeout-minutes"] <= 15


def _runners() -> dict[str, str]:
    """Each job's runs-on, by workflow and job, across ci.yml and audit.yml."""
    return {
        f"{path.name}:{name}": job["runs-on"]
        for path in (WORKFLOW, AUDIT_WORKFLOW)
        for name, job in yaml.safe_load(path.read_text())["jobs"].items()
    }


def test_every_job_runs_on_the_nodes_image() -> None:
    """Not ubuntu-latest, which moved to 26.04 under us (#81, as broker#88): the
    shell lint and the suite run on co-processor's userland, and the Redis
    fallback above is 24.04's apt redis-server."""
    runners = _runners()
    assert {"ci.yml:lint", "ci.yml:test", "audit.yml:audit"} <= set(runners)
    assert set(runners.values()) == {NODE_IMAGE}, runners


def _os_version_id(os_release: Path) -> str:
    found = re.search(r'^VERSION_ID="?([^"\n]+)"?$', os_release.read_text(), re.MULTILINE)
    assert found, f"{os_release} names no VERSION_ID"
    return found[1]


def test_the_node_is_on_that_image() -> None:
    """On co-processor, by hostname (never by the file, broker#79): a node
    upgrade moves NODE_IMAGE and every runs-on with it."""
    if socket.gethostname() != NODE:
        pytest.skip("not co-processor")
    assert f"ubuntu-{_os_version_id(OS_RELEASE)}" == NODE_IMAGE


def _outcome_of(test) -> BaseException | None:
    """The skip, fail or assertion ``test()`` ends in, or ``None`` if it returns.
    A skip is caught too, so "fails on the node" regressing to "skips" shows."""
    try:
        test()
    except (pytest.skip.Exception, pytest.fail.Exception, AssertionError) as outcome:
        return outcome
    return None


@pytest.mark.parametrize(("host", "version_id", "outcome"), [
    ("co-processor", "26.04", AssertionError),
    ("co-processor", "24.04", type(None)),
    ("elsewhere", "26.04", pytest.skip.Exception),
])  # fmt: skip
def test_the_node_check_fails_on_a_node_that_moved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, host: str, version_id: str, outcome: type
) -> None:
    os_release = tmp_path / "os-release"
    os_release.write_text(f'NAME="Ubuntu"\nVERSION_ID="{version_id}"\n')
    monkeypatch.setattr(f"{__name__}.OS_RELEASE", os_release)
    monkeypatch.setattr(socket, "gethostname", lambda: host)
    assert isinstance(_outcome_of(test_the_node_is_on_that_image), outcome)


def test_lint_gates(ci: dict) -> None:
    runs = _runs(ci["jobs"]["lint"])
    for gate in ("uv run ruff check .", "uv run ruff format --check .", "uv lock --locked"):
        assert gate in runs


def test_the_whole_suite_runs_against_the_brokers_redis(ci: dict) -> None:
    job = ci["jobs"]["test"]
    redis = job["services"]["redis"]
    assert redis["image"] == BROKER_REDIS
    assert "6379:6379" in redis["ports"]
    runs = [line for line in _runs(job).splitlines() if "pytest" in line]
    assert runs == [FULL_SUITE, INTEGRATION], "a marker filter would drop the integration tests"


# The local-gate attestation (#147; shipping-work's references/attestation.md).
# CannObserv/status's shape: the attest step reads only on a PR, and the full suite
# skips at step level, so `test` stays `success` and every push to main runs it all:
# deploy.sh's gate is unchanged. The local gate deselects the integration tier, so an
# attested PR still runs that, on the inverse condition.
FULL_SUITE = "uv run pytest"
INTEGRATION = "uv run pytest -m integration"
REPO = "CannObserv/processor"


def _step_running(job: dict, command: str) -> dict:
    (step,) = [s for s in _steps(job) if s.get("run", "").strip() == command]
    return step


def _attest(ci: dict) -> dict:
    (step,) = [s for s in _steps(ci["jobs"]["test"]) if s.get("id") == "attest"]
    return step


def test_the_attest_step_reads_the_heads_combined_status_on_a_pr_only(ci: dict) -> None:
    """Only on a PR. Processor merges by fast-forward, so main's push run is on the
    PR's head SHA, which carries its ``gate/local``: a read there would skip the full
    suite on the run deploy.sh reads. The combined status keeps each context's latest
    state; a page is 30 contexts unless asked for more."""
    step = _attest(ci)
    assert step["if"] == "github.event_name == 'pull_request'"
    assert step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "HEAD": "${{ github.event.pull_request.head.sha || github.sha }}",
    }
    run = step["run"]
    assert "commits/$HEAD/status?per_page=100" in run
    assert '.context == "gate/local" and .state == "success"' in run
    assert '"attested=$(' in run and '"$GITHUB_OUTPUT"' in run


def test_the_attest_step_comes_between_the_sync_and_the_suite(ci: dict) -> None:
    job = ci["jobs"]["test"]
    steps = _steps(job)
    order = [_sync_step(job), _attest(ci), _step_running(job, FULL_SUITE)]
    assert [steps.index(s) for s in order] == sorted(steps.index(s) for s in order)
    assert steps.index(_step_running(job, INTEGRATION)) > steps.index(_attest(ci))


# A minimal evaluator for GitHub's expression syntax: enough for these `if:`s.
# Literals, dotted context paths (missing ones are null, read as ''), ==, !=, !(…),
# &&, ||, parentheses and startsWith. GitHub compares strings case-insensitively; the
# values below are written in one case, so plain equality agrees. A step's implicit
# `success() &&` is not modelled: every earlier step is taken to have passed.
_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>\|\||&&|==|!=|!|\(|\)|,)|(?P<name>[A-Za-z_][\w.-]*))"
)
_PY = {
    "||": " or ",
    "&&": " and ",
    "!": " not ",
    "==": "==",
    "!=": "!=",
    "(": "(",
    ")": ")",
    ",": ",",
}
_LITERALS = {"true": "True", "false": "False", "null": "''"}


def _evaluate(expression: str, context: dict) -> bool:
    def lookup(path: str) -> object:
        value: object = context
        for key in path.split("."):
            value = value.get(key, "") if isinstance(value, dict) else ""
        return value

    def starts_with(text: object, prefix: object) -> bool:
        return str(text).lower().startswith(str(prefix).lower())

    out, pos, text = [], 0, expression.strip()
    while pos < len(text):
        found = _TOKEN.match(text, pos)
        assert found and found.end() > pos, f"cannot read {text[pos:]!r}"
        pos = found.end()
        if found["str"] is not None:
            out.append(repr(found["str"][1:-1].replace("''", "'")))
        elif found["op"] is not None:
            # Python's `not` binds looser than `==`, GitHub's `!` tighter: they agree
            # only on a parenthesised operand.
            bare = found["op"] == "!" and not text[pos:].lstrip().startswith("(")
            assert not bare, f"`!` is read only before `(`: {text!r}"
            out.append(_PY[found["op"]])
        elif found["name"] in _LITERALS:
            out.append(_LITERALS[found["name"]])
        elif found["name"] == "startsWith":
            out.append("_starts_with")
        else:
            out.append(f"_lookup({found['name']!r})")
    names = {"__builtins__": {}, "_lookup": lookup, "_starts_with": starts_with}
    return bool(eval("".join(out), names))  # noqa: S307 - our own workflow's text


@pytest.mark.parametrize(("expression", "expected"), [
    ("github.event_name == 'push'", True),
    ("github.event_name != 'push' || startsWith(github.ref, 'refs/tags/')", False),
    ("!(github.event_name == 'push') || steps.none.outputs.x != 'yes'", True),
    ("github.event.pull_request.user.type == 'Bot'", False),
    ("'it''s' == 'it''s' && true", True),
])  # fmt: skip
def test_the_evaluator_reads_githubs_syntax(expression: str, expected: bool) -> None:
    # Guard the guard: a missing path reads as null, never as an error.
    context = {"github": {"event_name": "push", "ref": "refs/heads/main"}, "steps": {}}
    assert _evaluate(expression, context) is expected


def test_the_evaluator_refuses_a_bare_not() -> None:
    """GitHub's ``!`` binds tighter than ``==``; Python's ``not`` looser. Only ``!(`` is
    read, so ``!a == b`` is refused rather than misread."""
    with pytest.raises(AssertionError, match="only before"):
        _evaluate("!github.event_name == 'push'", {"github": {"event_name": "push"}})


def _job_run(
    ci: dict,
    event: str,
    *,
    status: bool,
    ref: str = "refs/heads/main",
    bot: bool = False,
    fork: bool = False,
) -> tuple[bool, bool]:
    """Whether the full suite and the integration step run, for one trigger.

    ``status`` is whether the head carries ``gate/local`` = ``success``. The attest
    step's output exists only if that step ran, as on Actions."""
    github: dict = {"event_name": event, "ref": ref, "repository": REPO}
    if event == "pull_request":
        github["event"] = {
            "pull_request": {
                "user": {"type": "Bot" if bot else "User"},
                "head": {"repo": {"full_name": "someone/processor" if fork else REPO}},
            }
        }
    context = {"github": github, "steps": {}}
    if _evaluate(_attest(ci)["if"], context):
        attested = "yes" if status else "no"
        context["steps"] = {"attest": {"outputs": {"attested": attested}}}
    job = ci["jobs"]["test"]
    return tuple(
        _evaluate(_step_running(job, command).get("if", "true"), context)
        for command in (FULL_SUITE, INTEGRATION)
    )


# A tag push never fires ci.yml today (push.branches is [main]). Its rows hold the
# reference's tag clause, should a tag trigger ever be added.
TRIGGERS = pytest.mark.parametrize(("event", "ref", "bot", "fork"), [
    ("push", "refs/heads/main", False, False),
    ("push", "refs/tags/v1.0.0", False, False),
    ("workflow_dispatch", "refs/heads/main", False, False),
    ("pull_request", "refs/pull/1/merge", False, False),
    ("pull_request", "refs/pull/1/merge", True, False),
    ("pull_request", "refs/pull/1/merge", False, True),
])  # fmt: skip


@TRIGGERS
@pytest.mark.parametrize("status", [True, False])
def test_exactly_one_of_the_suite_and_the_integration_tier_runs(
    ci: dict, event: str, ref: str, bot: bool, fork: bool, status: bool
) -> None:
    full, integration = _job_run(ci, event, status=status, ref=ref, bot=bot, fork=fork)
    assert full != integration


@TRIGGERS
def test_the_full_suite_runs_on_every_unattested_trigger(
    ci: dict, event: str, ref: str, bot: bool, fork: bool
) -> None:
    assert _job_run(ci, event, status=False, ref=ref, bot=bot, fork=fork) == (True, False)


@pytest.mark.parametrize(("event", "ref", "bot", "fork"), [
    ("push", "refs/heads/main", False, False),  # main's run, which deploy.sh reads
    ("push", "refs/tags/v1.0.0", False, False),  # unreachable today: see TRIGGERS
    ("workflow_dispatch", "refs/heads/main", False, False),
    ("pull_request", "refs/pull/1/merge", True, False),  # Dependabot
    ("pull_request", "refs/pull/1/merge", False, True),  # a fork
])  # fmt: skip
def test_a_status_never_skips_the_suite_outside_a_humans_pr(
    ci: dict, event: str, ref: str, bot: bool, fork: bool
) -> None:
    """A bot's PR and a fork's never came from a local gate; a push to main reads no
    status at all, so its run is always the whole suite."""
    assert _job_run(ci, event, status=True, ref=ref, bot=bot, fork=fork) == (True, False)


def test_an_attested_humans_pr_runs_only_the_integration_tier(ci: dict) -> None:
    """The local gate ran the rest (pre-ship.sh deselects `integration`)."""
    assert _job_run(ci, "pull_request", status=True) == (False, True)


def test_the_integration_steps_condition_is_the_suites_negated(ci: dict) -> None:
    job = ci["jobs"]["test"]
    full, integration = (_step_running(job, c)["if"] for c in (FULL_SUITE, INTEGRATION))
    assert " ".join(integration.split()) == f"!({' '.join(full.split())})"


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
