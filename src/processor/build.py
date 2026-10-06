"""The build id: what the running code reports itself as (#2; the cohort's R10).

A release (``/srv/processor/releases/<build>/``, built by ``scripts/deploy.sh``)
holds its venv and a ``REVISION`` file, the commit's 12-character short SHA, which
the deploy writes last. Anywhere else (a checkout, a worktree) there is none: ``dev``.
"""

import sys
from pathlib import Path


def build_id(prefix: Path | None = None) -> str:
    """``REVISION`` beside the venv at ``prefix`` (default ``sys.prefix``), else ``dev``."""
    revision = Path(sys.prefix if prefix is None else prefix).parent / "REVISION"
    try:
        return revision.read_text().strip() or "dev"
    except FileNotFoundError:
        return "dev"
