"""``scripts/co_pypi_token.py`` and ``scripts/co-pypi.sh``: one ``uv`` command
with a co-pypi access token in its environment, and nowhere else (#121, after
CannObserv/watcher#358).

co-core resolves from the co-pypi Artifact Registry index, which takes the
username ``oauth2accesstoken`` and an access token as the password. The host has
no gcloud, so the wrapper mints the token from the co-pypi-reader key at
``/etc/processor-deploy/co-pypi-reader.json`` (``PROCESSOR_CO_PYPI_KEY``
overrides it) and ``exec``s the wrapped ``uv`` with the pair in that child's
environment alone.

What is pinned here, because a credential leak is silent:

* **The token is never printed**, on success or on any failure path: not on
  stdout or stderr, not in a log record, not in argv.
* **Only ``uv`` is wrapped.** Anything else is refused before a token exists.
* **The parent's environment is not changed.** The pair goes into the child's
  copy; ``VIRTUAL_ENV`` from the ``uv run`` the wrapper runs under is dropped.
* **The wrapper's own dependency is locked** (#79's rule): the ``co-pypi``
  group, ``--frozen``, in the wrapper's own checkout (``--project``), while the
  wrapped ``uv`` runs where it was called from (``scripts/deploy.sh``: the
  release).

No network: the mint is a seam. The real one ran against the operator's key
before the relock (#121's plan, step 1).
"""

import importlib.util
import logging
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SHELL = REPO / "scripts" / "co-pypi.sh"
SCRIPT = REPO / "scripts" / "co_pypi_token.py"

# Standalone, like every script here: loaded from its path.
_spec = importlib.util.spec_from_file_location("co_pypi_token", SCRIPT)
co_pypi_token = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(co_pypi_token)

#: Stands in for a minted token; no output of any test may contain it.
TOKEN = "ya29.SENTINEL-TOKEN-never-printed"
DEFAULT_KEY = "/etc/processor-deploy/co-pypi-reader.json"
OTHER_KEY = "/home/someone/co-pypi-reader.json"


class Exec:
    """Records the ``os.execvpe`` call instead of replacing the process."""

    def __init__(self, raises: OSError | None = None) -> None:
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []
        self.raises = raises

    def __call__(self, file: str, args: list[str], env: dict[str, str]) -> None:
        self.calls.append((file, list(args), dict(env)))
        if self.raises:
            raise self.raises


class Mint:
    """A mint that records the key path it was given."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.paths: list[str] = []
        self.raises = raises

    def __call__(self, key_path: str) -> str:
        self.paths.append(key_path)
        if self.raises:
            raise self.raises
        return TOKEN


@pytest.fixture
def environ() -> dict[str, str]:
    return {
        "VIRTUAL_ENV": "/home/exedev/processor/.venv",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "KEPT": "1",
    }


def run(argv, environ, *, mint=None, execvpe=None):
    mint = mint or Mint()
    execvpe = execvpe or Exec()
    code = co_pypi_token.main(argv, environ, mint=mint, execvpe=execvpe)
    return code, mint, execvpe


def assert_never_printed(capsys, caplog) -> None:
    out, err = capsys.readouterr()
    assert TOKEN not in out
    assert TOKEN not in err
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


# -- the success path ---------------------------------------------------------


def test_execs_uv_with_the_pair_in_the_child_environment(environ, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    before = dict(environ)
    code, mint, execvpe = run(["uv", "lock"], environ)

    assert code is None  # exec replaced the process; nothing returns in production
    assert mint.paths == [DEFAULT_KEY]
    [(file, args, env)] = execvpe.calls
    assert (file, args) == ("uv", ["uv", "lock"])
    assert env[co_pypi_token.USERNAME_VAR] == "oauth2accesstoken"
    assert env[co_pypi_token.PASSWORD_VAR] == TOKEN
    assert TOKEN not in args
    assert env["KEPT"] == "1"
    assert env["PATH"] == environ["PATH"]
    assert "VIRTUAL_ENV" not in env, "the wrapped uv must find its own project's .venv"
    assert environ == before, "the pair goes into the child's copy, never the parent's"
    assert_never_printed(capsys, caplog)


def test_the_default_key_is_the_deploy_hosts():
    """Decision 1 (operator, 2026-11-06): root:exedev 640 in a root:exedev 750
    directory, outside /etc/processor/ (root 700, the service's)."""
    assert co_pypi_token.DEFAULT_KEY == DEFAULT_KEY


def test_the_key_path_can_be_overridden(environ):
    environ[co_pypi_token.KEY_VAR] = OTHER_KEY
    _, mint, execvpe = run(["uv", "lock"], environ)
    assert mint.paths == [OTHER_KEY]
    assert execvpe.calls[0][2][co_pypi_token.KEY_VAR] == OTHER_KEY


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_override_means_the_default(environ, value):
    environ[co_pypi_token.KEY_VAR] = value
    _, mint, _ = run(["uv", "lock"], environ)
    assert mint.paths == [DEFAULT_KEY]


def test_the_override_is_not_googles_variable():
    """exedev sets no GOOGLE_APPLICATION_CREDENTIALS, and one set shell-wide would
    hand every Google client in a dev shell, tests included, an identity."""
    assert co_pypi_token.KEY_VAR == "PROCESSOR_CO_PYPI_KEY"


def test_a_uv_named_by_path_is_wrapped(environ):
    code, _, execvpe = run(["/usr/local/bin/uv", "sync", "--locked"], environ)
    assert code is None
    assert execvpe.calls[0][1] == ["/usr/local/bin/uv", "sync", "--locked"]


def test_an_inherited_pair_is_replaced_not_kept(environ):
    environ[co_pypi_token.PASSWORD_VAR] = "stale"
    environ[co_pypi_token.USERNAME_VAR] = "someone"
    _, _, execvpe = run(["uv", "lock"], environ)
    env = execvpe.calls[0][2]
    assert env[co_pypi_token.PASSWORD_VAR] == TOKEN
    assert env[co_pypi_token.USERNAME_VAR] == "oauth2accesstoken"


# -- refusals: before any token exists ----------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["bash", "-c", "env"],
        ["env"],
        ["uvx", "something"],
        ["python", "-c", "import os; print(os.environ)"],
        ["./uv-but-not"],
    ],
)
def test_refuses_anything_but_uv(argv, environ, capsys, caplog):
    code, mint, execvpe = run(argv, environ)
    assert code == 2
    assert mint.paths == [], "no token is minted for a command that will not get it"
    assert execvpe.calls == []
    assert "usage: bash scripts/co-pypi.sh uv" in capsys.readouterr().err
    assert_never_printed(capsys, caplog)


# -- failures: after the mint, or of it ---------------------------------------


def test_a_failed_mint_exits_1_and_runs_nothing(environ, capsys, caplog):
    mint = Mint(raises=RuntimeError("invalid_grant: account not found"))
    code, _, execvpe = run(["uv", "lock"], environ, mint=mint)
    assert code == 1
    assert execvpe.calls == []
    err = capsys.readouterr().err
    assert "could not mint a co-pypi access token" in err
    assert "invalid_grant" in err, "the cause is named, so the operator can act on it"
    assert DEFAULT_KEY in err
    assert "docs/PROVISIONING.md" in err, "where the key comes from"
    assert_never_printed(capsys, caplog)


def test_a_missing_key_is_a_failed_mint(environ, capsys, caplog):
    mint = Mint(raises=FileNotFoundError(2, "No such file or directory", DEFAULT_KEY))
    code, _, execvpe = run(["uv", "sync", "--locked"], environ, mint=mint)
    assert code == 1
    assert execvpe.calls == []
    assert "FileNotFoundError" in capsys.readouterr().err
    assert_never_printed(capsys, caplog)


def test_a_failed_exec_exits_127_without_the_environment(environ, capsys, caplog):
    execvpe = Exec(raises=FileNotFoundError(2, "No such file or directory"))
    code, _, _ = run(["uv", "lock"], environ, execvpe=execvpe)
    assert code == 127
    err = capsys.readouterr().err
    assert "could not run uv" in err
    assert_never_printed(capsys, caplog)


# -- the real mint, without the network ---------------------------------------


def test_mint_token_reads_the_key_with_a_cloud_platform_scope(monkeypatch):
    seen = {}

    class Credentials:
        token = None

        def refresh(self, request):
            seen["request"] = request
            self.token = TOKEN

    def from_service_account_file(path, scopes):
        seen["path"], seen["scopes"] = path, scopes
        return Credentials()

    monkeypatch.setattr(
        co_pypi_token.service_account.Credentials,
        "from_service_account_file",
        from_service_account_file,
    )
    assert co_pypi_token.mint_token(DEFAULT_KEY) == TOKEN
    assert seen["path"] == DEFAULT_KEY
    assert seen["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]
    assert isinstance(seen["request"], co_pypi_token.Request)


def test_mint_token_refuses_an_empty_token(monkeypatch):
    class Credentials:
        token = None

        def refresh(self, request):
            pass

    monkeypatch.setattr(
        co_pypi_token.service_account.Credentials,
        "from_service_account_file",
        lambda path, scopes: Credentials(),
    )
    with pytest.raises(RuntimeError, match="no access token"):
        co_pypi_token.mint_token(DEFAULT_KEY)


# -- the names uv reads -------------------------------------------------------


def test_the_variables_are_uvs_spelling_of_the_index_name():
    """uv reads ``UV_INDEX_<NAME>_USERNAME``/``_PASSWORD``, the index name
    upper-cased with ``-`` as ``_``: rename the index and this follows."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    [index] = [i for i in pyproject["tool"]["uv"]["index"] if i["name"] == "co-pypi"]
    stem = "UV_INDEX_" + index["name"].upper().replace("-", "_")
    assert co_pypi_token.USERNAME_VAR == f"{stem}_USERNAME"
    assert co_pypi_token.PASSWORD_VAR == f"{stem}_PASSWORD"


# -- the shell front-end ------------------------------------------------------


def test_the_shell_front_end_runs_the_module_from_the_lock(tmp_path):
    """``bash scripts/co-pypi.sh uv sync`` is one ``uv run`` of the locked
    ``co-pypi`` group in the wrapper's own checkout, from wherever it is called."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "calls.log"
    (stubs / "uv").write_text(f'#!/bin/bash\nprintf "%s\\n" "$PWD" "$@" > {log}\n')
    (stubs / "uv").chmod(0o755)
    elsewhere = tmp_path / "release"
    elsewhere.mkdir()
    result = subprocess.run(
        ["bash", str(SHELL), "uv", "sync", "--locked"],
        env={"PATH": f"{stubs}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        cwd=elsewhere,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        str(elsewhere),
        "run",
        "--project",
        str(REPO),
        "--frozen",
        "--only-group",
        "co-pypi",
        "python",
        str(SCRIPT),
        "uv",
        "sync",
        "--locked",
    ]
