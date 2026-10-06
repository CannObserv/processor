"""``scripts/deploy.sh`` end to end, against a throwaway root (#2).

As CannObserv/status's ``tests/deploy/test_deploy.py``: a temporary origin and
checkout, ``PROCESSOR_DEPLOY_ROOT`` and ``PROCESSOR_DEPLOY_ETC`` under ``tmp_path``,
and stubs for ``uv``, ``sudo``, ``systemctl``, ``journalctl``, ``systemd-run``,
``getent``, ``logger`` and ``curl`` on ``PATH``. git, tar and jq are real.

The stubs model the service: ``systemctl restart`` starts whatever build ``live``
names, ``journalctl`` prints that process's ``starting`` and ``consuming`` records,
and ``systemd-run`` answers the smoke run, unless the build is listed as dead.
``curl`` plays GitHub's Actions API (#34), never the real one.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "deploy.sh"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
UNIT = (REPO / "deploy" / "processor.service").read_text()

STUBS = {
    "uv": r"""#!/usr/bin/env bash
echo "$PWD UV_LINK_MODE=${UV_LINK_MODE:-}" \
  "UV_PYTHON_DOWNLOADS=${UV_PYTHON_DOWNLOADS:-} $*" >>"$STATE/uv.log"
echo "uv $*" >>"$STATE/calls.log"
[[ "${STUB_UV_FAIL:-}" == 1 ]] && exit 1
mkdir -p .venv/bin
printf 'home = %s\n' "${STUB_PY_HOME:-/usr/bin}" >.venv/pyvenv.cfg
printf '#!/usr/bin/env bash\nexit "${STUB_PROBE_RC:-0}"\n' >.venv/bin/python
chmod +x .venv/bin/python
""",
    "sudo": r"""#!/usr/bin/env bash
echo "$*" >>"$STATE/sudo.log"
exec "$@"
""",
    "systemctl": r"""#!/usr/bin/env bash
echo "$*" >>"$STATE/systemctl.log"
case "$1" in
  restart)
    rev="$(cat "$PROCESSOR_DEPLOY_ROOT/live/REVISION" 2>/dev/null || echo dev)"
    echo "$rev" >>"$STATE/started"
    ;;
  show) wc -l <"$STATE/started" 2>/dev/null || echo 0 ;;
esac
exit 0
""",
    "journalctl": r"""#!/usr/bin/env bash
echo "$*" >>"$STATE/journalctl.log"
build="$(tail -n 1 "$STATE/started" 2>/dev/null)"
[[ " ${STUB_DEAD:-} " == *" $build "* ]] && exit 0
echo "-- a systemd line --"
printf '{"message": "starting", "build": "%s", "child_containment": "%s"}\n' \
  "$build" "${STUB_CONTAINMENT:-required}"
echo '{"message": "consuming"}'
""",
    "systemd-run": r"""#!/usr/bin/env bash
echo "$*" >>"$STATE/systemd-run.log"
build="$(tail -n 1 "$STATE/started" 2>/dev/null)"
[[ " ${STUB_SMOKE_DEAD:-} " == *" $build "* ]] && { echo "smoke: boom" >&2; exit 1; }
printf '{"result": "pass", "build": "%s", "child_containment": "required"}\n' "$build"
""",
    "getent": r"""#!/usr/bin/env bash
[[ "$1 $2" == "passwd processor" && "${STUB_NO_USER:-}" != 1 ]]
""",
    "logger": r"""#!/usr/bin/env bash
echo "$*" >>"$STATE/logger.log"
echo "logger $*" >>"$STATE/calls.log"
""",
    # GitHub's Actions API (#34), as in CannObserv/status's tests (status#11): it
    # answers from $STATE/ci, runs-<n>.json poll by poll (the last repeating) and
    # jobs-<id>.json. With none written, one green push run on main for whatever
    # commit was asked about, with green lint and test. STUB_CI_FAIL is GitHub
    # refusing: as real curl does, only --fail-with-body passes STUB_CI_BODY on.
    "curl": r"""#!/usr/bin/env bash
url="${@: -1}"
echo "$url" >>"$STATE/curl.log"
echo "curl $url" >>"$STATE/calls.log"
ci="$STATE/ci"
if [[ -n "${STUB_CI_FAIL:-}" ]]; then
  [[ " $* " == *" --fail-with-body "* ]] && printf '%s\n' "${STUB_CI_BODY:-}"
  exit "$STUB_CI_FAIL"
fi
case "$url" in
  */jobs*)
    id="${url#*/actions/runs/}"
    id="${id%%/*}"
    cat "$ci/jobs-$id.json" 2>/dev/null || echo '{"jobs":[
      {"name":"lint","status":"completed","conclusion":"success"},
      {"name":"test","status":"completed","conclusion":"success"}]}' ;;
  *)
    sha="${url#*head_sha=}"
    sha="${sha%%&*}"
    n="$(cat "$ci/polls" 2>/dev/null || echo 0)"
    echo $((n + 1)) >"$ci/polls"
    answers=("$ci"/runs-*.json)
    if [[ -e "${answers[0]}" ]]; then
      last=$((${#answers[@]} - 1))
      cat "$ci/runs-$((n < last ? n : last)).json"
    else
      echo "{\"workflow_runs\":[{\"id\":1,\"head_sha\":\"$sha\",\"event\":\"push\",
        \"head_branch\":\"main\",\"status\":\"completed\",\"conclusion\":\"success\",
        \"created_at\":\"2026-10-01T00:00:00Z\",\"html_url\":\"https://github.test/runs/1\"}]}"
    fi ;;
esac
""",
}


def ci_run(
    run_id: int,
    sha: str,
    *,
    event: str = "push",
    branch: str = "main",
    status: str = "completed",
    conclusion: str = "success",
    created: str = "2026-10-01T00:00:00Z",
) -> dict:
    """One workflow run as GitHub's Actions API lists it."""
    return {
        "id": run_id,
        "head_sha": sha,
        "event": event,
        "head_branch": branch,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "created_at": created,
        "html_url": f"https://github.test/runs/{run_id}",
    }


class Env:
    """The throwaway world one test deploys into."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root = tmp / "srv"
        self.etc = tmp / "etc"
        self.state = tmp / "state"
        self.bin = tmp / "bin"
        self.checkout = tmp / "checkout"
        self.ci = self.state / "ci"
        for d in (self.root, self.etc / "systemd" / "system", self.ci, self.bin):
            d.mkdir(parents=True)
        for name, body in STUBS.items():
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        origin = tmp / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp)
        self.git("clone", "-q", str(origin), str(self.checkout), cwd=tmp)
        (self.checkout / "scripts").mkdir()
        (self.checkout / "deploy").mkdir()
        shutil.copy(SCRIPT, self.checkout / "scripts" / "deploy.sh")
        (self.checkout / "deploy" / "processor.service").write_text(UNIT)
        (self.checkout / "scripts" / "smoke_scratch_bus.py").write_text("# stub\n")
        wheelhouse = self.checkout / ".wheelhouse"
        wheelhouse.mkdir()
        (wheelhouse / "co_core-0.19.7-py3-none-any.whl").write_text("wheel")
        (self.checkout / ".gitignore").write_text(".wheelhouse/\n")
        self.commit("first")
        self.env = os.environ | {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STATE": str(self.state),
            "PROCESSOR_DEPLOY_ROOT": str(self.root),
            "PROCESSOR_DEPLOY_ETC": str(self.etc),
            "PROCESSOR_DEPLOY_VERIFY_SECONDS": "2",
            "TMPDIR": str(tmp),
        }

    def git(self, *args: str, cwd: Path | None = None) -> str:
        env = os.environ | {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }  # fmt: skip
        return subprocess.run(
            ["git", *args], cwd=cwd or self.checkout, env=env, check=True,
            capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip

    def commit(self, message: str, *, push: bool = True) -> str:
        (self.checkout / "change.txt").write_text(message)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        if push:
            self.git("push", "-q", "origin", "HEAD:main")
        return self.git("rev-parse", "--short=12", "HEAD")

    def deploy(self, *args: str, umask: str = "022", **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", f'umask {umask} && exec bash "$0" "$@"',
             str(self.checkout / "scripts" / "deploy.sh"), *args],
            env=self.env | env, capture_output=True, text=True, timeout=60,
        )  # fmt: skip

    def live(self) -> str:
        return os.readlink(self.root / "live")

    def log(self, name: str) -> str:
        path = self.state / f"{name}.log"
        return path.read_text() if path.exists() else ""

    @property
    def installed_unit(self) -> Path:
        return self.etc / "systemd" / "system" / "processor.service"

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def ci_answers(self, *answers: list[dict] | str) -> None:
        """What GitHub lists as the commit's runs, poll by poll; the last repeats."""
        for n, runs in enumerate(answers):
            body = runs if isinstance(runs, str) else json.dumps({"workflow_runs": runs})
            (self.ci / f"runs-{n}.json").write_text(body)

    def ci_jobs(self, run_id: int, **conclusions: str) -> None:
        """A run's jobs by conclusion; lint or test left out is green, "absent" drops one."""
        jobs = {"lint": "success", "test": "success", **conclusions}
        body = [
            {"name": name, "status": "completed", "conclusion": conclusion}
            for name, conclusion in jobs.items()
            if conclusion != "absent"
        ]
        (self.ci / f"jobs-{run_id}.json").write_text(json.dumps({"jobs": body}))

    def calls(self) -> list[str]:
        return self.log("calls").splitlines()

    def github_calls(self) -> list[str]:
        return self.log("curl").splitlines()


@pytest.fixture
def w(tmp_path: Path) -> Env:
    if not shutil.which("jq"):
        pytest.skip("jq is not installed")
    world = Env(tmp_path)
    yield world
    for path in world.root.rglob("*"):  # releases are read-only; let pytest clean up
        if path.is_dir() and not path.is_symlink():
            path.chmod(path.stat().st_mode | stat.S_IWUSR)


def test_a_first_deploy_builds_switches_installs_and_verifies(w: Env) -> None:
    build = w.git("rev-parse", "--short=12", "HEAD")
    result = w.deploy()
    assert result.returncode == 0, result.stderr
    assert w.live() == f"releases/{build}"
    release = w.root / "releases" / build
    assert (release / "REVISION").read_text() == f"{build}\n"
    assert (release / ".wheelhouse" / "co_core-0.19.7-py3-none-any.whl").exists()
    assert w.installed_unit.read_text() == UNIT
    assert "restart processor" in w.log("systemctl")
    assert f"live -> {build} (was nothing)" in w.log("logger")


def test_the_release_is_read_only_and_readable_by_the_service_user(w: Env) -> None:
    # Under exedev's tightest umask too: the service user is "other" to the release.
    assert w.deploy(umask="077").returncode == 0
    release = w.root / "releases" / w.git("rev-parse", "--short=12", "HEAD")
    for path in [release, *release.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        assert not mode & 0o222, path
        assert mode & stat.S_IROTH, path
        if path.is_dir():
            assert mode & stat.S_IXOTH, path


def test_the_service_user_can_reach_the_release_under_any_umask(w: Env) -> None:
    # CR 14: `mkdir -p` made releases/ with the operator's umask (700 under 077),
    # and the release's chmod does not reach its parent: processor could reach no
    # release at all. Every directory from the root down must let "other" through.
    assert w.deploy(umask="077").returncode == 0
    release = w.root / "releases" / w.git("rev-parse", "--short=12", "HEAD")
    for path in (w.root, w.root / "releases", release):
        assert path.stat().st_mode & stat.S_IXOTH, f"{path} is {stat.filemode(path.stat().st_mode)}"


def test_the_build_is_non_editable_against_the_system_python(w: Env) -> None:
    assert w.deploy().returncode == 0
    (line,) = w.log("uv").splitlines()
    for flag in ("sync --locked", "--no-dev", "--no-editable", "--compile-bytecode",
                 "--python /usr/bin/python3.12"):  # fmt: skip
        assert flag in line


def test_the_build_copies_never_hardlinks_from_the_uv_cache(w: Env) -> None:
    # CR 11: on Linux uv hardlinks from ~/.cache/uv, which shares each inode with the
    # cache and every dev venv (measured: 4 links on lxml/__init__.py). The release's
    # chmod would reach them, and an edit to any of them would reach production.
    assert w.deploy().returncode == 0
    (line,) = w.log("uv").splitlines()
    assert "UV_LINK_MODE=copy" in line and "UV_PYTHON_DOWNLOADS=never" in line


def test_a_venv_on_an_interpreter_under_home_is_refused(w: Env) -> None:
    result = w.deploy(STUB_PY_HOME="/home/exedev/.local/share/uv/python/bin")
    assert result.returncode == 1
    assert "cannot; nothing switched" in result.stderr
    assert not (w.root / "live").exists()


def test_the_smoke_runs_as_the_unit_with_the_credentials_dir_spelled_out(w: Env) -> None:
    assert w.deploy().returncode == 0
    run = w.log("systemd-run")
    assert "-p User=processor" in run and "-p ProtectHome=yes" in run
    assert "-p EnvironmentFile=/etc/processor/.env" in run
    assert (
        "-p Environment=GOOGLE_APPLICATION_CREDENTIALS="
        "/run/credentials/processor-smoke.service/gcs-writer-key"
    ) in run
    assert "%d" not in run


@pytest.mark.parametrize(
    ("ref_kind", "message"),
    [("unpushed", "not on origin/main"), ("bogus", "cannot resolve")],
)
def test_only_a_pushed_main_commit_is_deployed(w: Env, ref_kind: str, message: str) -> None:
    ref = w.commit("local only", push=False) if ref_kind == "unpushed" else "no-such-ref"
    result = w.deploy(ref)
    assert result.returncode == 1 and message in result.stderr
    assert not (w.root / "releases").exists()


def test_refuses_root(w: Env) -> None:
    (w.bin / "id").write_text("#!/usr/bin/env bash\necho 0\n")
    (w.bin / "id").chmod(0o755)
    result = w.deploy()
    assert result.returncode == 1 and "not root" in result.stderr


def test_refuses_while_another_deploy_holds_the_lock(w: Env) -> None:
    lock = w.root / ".deploy.lock"
    with subprocess.Popen(["flock", str(lock), "sleep", "10"]) as holder:
        try:
            for _ in range(50):
                if lock.exists():
                    break
                subprocess.run(["sleep", "0.1"])
            result = w.deploy()
        finally:
            holder.kill()
    assert result.returncode == 1 and "another deploy is running" in result.stderr


def test_refuses_without_the_service_user(w: Env) -> None:
    result = w.deploy(STUB_NO_USER="1")
    assert result.returncode == 1 and "does not exist here; nothing switched" in result.stderr
    assert not (w.root / "live").exists()


@pytest.mark.parametrize(
    ("break_it", "message"),
    [("archive", "git archive failed for"), ("wheelhouse", "copying the wheelhouse failed for")],
)
def test_a_failed_extract_or_copy_says_so(w: Env, break_it: str, message: str) -> None:
    # CR 13: under set -e alone, the deploy would exit with no message.
    if break_it == "archive":
        (w.bin / "tar").write_text("#!/usr/bin/env bash\ncat >/dev/null; exit 2\n")
        (w.bin / "tar").chmod(0o755)
    else:
        (w.checkout / ".wheelhouse" / "co_core-0.19.7-py3-none-any.whl").chmod(0)
    result = w.deploy()
    assert result.returncode == 1
    assert f"deploy: {message}" in result.stderr and "nothing switched" in result.stderr
    assert not (w.root / "live").exists()


def test_a_failed_build_switches_nothing(w: Env) -> None:
    result = w.deploy(STUB_UV_FAIL="1")
    assert result.returncode == 1 and "uv sync failed" in result.stderr
    assert not (w.root / "live").exists()


def test_a_failed_verify_switches_back_and_proves_the_old_build(w: Env) -> None:
    old = w.git("rev-parse", "--short=12", "HEAD")
    assert w.deploy().returncode == 0
    old_unit = w.installed_unit.read_text()
    new = w.commit("second")
    (w.checkout / "deploy" / "processor.service").write_text(UNIT + "# changed\n")
    new = w.commit("unit change")
    result = w.deploy(STUB_DEAD=new)
    assert result.returncode == 1, result.stderr
    assert f"switched back to {old}, which is answering" in result.stderr
    assert w.live() == f"releases/{old}"
    assert w.installed_unit.read_text() == old_unit
    assert f"rolled back to {old} after {new} failed" in w.log("logger")


def test_a_failed_smoke_switches_back(w: Env) -> None:
    old = w.git("rev-parse", "--short=12", "HEAD")
    assert w.deploy().returncode == 0
    new = w.commit("second")
    result = w.deploy(STUB_SMOKE_DEAD=new)
    assert result.returncode == 1
    assert f"the smoke run failed on {new}" in result.stderr
    assert w.live() == f"releases/{old}"


def test_containment_off_fails_the_verify(w: Env) -> None:
    # Production runs required: a release that starts uncontained is not deployed.
    result = w.deploy(STUB_CONTAINMENT="off")
    assert result.returncode == 4
    assert "child_containment required" in result.stderr


def test_a_first_deploy_that_fails_puts_the_old_unit_back_and_exits_4(w: Env) -> None:
    w.installed_unit.write_text("[Service]\nUser=exedev\n")
    build = w.git("rev-parse", "--short=12", "HEAD")
    result = w.deploy(STUB_DEAD=build)
    assert result.returncode == 4
    assert "no previous release" in result.stderr
    assert w.installed_unit.read_text() == "[Service]\nUser=exedev\n"
    # CR 3: and the service is restarted onto it, never left on the failed build.
    calls = w.log("systemctl").splitlines()
    restore = calls.index("daemon-reload", calls.index("restart processor"))
    assert "restart processor" in calls[restore:]
    assert "restarted on the unit it replaced" in result.stderr


def test_when_the_old_build_fails_too_it_exits_4(w: Env) -> None:
    old = w.git("rev-parse", "--short=12", "HEAD")
    assert w.deploy().returncode == 0
    new = w.commit("second")
    result = w.deploy(STUB_DEAD=f"{old} {new}")
    assert result.returncode == 4 and "NOT answering" in result.stderr


def test_a_rollback_reuses_the_old_release(w: Env) -> None:
    old = w.git("rev-parse", "--short=12", "HEAD")
    assert w.deploy().returncode == 0
    w.commit("second")
    assert w.deploy().returncode == 0
    result = w.deploy(old)
    assert result.returncode == 0, result.stderr
    assert f"reusing release {old}" in result.stderr
    assert w.live() == f"releases/{old}"
    assert len(w.log("uv").splitlines()) == 2


def test_an_interrupted_build_is_rebuilt(w: Env) -> None:
    build = w.git("rev-parse", "--short=12", "HEAD")
    (w.root / "releases" / build).mkdir(parents=True)  # no REVISION
    result = w.deploy()
    assert result.returncode == 0, result.stderr
    assert "an interrupted build; rebuilding" in result.stderr


def test_the_live_release_is_never_rebuilt_in_place(w: Env) -> None:
    assert w.deploy().returncode == 0
    result = w.deploy(STUB_PROBE_RC="1")  # its venv no longer runs
    assert result.returncode == 1 and "live runs it" in result.stderr


def test_an_unchanged_unit_is_not_reinstalled(w: Env) -> None:
    assert w.deploy().returncode == 0
    w.commit("second")
    before = w.log("sudo").count("install -m 644")
    assert w.deploy().returncode == 0
    assert w.log("sudo").count("install -m 644") == before


def test_host_configs_are_compared_never_installed(w: Env) -> None:
    dropin = w.checkout / "deploy" / "tailscaled.service.d" / "90-processor-oom.conf"
    dropin.parent.mkdir()
    dropin.write_text("[Service]\nOOMScoreAdjust=-950\n")
    w.commit("drop-in")
    result = w.deploy()
    assert result.returncode == 0
    assert "deploy/tailscaled.service.d/90-processor-oom.conf is not installed" in result.stderr
    assert not (w.etc / "systemd/system/tailscaled.service.d").exists()


def test_old_releases_are_pruned_but_never_live(w: Env) -> None:
    builds = [w.git("rev-parse", "--short=12", "HEAD")]
    assert w.deploy().returncode == 0
    for i in range(3):
        builds.append(w.commit(f"c{i}"))
        assert w.deploy().returncode == 0
    assert w.deploy(builds[0]).returncode == 0  # live on the oldest
    kept = sorted(p.name for p in (w.root / "releases").iterdir())
    result = w.deploy(builds[0], PROCESSOR_DEPLOY_KEEP="1")
    assert result.returncode == 0
    assert sorted(p.name for p in (w.root / "releases").iterdir()) == [builds[0]]
    assert len(kept) == 4


def test_help() -> None:
    result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "scripts/deploy.sh [<ref>]" in result.stdout


def test_the_verify_reads_only_the_new_process_since_the_restart(w: Env) -> None:
    # CR 5: by PID alone, an older process that had the same PID, in this boot or
    # a past one, could pass the verify with its own records.
    assert w.deploy().returncode == 0
    (query,) = set(w.log("journalctl").splitlines())
    assert query.startswith("-b --since @")
    assert "_SYSTEMD_UNIT=processor.service _PID=1" in query
    assert "-u " not in query


# A wait no stubbed poll can outlast, polled at once: SECONDS ticks on the wall
# clock's second, so a 1 s wait can be spent before its first answer (status#11, CR 1).
PROMPT_POLLS = {"PROCESSOR_DEPLOY_CI_WAIT_SECONDS": "60", "PROCESSOR_DEPLOY_CI_POLL_SECONDS": "0"}


def index_of(calls: list[str], needle: str) -> int:
    return next(i for i, call in enumerate(calls) if needle in call)


def assert_nothing_built(w: Env) -> None:
    """Refused before the build: no release, no link, no uv, no unit touched."""
    assert not (w.root / "releases").exists()
    assert not (w.root / "live").is_symlink()
    assert not w.log("uv") and not w.log("sudo")


class TestTheCIGate:
    """#34: a deploy needs the commit's CI green, as status#11 shipped it.

    The run that decides is the newest push run of ci.yml on main for exactly
    this commit. Every FF merge here has a pull_request run on the same SHA too.
    """

    @staticmethod
    def other_and_push(w: Env, event: str, branch: str) -> None:
        """A newer run of another kind (8) beside the push run on main (7)."""
        sha = w.head()
        later, earlier = "2026-10-01T02:00:00Z", "2026-10-01T01:00:00Z"
        w.ci_answers([ci_run(8, sha, event=event, branch=branch, created=later),
                      ci_run(7, sha, created=earlier)])  # fmt: skip

    # Each differs from the push run on main in one way only, so each half of
    # the filter is tested on its own.
    OTHER_RUNS = pytest.mark.parametrize(
        ("event", "branch"),
        [("pull_request", "34-deploy-ci-gate"), ("pull_request", "main"),
         ("workflow_dispatch", "main"), ("push", "feature")],
        ids=["pr", "pr-from-main", "dispatch-on-main", "push-elsewhere"],
    )  # fmt: skip

    def test_a_deploy_asks_about_this_commit_as_pushed_to_main(self, w: Env) -> None:
        result = w.deploy()
        assert result.returncode == 0, result.stderr
        runs, jobs = w.github_calls()
        assert runs.startswith(
            "https://api.github.com/repos/CannObserv/processor/actions/workflows/ci.yml/runs?"
        )
        for param in (f"head_sha={w.head()}", "event=push", "branch=main"):
            assert param in runs
        assert jobs.endswith("/repos/CannObserv/processor/actions/runs/1/jobs?per_page=100")

    def test_a_pass_is_logged_with_its_run_before_anything_is_built(self, w: Env) -> None:
        build = w.git("rev-parse", "--short=12", "HEAD")
        result = w.deploy()
        assert result.returncode == 0, result.stderr
        assert f"CI passed for {build}: https://github.test/runs/1" in result.stderr
        calls = w.calls()
        passed = index_of(calls, "CI passed")
        assert calls[passed] == (
            f"logger -t processor-deploy CI passed for {build}: https://github.test/runs/1"
        )
        assert passed < index_of(calls, "uv sync")

    def test_a_failed_job_is_refused_by_name(self, w: Env) -> None:
        w.ci_answers([ci_run(7, w.head(), conclusion="failure")])
        w.ci_jobs(7, test="failure")
        result = w.deploy()
        assert result.returncode == 1
        assert "test (failure)" in result.stderr and "lint (" not in result.stderr
        assert "https://github.test/runs/7" in result.stderr
        assert "fix it on main" in result.stderr
        assert_nothing_built(w)

    @pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "absent"])
    def test_a_required_job_that_did_not_succeed_is_not_a_pass(
        self, w: Env, conclusion: str
    ) -> None:
        """A skipped job leaves the run's own conclusion "success"."""
        w.ci_answers([ci_run(7, w.head())])
        w.ci_jobs(7, lint=conclusion)
        result = w.deploy()
        assert result.returncode == 1
        expected = "lint (not in the run)" if conclusion == "absent" else f"lint ({conclusion})"
        assert expected in result.stderr
        assert_nothing_built(w)

    def test_a_failed_job_the_checkout_does_not_know_is_refused(self, w: Env) -> None:
        """CI_JOBS comes from the checkout running deploy.sh, which may predate a
        job added to ci.yml since. Every job the run lists counts (status#11, CR 6)."""
        w.ci_answers([ci_run(7, w.head())])
        w.ci_jobs(7, e2e="failure")
        result = w.deploy()
        assert result.returncode == 1 and "e2e (failure)" in result.stderr
        assert_nothing_built(w)

    def test_a_run_that_did_not_conclude_success_is_refused_whatever_its_jobs(self, w: Env) -> None:
        w.ci_answers([ci_run(7, w.head(), conclusion="failure")])
        w.ci_jobs(7)
        result = w.deploy()
        assert result.returncode == 1 and "run concluded failure" in result.stderr
        assert_nothing_built(w)

    def test_a_cancelled_run_says_re_run_it(self, w: Env) -> None:
        """Cancelled is not a verdict: a run that timed out waiting for a runner
        (#23's PR, GitHub capacity) lists no jobs, and has nothing to fix."""
        w.ci_answers([ci_run(7, w.head(), conclusion="cancelled")])
        w.ci_jobs(7, lint="absent", test="absent")
        result = w.deploy()
        assert result.returncode == 1
        assert "run concluded cancelled" in result.stderr
        assert "test (not in the run)" in result.stderr
        assert "re-run it from its page (a re-run counts)" in result.stderr
        assert "fix it on main" not in result.stderr
        assert_nothing_built(w)

    def test_a_run_still_going_when_the_wait_ends_is_refused(self, w: Env) -> None:
        w.ci_answers([ci_run(7, w.head(), status="in_progress")])
        result = w.deploy(PROCESSOR_DEPLOY_CI_WAIT_SECONDS="0")
        assert result.returncode == 1
        assert "still in_progress" in result.stderr
        assert "https://github.test/runs/7" in result.stderr
        assert not [c for c in w.github_calls() if "/jobs" in c]
        assert_nothing_built(w)

    def test_a_run_that_finishes_within_the_wait_is_deployed(self, w: Env) -> None:
        sha = w.head()
        w.ci_answers([ci_run(7, sha, status="in_progress")], [ci_run(7, sha)])
        started = time.monotonic()
        result = w.deploy(**PROMPT_POLLS)
        assert result.returncode == 0, result.stderr
        assert time.monotonic() - started < 15, "polled every 30 s, not as told"
        # The wait holds the lock for up to 10 minutes: say where to watch.
        waiting = next(ln for ln in result.stderr.splitlines() if "waiting for CI" in ln)
        assert "https://github.test/runs/7" in waiting
        assert len([c for c in w.github_calls() if "/runs?" in c]) == 2

    def test_the_tip_just_pushed_waits_for_its_run_to_appear(self, w: Env) -> None:
        """GitHub queues the push run a few seconds after the push."""
        w.ci_answers([], [ci_run(7, w.head())])
        result = w.deploy(**PROMPT_POLLS)
        assert result.returncode == 0, result.stderr

    def test_the_tip_with_no_run_waits_bounded_then_is_refused(self, w: Env) -> None:
        w.ci_answers([])
        # CR 3: SECONDS ticks on the wall clock's second, so a 2 s wait can show
        # its first poll 0 s left; 3 s needs that poll to take over a second.
        result = w.deploy(PROCESSOR_DEPLOY_CI_WAIT_SECONDS="3",
                          PROCESSOR_DEPLOY_CI_POLL_SECONDS="1")  # fmt: skip
        assert result.returncode == 1
        assert "no CI run" in result.stderr and "after 3s" in result.stderr
        assert "--skip-ci" in result.stderr
        assert len(w.github_calls()) >= 2
        assert_nothing_built(w)

    def test_a_commit_behind_the_tip_with_no_run_is_refused_at_once(self, w: Env) -> None:
        """An FF merge pushes many commits, and GitHub runs CI on the push's newest
        only: #33 put 21 commits on main, and only fe19a2b has a push run."""
        middle = w.git("rev-parse", "--short=12", "HEAD")
        w.commit("tip")
        w.ci_answers([])
        result = w.deploy(middle)  # the default wait, 600 s
        assert result.returncode == 1
        assert "no CI run" in result.stderr
        assert "newest commit of each push" in result.stderr and "--skip-ci" in result.stderr
        assert len(w.github_calls()) == 1
        assert_nothing_built(w)

    def test_a_run_for_another_commit_is_not_this_ones(self, w: Env) -> None:
        older = w.head()
        w.commit("tip")
        w.ci_answers([ci_run(7, older)])
        result = w.deploy(PROCESSOR_DEPLOY_CI_WAIT_SECONDS="0")
        assert result.returncode == 1 and "no CI run" in result.stderr

    @OTHER_RUNS
    def test_a_run_of_another_kind_alone_does_not_count(
        self, w: Env, event: str, branch: str
    ) -> None:
        """Every FF merge has a pull_request run beside its push run, on the same SHA."""
        w.ci_answers([ci_run(8, w.head(), event=event, branch=branch)])
        w.ci_jobs(8)
        result = w.deploy(PROCESSOR_DEPLOY_CI_WAIT_SECONDS="0")
        assert result.returncode == 1 and "no CI run" in result.stderr
        assert not [c for c in w.github_calls() if "/jobs" in c]
        assert_nothing_built(w)

    @OTHER_RUNS
    def test_a_failed_run_of_another_kind_changes_nothing(
        self, w: Env, event: str, branch: str
    ) -> None:
        self.other_and_push(w, event, branch)
        w.ci_jobs(8, test="failure")
        w.ci_jobs(7)
        result = w.deploy()
        assert result.returncode == 0, result.stderr
        assert w.github_calls()[-1].endswith("/actions/runs/7/jobs?per_page=100")

    @OTHER_RUNS
    def test_a_green_run_of_another_kind_does_not_pass_a_failed_push_run(
        self, w: Env, event: str, branch: str
    ) -> None:
        self.other_and_push(w, event, branch)
        w.ci_jobs(8)
        w.ci_jobs(7, lint="failure")
        result = w.deploy()
        assert result.returncode == 1
        assert "lint (failure)" in result.stderr and "https://github.test/runs/7" in result.stderr

    def test_of_two_push_runs_on_main_the_newest_decides(self, w: Env) -> None:
        """A re-run counts: GitHub reports a run's latest attempt, and main moved back
        and forward again lists the commit twice."""
        sha = w.head()
        w.ci_answers([ci_run(7, sha, created="2026-10-01T01:00:00Z"),
                      ci_run(9, sha, created="2026-10-01T03:00:00Z"),
                      ci_run(8, sha, created="2026-10-01T02:00:00Z")])  # fmt: skip
        w.ci_jobs(7, test="failure")
        w.ci_jobs(8, test="failure")
        w.ci_jobs(9)
        result = w.deploy()
        assert result.returncode == 0, result.stderr
        assert w.github_calls()[-1].endswith("/actions/runs/9/jobs?per_page=100")

    @pytest.mark.parametrize(
        ("rc", "body", "said"),
        [
            ("22", '{"message":"API rate limit exceeded for 192.0.2.1."}',
             "GitHub says: API rate limit exceeded for 192.0.2.1."),
            ("22", "<html>502 Bad Gateway</html>", "GitHub did not answer"),
            ("28", "", "GitHub did not answer"),
        ],
        ids=["403-rate-limit", "5xx-html", "timeout"],
    )  # fmt: skip
    def test_github_refusing_refuses_the_deploy(
        self, w: Env, rc: str, body: str, said: str
    ) -> None:
        """Unauthenticated: 60 requests an hour per address. A refusal never passes."""
        result = w.deploy(STUB_CI_FAIL=rc, STUB_CI_BODY=body)
        assert result.returncode == 1
        assert said in result.stderr
        assert "Nothing was built" in result.stderr and "--skip-ci" in result.stderr
        assert_nothing_built(w)

    @pytest.mark.parametrize("body", ["<html>unicorn</html>", '{"total_count":0}', "", " \n"])
    def test_a_runs_answer_that_is_not_json_refuses_the_deploy(self, w: Env, body: str) -> None:
        """CR 4: JSON without workflow_runs fails the same way as no JSON at all.
        CR 5: so does an empty body, which jq reads as no input, not as a run."""
        w.ci_answers(body)
        result = w.deploy(PROCESSOR_DEPLOY_CI_WAIT_SECONDS="0")
        assert result.returncode == 1
        assert "not the JSON expected" in result.stderr and "nothing was built" in result.stderr
        assert_nothing_built(w)

    @pytest.mark.parametrize("body", ["<html>unicorn</html>", '{"total_count":0}', "", " \n"])
    def test_a_jobs_answer_that_is_not_json_refuses_the_deploy(self, w: Env, body: str) -> None:
        """jq alone exits 5, and says neither that nothing was built nor --skip-ci
        (status#11, CR 10). CR 5: an empty body passed the gate: jq given no input
        prints nothing and exits 0, so no job looked failed."""
        w.ci_answers([ci_run(7, w.head())])
        (w.ci / "jobs-7.json").write_text(body)
        result = w.deploy()
        assert result.returncode == 1
        assert "not the JSON expected" in result.stderr and "nothing was built" in result.stderr
        assert_nothing_built(w)

    def test_skip_ci_deploys_without_asking_and_logs_it_first(self, w: Env) -> None:
        build = w.git("rev-parse", "--short=12", "HEAD")
        w.ci_answers([ci_run(7, w.head(), conclusion="failure")])
        w.ci_jobs(7, test="failure")
        result = w.deploy("--skip-ci")
        assert result.returncode == 0, result.stderr
        assert not w.github_calls()
        calls = w.calls()
        skipped = index_of(calls, "--skip-ci")
        assert (
            calls[skipped] == f"logger -t processor-deploy CI not checked for {build} (--skip-ci)"
        )
        assert skipped < index_of(calls, "uv sync")

    def test_a_rollback_is_gated_too(self, w: Env) -> None:
        """One rule (#34, option a): a kept release proves it was built, not that
        its CI passed. --skip-ci is the escape when GitHub cannot answer."""
        old = w.head()
        assert w.deploy().returncode == 0
        second = w.commit("second")
        assert w.deploy().returncode == 0
        syncs, restarts = len(w.log("uv").splitlines()), w.log("systemctl").count("restart ")
        w.ci_answers([ci_run(7, old, conclusion="failure")])
        w.ci_jobs(7, test="failure")
        result = w.deploy(old[:12])
        assert result.returncode == 1 and "test (failure)" in result.stderr
        # CR 2: refused before anything changed, not merely somewhere other than old.
        assert w.live() == f"releases/{second}"
        assert len(w.log("uv").splitlines()) == syncs
        assert w.log("systemctl").count("restart ") == restarts

    def test_help_names_skip_ci(self) -> None:
        result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
        assert result.returncode == 0 and "--skip-ci" in result.stdout

    def test_the_jobs_the_gate_requires_run_on_a_push_to_main(self) -> None:
        """A job renamed in ci.yml must change CI_JOBS too. A job added need not:
        every job a run lists counts."""
        required = re.search(r"^CI_JOBS=\((.*)\)$", SCRIPT.read_text(), re.M)
        assert required, "deploy.sh names its jobs in CI_JOBS=(...)"
        workflow = yaml.safe_load(CI_WORKFLOW.read_text())
        assert "main" in workflow[True]["push"]["branches"]  # `on:` loads as True
        names = {job.get("name", key) for key, job in workflow["jobs"].items()}
        for name in required.group(1).split():
            assert name in names
            (job,) = [j for k, j in workflow["jobs"].items() if j.get("name", k) == name]
            assert "if" not in job, f"{name} may be skipped on a push"
