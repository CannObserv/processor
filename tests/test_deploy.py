"""``scripts/deploy.sh`` end to end, against a throwaway root (#2).

As CannObserv/status's ``tests/deploy/test_deploy.py``: a temporary origin and
checkout, ``PROCESSOR_DEPLOY_ROOT`` and ``PROCESSOR_DEPLOY_ETC`` under ``tmp_path``,
and stubs for ``uv``, ``sudo``, ``systemctl``, ``journalctl``, ``systemd-run``,
``getent`` and ``logger`` on ``PATH``. git, tar and jq are real.

The stubs model the service: ``systemctl restart`` starts whatever build ``live``
names, ``journalctl`` prints that process's ``starting`` and ``consuming`` records,
and ``systemd-run`` answers the smoke run, unless the build is listed as dead.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "deploy.sh"
UNIT = (REPO / "deploy" / "processor.service").read_text()

STUBS = {
    "uv": r"""#!/usr/bin/env bash
echo "$PWD UV_LINK_MODE=${UV_LINK_MODE:-} UV_PYTHON_DOWNLOADS=${UV_PYTHON_DOWNLOADS:-} $*" >>"$STATE/uv.log"
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
""",
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
        for d in (self.root, self.etc / "systemd" / "system", self.state, self.bin):
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
