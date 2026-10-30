"""shellcheck over this repo's own shell scripts, at commit time and in CI (#81).

A port of CannObserv/broker#112. Nothing linted the shell that ships
(``scripts/deploy.sh``, ``deploy/nodesource.sh``, …) beyond the tests that run it.

**One version, locked.** ``shellcheck-py`` sits in the ``lint`` dependency group,
so ``uv.lock`` hash-pins its wheel, ``scripts/audit_lock.sh`` audits it, and the
node, CI and every clone run the same binary. No root install on the node.

**Two callers, one selector.** ``scripts/shellcheck.sh`` decides which files are
this repo's: every tracked ``*.sh`` and ``.githooks/*``, never a symlink (the
``.claude/hooks/`` ones point into ``skills-vendor/``) and never ``.skills/``
(the vendored refresh hook rewrites ``doctor.sh`` there with no review).
``.githooks/pre-commit`` runs it over what is staged; ``ci.yml``'s ``lint`` job
over the tree. The hook is per clone and ``--no-verify`` skips it, so CI is the
enforcement, and a step in ``lint`` keeps ``scripts/deploy.sh``'s ``CI_JOBS``
gate whole.

**No wheelhouse.** Both call ``uv run --frozen --no-config --only-group lint``:
no project, so no co-core, and ``--no-config`` because ``[tool.uv] find-links``
is read even then, and a clone has no ``./.wheelhouse`` (#79's stand-in for
broker's tracked ``.wheelhouse/.gitkeep``).

The hook runs here in a scratch repository under a fake ``uv`` that hands each
tool to this venv's own copy, so nothing is resolved or fetched.
"""

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SELECTOR = ROOT / "scripts" / "shellcheck.sh"
HOOK = ROOT / ".githooks" / "pre-commit"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY = ROOT / "scripts" / "deploy.sh"
DOC = ROOT / "docs" / "DEPLOYMENT.md"
VENV_BIN = Path(sys.executable).parent

#: How both callers reach the locked tools: no project, no config, the lock as committed.
UV_RUN = ["run", "--frozen", "--no-config", "--only-group", "lint"]
#: A finding shellcheck reports at its default severity.
BAD = '#!/usr/bin/env bash\ncd "$1"\n'


def _fake_uv(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """A ``uv`` that records each argv and runs ``uv run ... <tool>`` as this
    venv's ``<tool>``; any other spelling exits 99. Returns the environment to
    run under and the argv log."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    log = tmp_path / "uv-argv"
    fake = bin_dir / "uv"
    prefix, n = " ".join(UV_RUN), len(UV_RUN)
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >> "{log}"\n'
        f'[ "${{*:1:{n}}}" = "{prefix}" ] || {{ echo "fake uv: $*" >&2; exit 99; }}\n'
        f"shift {n}\n"
        f'tool=$1; shift\nexec "{VENV_BIN}/$tool" "$@"\n'
    )
    fake.chmod(0o755)
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    return env, log


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60
    )


def _own_scripts() -> set[str]:
    """What the selector should take, worked out here from the index."""
    own = set()
    for line in _git(ROOT, "ls-files", "-s").stdout.splitlines():
        mode, _, _, path = line.split(maxsplit=3)
        if mode == "120000" or path.startswith(("skills-vendor/", ".skills/")):
            continue
        if path.endswith(".sh") or path.startswith(".githooks/"):
            own.add(path)
    return own


def _lock_package(name: str) -> dict:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    (pkg,) = [p for p in lock["package"] if p["name"] == name]
    return pkg


class TestTheLock:
    def test_the_lint_group_carries_both_tools_and_dev_includes_it(self):
        groups = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]
        assert any(re.match(r"shellcheck-py\b", spec) for spec in groups["lint"])
        assert any(re.match(r"ruff\b", spec) for spec in groups["lint"])
        assert {"include-group": "lint"} in groups["dev"]
        assert not any(isinstance(s, str) and s.startswith("ruff") for s in groups["dev"]), (
            "ruff lives in lint alone, so the hook and CI read one pin"
        )

    def test_the_binary_is_a_hashed_linux_x86_64_wheel(self):
        """The node and CI's runner are both linux x86_64. A wheel carries the
        binary; an sdist would fetch it at build time, unhashed."""
        pkg = _lock_package("shellcheck-py")
        wheels = [w for w in pkg["wheels"] if "manylinux" in w["url"] and "x86_64" in w["url"]]
        assert wheels, pkg["wheels"]
        assert all(w["hash"].startswith("sha256:") for w in wheels)

    def test_the_doc_names_the_locked_version(self):
        """docs/DEPLOYMENT.md says which shellcheck the gate runs, held to the lock:
        shellcheck-py's fourth field is its own packaging release, the first three
        the binary's."""
        binary = ".".join(_lock_package("shellcheck-py")["version"].split(".")[:3])
        reported = subprocess.run(
            [VENV_BIN / "shellcheck", "--version"], capture_output=True, text=True, check=True
        ).stdout
        assert f"version: {binary}\n" in reported
        assert f"shellcheck {binary} from `shellcheck-py`" in DOC.read_text()


class TestTheSelector:
    def test_it_takes_every_own_script_and_no_upstream_one(self, tmp_path):
        env, log = _fake_uv(tmp_path)
        result = subprocess.run(
            ["bash", str(SELECTOR)], cwd=tmp_path, env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr
        (call,) = _calls(log)
        argv = call.split()
        assert argv[: len(UV_RUN) + 2] == [*UV_RUN, "shellcheck", "--"]
        taken = set(argv[len(UV_RUN) + 2 :])
        assert taken == _own_scripts()
        assert {
            "scripts/deploy.sh",
            "scripts/audit_lock.sh",
            "scripts/build_wheelhouse.sh",
            "scripts/shellcheck.sh",
            "deploy/nodesource.sh",
            ".githooks/pre-commit",
        } <= taken
        assert ".skills/doctor.sh" not in taken
        assert not any(p.startswith(".claude/") for p in taken)

    def test_the_tree_is_clean(self, tmp_path):
        """The gate starts green: the same run CI's step makes."""
        env, _ = _fake_uv(tmp_path)
        result = subprocess.run(
            ["bash", str(SELECTOR)], cwd=ROOT, env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_an_unknown_argument_is_a_usage_error(self, tmp_path):
        env, log = _fake_uv(tmp_path)
        result = subprocess.run(
            ["bash", str(SELECTOR), "--tree"], cwd=ROOT, env=env, capture_output=True, text=True
        )
        assert result.returncode == 2
        assert "usage:" in result.stderr
        assert _calls(log) == []

    def test_every_disable_gives_its_reason(self):
        """A ``# shellcheck disable=`` that stays says why, on its own line."""
        for path in sorted(_own_scripts()):
            for n, line in enumerate((ROOT / path).read_text().splitlines(), 1):
                if "shellcheck disable=" in line:
                    assert re.search(r"disable=SC\d+(,SC\d+)* # \S", line), f"{path}:{n}"


class TestTheHook:
    @pytest.fixture
    def clone(self, tmp_path):
        """A scratch repository with the hook enabled the way the doc says."""
        env, log = _fake_uv(tmp_path)
        repo = tmp_path / "repo"
        for src in (SELECTOR, HOOK):
            dest = repo / src.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        _git(repo, "init", "-q", "-b", "main")
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid")):
            _git(repo, "config", key, value)
        _git(repo, "config", "core.hooksPath", ".githooks")
        _git(repo, "add", "-A")
        seeded = _git(repo, "commit", "-q", "-m", "seed", env=env)
        assert seeded.returncode == 0, seeded.stdout + seeded.stderr
        return repo, env, log

    @staticmethod
    def _commit(repo: Path, env: dict[str, str], **files: str):
        for name, text in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        _git(repo, "add", "-A")
        return _git(repo, "commit", "-q", "-m", "change", env=env)

    @staticmethod
    def _head(repo: Path) -> str:
        return _git(repo, "rev-parse", "HEAD").stdout.strip()

    def test_a_planted_finding_fails_the_commit(self, clone):
        repo, env, _ = clone
        before = self._head(repo)
        result = self._commit(repo, env, **{"bad.sh": BAD})
        assert result.returncode != 0
        assert "SC2164" in result.stdout + result.stderr
        assert self._head(repo) == before

    def test_a_clean_script_commits(self, clone):
        repo, env, log = clone
        seen = len(_calls(log))
        result = self._commit(repo, env, **{"ok.sh": '#!/usr/bin/env bash\ncd "$1" || exit\n'})
        assert result.returncode == 0, result.stdout + result.stderr
        assert _calls(log)[seen:] == [f"{' '.join(UV_RUN)} shellcheck -- ok.sh"]

    @pytest.mark.parametrize(
        ("text", "rule"),
        [("import os\n", "F401"), ("x = {  'a':1 }\n", "would be reformatted")],
    )
    def test_staged_python_is_ruff_checked_and_format_checked(self, clone, text, rule):
        repo, env, _ = clone
        before = self._head(repo)
        result = self._commit(repo, env, **{"mod.py": text})
        assert result.returncode != 0
        assert rule in result.stdout + result.stderr
        assert self._head(repo) == before

    def test_clean_python_runs_both_ruff_passes(self, clone):
        repo, env, log = clone
        seen = len(_calls(log))
        result = self._commit(repo, env, **{"mod.py": "X = 1\n"})
        assert result.returncode == 0, result.stdout + result.stderr
        prefix = " ".join(UV_RUN)
        assert _calls(log)[seen:] == [
            f"{prefix} ruff check --force-exclude -- mod.py",
            f"{prefix} ruff format --check --force-exclude -- mod.py",
        ]

    def test_a_doc_runs_no_tool(self, clone):
        """ruff here reads no Markdown (no ``extend-include``, unlike broker#38)."""
        repo, env, log = clone
        seen = len(_calls(log))
        result = self._commit(repo, env, **{"doc.md": "# A doc\n"})
        assert result.returncode == 0, result.stdout + result.stderr
        assert _calls(log)[seen:] == []

    def test_upstream_scripts_are_not_its_to_lint(self, clone):
        """A symlink (a ``.claude/hooks/`` one points into ``skills-vendor/``),
        ``skills-vendor/`` itself and ``.skills/`` are upstream's."""
        repo, env, log = clone
        (repo / "upstream").mkdir()
        (repo / "upstream" / "x.sh").write_text(BAD)
        os.symlink("../upstream/x.sh", repo / "linked.sh")
        _git(repo, "add", "upstream/x.sh")
        _git(repo, "commit", "-q", "--no-verify", "-m", "upstream")
        seen = len(_calls(log))
        result = self._commit(repo, env, **{"skills-vendor/v/x.sh": BAD, ".skills/doctor.sh": BAD})
        assert result.returncode == 0, result.stdout + result.stderr
        assert _calls(log)[seen:] == []

    def test_a_deletion_lints_nothing(self, clone):
        repo, env, _ = clone
        self._commit(repo, env, **{"ok.sh": "#!/usr/bin/env bash\ntrue\n"})
        _git(repo, "rm", "-q", "ok.sh")
        result = _git(repo, "commit", "-q", "-m", "rm", env=env)
        assert result.returncode == 0, result.stdout + result.stderr


class TestTheWiring:
    def test_ci_runs_it_as_a_step_of_lint(self):
        """A step of a job ``scripts/deploy.sh``'s ``CI_JOBS`` requires green, so
        its gate covers the lint with no new entry; test_deploy.py holds
        ``CI_JOBS`` to ``ci.yml``."""
        required = re.search(r"^CI_JOBS=\((.*)\)$", DEPLOY.read_text(), re.M)
        assert required, "deploy.sh names its jobs in CI_JOBS=(...)"
        assert "lint" in required.group(1).split()
        steps = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["lint"]["steps"]
        runs = [step.get("run") for step in steps]
        assert "bash scripts/shellcheck.sh" in runs
        assert runs.index("uv sync --locked") < runs.index("bash scripts/shellcheck.sh")

    def test_the_hook_and_selector_are_executable(self):
        assert os.access(HOOK, os.X_OK)
        assert os.access(SELECTOR, os.X_OK)
        assert "scripts/shellcheck.sh --staged" in HOOK.read_text()

    def test_the_opt_in_is_documented(self):
        assert "git config core.hooksPath .githooks" in DOC.read_text()
