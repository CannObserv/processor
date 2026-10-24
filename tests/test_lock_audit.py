"""The lock audit reports and never fails (#73, ported from CannObserv/broker#86).

``scripts/audit_lock.sh`` runs ``uv audit --frozen`` against ``uv.lock``, and
``.github/workflows/audit.yml`` runs it on every push to ``main``, every PR, once a
week and on dispatch. Nothing else reads the lock against OSV: #71's 12 advisories
surfaced only because someone ran ``uv audit`` by hand, and disclosures arrive
without commits.

**Report-only is the contract.** A gate that failed on a finding would turn
``main`` red on someone else's disclosure date. So every exit is 0: a finding is a
warning annotation plus the job summary, and an audit that could not run gets its
own title, so it never reads as clean. It is its own workflow, outside
``scripts/deploy.sh``'s CI gate, which reads ``ci.yml``'s run alone.

The script runs here against a fake ``uv`` on ``PATH``, so nothing reaches OSV.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "audit_lock.sh"
WORKFLOW = REPO / ".github" / "workflows" / "audit.yml"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
DEPLOY = REPO / "scripts" / "deploy.sh"
ROUTING_ANCHOR = "docs/DEPLOYMENT.md#extraction-dependencies"

# uv < 0.11.19 has no ``audit-command`` name (astral-sh/uv#19645): its parser
# rejects the value before the command runs.
UNKNOWN_FLAG = (
    "error: invalid value 'audit-command' for '--preview-features <PREVIEW_FEATURES>': "
    "Unknown feature flag\n"
)
FINDING = "- GHSA-gmh7-3g9r-mjh5: pypdf 6.19.0\n  Fixed in: 6.21.2\n"


def _run(
    tmp_path: Path,
    uv_exit: int,
    uv_output: str = "",
    *,
    actions: bool = True,
    knows_flag: bool = True,
    argv0: str | None = None,
):
    """Run the script with a fake ``uv`` that records each call's argv and cwd,
    prints ``uv_output`` and exits ``uv_exit``. With ``knows_flag=False`` it answers
    ``--preview-features`` as uv < 0.11.19 does, exit 2, and runs nothing.
    ``argv0`` runs the script's text under that ``$0`` instead of its path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "argv"
    (tmp_path / "output").write_text(uv_output)
    (tmp_path / "unknown").write_text(UNKNOWN_FLAG)
    reject = (
        ""
        if knows_flag
        else (
            'if [[ " $* " == *" --preview-features "* ]]; then\n'
            f'\tcat "{tmp_path / "unknown"}" >&2\n'
            "\texit 2\n"
            "fi\n"
        )
    )
    fake = bin_dir / "uv"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$PWD $*" >> "{argv_file}"\n'
        f"{reject}"
        f'cat "{tmp_path / "output"}"\n'
        f"exit {uv_exit}\n"
    )
    fake.chmod(0o755)
    summary = tmp_path / "summary.md"
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    if actions:
        env |= {
            "GITHUB_ACTIONS": "true",
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "CannObserv/processor",
        }
    argv = ["bash", str(SCRIPT)] if argv0 is None else ["bash", "-c", SCRIPT.read_text(), argv0]
    result = subprocess.run(argv, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    calls = (
        [line.split() for line in argv_file.read_text().splitlines()] if argv_file.exists() else []
    )
    return result, calls, summary.read_text() if summary.exists() else ""


# --- the script -------------------------------------------------------------


@pytest.mark.parametrize("uv_exit", [0, 1, 2, 127])
def test_every_exit_is_zero(tmp_path: Path, uv_exit: int) -> None:
    """Whatever uv says, the step stays green: report-only."""
    result, _, _ = _run(tmp_path, uv_exit)
    assert result.returncode == 0, result.stderr


def test_audits_the_lock_beside_it_without_relocking(tmp_path: Path) -> None:
    """``--frozen`` reads ``uv.lock`` as committed: a relock would need the private
    wheelhouse, which the job has no credential to fetch. It runs in the checkout
    the script sits in, whatever the caller's directory. ``uv audit`` is a preview
    command, so the flag goes first and a uv that knows it runs once."""
    _, calls, _ = _run(tmp_path, 0)
    assert calls == [[str(REPO), "audit", "--frozen", "--preview-features", "audit-command"]]


@pytest.mark.parametrize(
    ("uv_exit", "uv_output", "warning"),
    [
        (0, "Found no known vulnerabilities\n", None),
        (1, FINDING, "::warning title=uv.lock advisories::"),
    ],
)
def test_a_uv_older_than_the_flag_still_audits(
    tmp_path: Path, uv_exit: int, uv_output: str, warning: str | None
) -> None:
    """uv < 0.11.19 rejects ``audit-command`` with exit 2 and "Unknown feature flag"
    (broker#103), so the script retries without the flag. From 0.11.20 an unknown
    name only warns, so no later uv reaches this path. The rejected attempt never
    reaches the report."""
    result, calls, summary = _run(tmp_path, uv_exit, uv_output, knows_flag=False)
    assert [c[1:] for c in calls] == [
        ["audit", "--frozen", "--preview-features", "audit-command"],
        ["audit", "--frozen"],
    ]
    assert "did not run" not in result.stdout
    if warning:
        assert warning in result.stdout
    else:
        assert "::warning" not in result.stdout
    assert f"exit {uv_exit}" in summary
    assert uv_output.strip() in summary
    assert "Unknown feature flag" not in summary


def test_a_retry_that_fails_never_reads_as_clean(tmp_path: Path) -> None:
    """On an old uv the flagless call can still fail (an outage at OSV, say). The
    warning and the summary report that call, not the rejection."""
    result, calls, summary = _run(tmp_path, 2, "error: OSV unreachable\n", knows_flag=False)
    assert len(calls) == 2
    assert "::warning title=uv audit did not run::uv audit exited 2" in result.stdout
    assert "exit 2" in summary
    assert "OSV unreachable" in summary
    assert "Unknown feature flag" not in summary


def test_any_other_exit_2_is_not_retried(tmp_path: Path) -> None:
    """Only the unknown-flag rejection retries. An outage at OSV also exits 2, and a
    second call would only repeat it."""
    result, calls, _ = _run(tmp_path, 2, "error: OSV unreachable\n")
    assert len(calls) == 1
    assert "::warning title=uv audit did not run::" in result.stdout


def test_a_clean_lock_raises_no_warning(tmp_path: Path) -> None:
    result, _, summary = _run(tmp_path, 0, "Found no known vulnerabilities\n")
    assert "::warning" not in result.stdout
    assert "Found no known vulnerabilities" in summary


def test_a_finding_is_a_warning_and_lands_in_the_summary(tmp_path: Path) -> None:
    result, _, summary = _run(tmp_path, 1, FINDING)
    assert "::warning title=uv.lock advisories::" in result.stdout
    assert "GHSA-gmh7-3g9r-mjh5" in summary


def test_a_finding_says_where_it_goes(tmp_path: Path) -> None:
    """Processor's routing (#71): every finding is a re-lock plus a
    ``constraint-dependencies`` floor, so a later re-lock cannot undo it; an
    extraction-stack finding also bumps ``LOCAL_GENERATION`` (#71 did both for
    pypdf). The summary links the section that says so, and the annotation names
    it."""
    result, _, summary = _run(tmp_path, 1, FINDING)
    link = f"https://github.com/CannObserv/processor/blob/main/{ROUTING_ANCHOR}"
    assert link in summary
    for term in ("LOCAL_GENERATION", "constraint-dependencies", "test_lock_floors.py"):
        assert term in summary, term
    routing = summary[summary.index("Where a finding goes") :]
    every, stack = [line for line in routing.splitlines() if line.startswith("- ")]
    assert "every finding" in every and "constraint-dependencies" in every
    assert "LOCAL_GENERATION" in stack and "constraint-dependencies" not in stack
    (warning,) = [line for line in result.stdout.splitlines() if line.startswith("::warning")]
    assert "Extraction dependencies" in warning
    assert "floor" in warning and "also bumps LOCAL_GENERATION" in warning


def test_a_finding_off_actions_still_says_where_it_goes(tmp_path: Path) -> None:
    result, _, _ = _run(tmp_path, 1, FINDING, actions=False)
    assert ROUTING_ANCHOR in result.stdout


def test_a_clean_lock_carries_no_routing(tmp_path: Path) -> None:
    _, _, summary = _run(tmp_path, 0, "Found no known vulnerabilities\n")
    assert ROUTING_ANCHOR not in summary


@pytest.mark.parametrize("uv_exit", [2, 127])
def test_an_audit_that_did_not_run_never_reads_as_clean(tmp_path: Path, uv_exit: int) -> None:
    """uv exits 1 for findings and 2 for an error: an outage at OSV, or a changed
    flag on a preview command. A missing uv exits 127."""
    result, _, summary = _run(tmp_path, uv_exit)
    assert "::warning title=uv audit did not run::" in result.stdout
    assert f"exit {uv_exit}" in summary


def test_a_checkout_it_cannot_enter_is_not_audited(tmp_path: Path) -> None:
    """If the ``cd`` to the checkout fails it must not audit whatever lock the
    caller's directory holds, nor fail the step (shellcheck's SC2164)."""
    (tmp_path / "uv.lock").write_text("")
    result, calls, summary = _run(tmp_path, 0, argv0=str(tmp_path / "gone" / "scripts" / "x.sh"))
    assert result.returncode == 0, result.stderr
    assert calls == []
    assert "::warning title=uv audit did not run::" in result.stdout
    assert summary == ""


def test_uv_output_cannot_issue_workflow_commands(tmp_path: Path) -> None:
    """Advisory text comes from OSV. Echoed bare, a line starting ``::`` would be
    read by the runner as a workflow command, so it is echoed between
    ``stop-commands`` and a random resume token."""
    result, _, _ = _run(tmp_path, 1, "::error::forged\n")
    lines = result.stdout.splitlines()
    stop = next(i for i, line in enumerate(lines) if line.startswith("::stop-commands::"))
    token = lines[stop].removeprefix("::stop-commands::")
    assert len(token) >= 32
    resume = lines.index(f"::{token}::")
    assert stop < lines.index("::error::forged") < resume
    # The warning comes after the resume token, or the runner would ignore it.
    assert resume < next(i for i, line in enumerate(lines) if line.startswith("::warning"))


def test_the_resume_token_differs_per_run(tmp_path: Path) -> None:
    tokens = []
    for n in range(2):
        d = tmp_path / str(n)
        d.mkdir()
        result, _, _ = _run(d, 0)
        (line,) = [x for x in result.stdout.splitlines() if x.startswith("::stop-commands::")]
        tokens.append(line)
    assert tokens[0] != tokens[1]


def test_runs_off_actions_without_a_summary(tmp_path: Path) -> None:
    """Run by hand, it prints the audit and touches no summary file."""
    result, _, summary = _run(tmp_path, 1, "a finding\n", actions=False)
    assert result.returncode == 0
    assert "a finding" in result.stdout
    assert "stop-commands" not in result.stdout
    assert summary == ""


# --- the workflow -----------------------------------------------------------


@pytest.fixture(scope="module")
def audit() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_runs_on_push_pr_schedule_and_dispatch(audit: dict) -> None:
    """Push and PR runs read the lock a change ships; the weekly run reads the lock
    nobody touched, since disclosures arrive without commits."""
    on = audit[True]  # YAML 1.1 reads the bare key `on` as a boolean
    assert on["push"]["branches"] == ["main"]
    assert on["pull_request"]["branches"] == ["main"]
    assert on["schedule"] and all("cron" in s for s in on["schedule"])
    assert "workflow_dispatch" in on


def test_holds_no_secret_and_no_oidc(audit: dict) -> None:
    """``uv audit --frozen`` reads the lock without the wheelhouse, so the job needs
    no WIF to ``co-pypi-reader``, and gets nothing more than read."""
    text = WORKFLOW.read_text()
    assert audit["permissions"] == {"contents": "read"}
    for name, job in audit["jobs"].items():
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}, name
    assert "secrets." not in text
    assert "id-token" not in text
    assert "google-github-actions/auth" not in text
    assert "sync_wheelhouse" not in text


def test_runs_the_script_and_nothing_masks_it(audit: dict) -> None:
    (job,) = audit["jobs"].values()
    runs = [s["run"] for s in job["steps"] if "run" in s]
    assert runs == ["bash scripts/audit_lock.sh"]
    assert "continue-on-error" not in WORKFLOW.read_text(), (
        "report-only is the script's exit, not a mask"
    )
    assert job["timeout-minutes"] <= 15


def test_actions_are_pinned_as_ci_pins_them(audit: dict) -> None:
    """One pin per action across workflows: a bump in ``ci.yml`` moves this too."""
    ci = yaml.safe_load(CI_WORKFLOW.read_text())
    pins = {
        s["uses"].split("@")[0]: s["uses"]
        for job in ci["jobs"].values()
        for s in job["steps"]
        if "uses" in s
    }
    (job,) = audit["jobs"].values()
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    assert uses
    for u in uses:
        assert pins.get(u.split("@")[0]) == u, u


def test_the_audit_is_outside_the_deploy_gate(audit: dict) -> None:
    """A finding must never block ``scripts/deploy.sh``: the gate reads ``ci.yml``'s
    push run alone, and ``ci.yml`` runs no audit."""
    deploy = DEPLOY.read_text()
    assert "actions/workflows/ci.yml/runs" in deploy
    assert "audit.yml" not in deploy
    assert "audit_lock" not in CI_WORKFLOW.read_text()
    assert audit["name"] != yaml.safe_load(CI_WORKFLOW.read_text())["name"]


@pytest.mark.parametrize("heading", ["Extraction dependencies", "The lock audit"])
def test_the_linked_sections_exist(heading: str) -> None:
    """The summary and AGENTS.md link DEPLOYMENT.md's sections by anchor: keep the
    headings."""
    deployment = (REPO / "docs" / "DEPLOYMENT.md").read_text()
    assert re.search(rf"^## {heading}$", deployment, re.M)
