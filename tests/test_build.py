"""The build id: the release's ``REVISION``, or ``dev`` outside a release (#2)."""

import sys
from pathlib import Path

from processor.build import build_id


def test_a_release_reports_its_revision(tmp_path: Path) -> None:
    # /srv/processor/releases/<build>/.venv is sys.prefix; REVISION sits beside it.
    (tmp_path / ".venv").mkdir()
    (tmp_path / "REVISION").write_text("0123456789ab\n")
    assert build_id(tmp_path / ".venv") == "0123456789ab"


def test_a_checkout_reports_dev(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    assert build_id(tmp_path / ".venv") == "dev"


def test_an_empty_revision_is_dev(tmp_path: Path) -> None:
    (tmp_path / "REVISION").write_text("\n")
    assert build_id(tmp_path / ".venv") == "dev"


def test_the_default_is_this_interpreters_venv() -> None:
    expected = Path(sys.prefix).parent / "REVISION"
    assert build_id() == (expected.read_text().strip() if expected.exists() else "dev")
