"""Run one ``uv`` command with a co-pypi access token in its environment (#121).

    bash scripts/co-pypi.sh uv lock
    bash scripts/co-pypi.sh uv sync --locked

``co-core``, ``co-core-aio`` and ``co-core-sync`` resolve from the co-pypi
Artifact Registry index (``pyproject.toml``'s ``[[tool.uv.index]]``). It takes
HTTP basic auth: the username ``oauth2accesstoken`` and an OAuth2 access token
as the password, which uv reads from ``UV_INDEX_CO_PYPI_USERNAME`` /
``UV_INDEX_CO_PYPI_PASSWORD``. The host has no gcloud, so this mints the token
from the co-pypi-reader key (``roles/artifactregistry.reader`` on co-pypi) at
``/etc/processor-deploy/co-pypi-reader.json``, or at ``PROCESSOR_CO_PYPI_KEY``,
and ``exec``s the command with the pair set in that child's environment alone.
Shaped after CannObserv/watcher#358's wrapper; where the key lives and how it
gets there: docs/PROVISIONING.md § GCP provisioning.

**The token is never printed, logged, put in argv or written anywhere.** It
exists in this process's memory and in the wrapped ``uv``'s environment, and
dies with it (an hour's lifetime at most). Only ``uv`` is wrapped: anything else
is refused before a token is minted. ``VIRTUAL_ENV``, set by the ``uv run``
this runs under, is dropped, so the wrapped ``uv`` finds its own project's
``.venv``. **Accepted limit:** whatever that ``uv`` spawns inherits its
environment, the pair included: the PEP 517 build backend for the project
itself. uv has no per-index credential scoping short of a keyring or netrc
file; the bound is the identity's read-only role and the token's hour. It also
inherits the ``PATH`` that ``uv run`` sets, the wrapper's checkout's ``.venv/bin``
first: ``scripts/deploy.sh`` names ``--python`` explicitly, and a release venv
built through here runs ``/usr/bin/python3.12`` (measured, #121).

Its own dependency (google-auth) is uv.lock's ``co-pypi`` group, run
``--frozen`` in this script's checkout (#79's rule; ``scripts/co-pypi.sh``), so
nothing in the process that holds the key is resolved at run time.

Only a command that downloads from the index needs this: ``uv lock``, and a
``uv sync`` that needs a co-core file neither installed nor cached.
``uv audit --frozen`` doesn't.

Exit codes: the wrapped command's own · ``2`` usage or a refused command ·
``1`` the mint failed (the key included) · ``127`` the command could not be run.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account

USERNAME_VAR = "UV_INDEX_CO_PYPI_USERNAME"
PASSWORD_VAR = "UV_INDEX_CO_PYPI_PASSWORD"
USERNAME = "oauth2accesstoken"
# The deploy host's key (operator, 2026-11-06): root:exedev 640 in a
# root:exedev 750 directory. Not /etc/processor/, which is root 700, the service's.
DEFAULT_KEY = "/etc/processor-deploy/co-pypi-reader.json"
# Not GOOGLE_APPLICATION_CREDENTIALS: set shell-wide, that would give every
# Google client in the shell an identity.
KEY_VAR = "PROCESSOR_CO_PYPI_KEY"
# Artifact Registry's documented scope for a token-as-password read; the
# identity's IAM role, artifactregistry.reader, is what bounds it.
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
# Set by the `uv run` this runs under, pointing at the wrapper's checkout's venv;
# inherited, a wrapped `uv sync` elsewhere (a release) would warn about it.
DROPPED = ("VIRTUAL_ENV",)
USAGE = "usage: bash scripts/co-pypi.sh uv <command> [args...]"


def mint_token(key_path: str) -> str:
    """An access token for the service account whose key is at *key_path*."""
    credentials = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
    credentials.refresh(Request())
    if not credentials.token:
        raise RuntimeError("the token endpoint returned no access token")
    return credentials.token


def main(
    argv: Sequence[str],
    environ: Mapping[str, str],
    *,
    mint: Callable[[str], str] = mint_token,
    execvpe: Callable[[str, list[str], dict[str, str]], object] = os.execvpe,
) -> int | None:
    """Exec *argv* (a ``uv`` command) with the co-pypi pair in its environment.

    Returns an exit code on failure; on success *execvpe* replaces the process,
    so in production nothing returns.
    """
    if not argv or Path(argv[0]).name != "uv":
        print(f"error: co-pypi wraps uv alone. {USAGE}", file=sys.stderr)
        return 2

    key_path = environ.get(KEY_VAR, "").strip() or DEFAULT_KEY
    try:
        token = mint(key_path)
    except Exception as exc:  # broad by design: any failure here runs nothing
        # The exception is the token endpoint's or the key file's, never the token.
        print(
            f"error: could not mint a co-pypi access token from {key_path}:"
            f" {type(exc).__name__}: {exc}. The key: docs/PROVISIONING.md § GCP provisioning.",
            file=sys.stderr,
        )
        return 1

    env = {k: v for k, v in environ.items() if k not in DROPPED}
    env[USERNAME_VAR] = USERNAME
    env[PASSWORD_VAR] = token
    try:
        execvpe(argv[0], list(argv), env)
    except OSError as exc:
        # Names the command and errno, never `env`.
        print(f"error: could not run {argv[0]}: {exc.strerror or exc}", file=sys.stderr)
        return 127
    return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
