#!/usr/bin/env bash
# Run one uv command with a co-pypi access token in its environment (#121).
#
#   bash scripts/co-pypi.sh uv lock
#   bash scripts/co-pypi.sh uv sync --locked
#
# The token is minted by scripts/co_pypi_token.py from the co-pypi-reader key
# and exported to the wrapped uv alone; it is never printed. That module's own
# dependency is uv.lock's co-pypi group, run --frozen in this checkout
# (--project), never resolved at run time (#79). The wrapped uv runs where this
# was called from: scripts/deploy.sh calls it in a release. Details: that
# module's docstring and docs/DEPENDENCIES.md § Bumping co-core.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec uv run --project "$(dirname "$HERE")" --frozen --only-group co-pypi \
  python "$HERE/co_pypi_token.py" "$@"
