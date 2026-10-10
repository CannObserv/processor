"""The co-core pin is exact, and Processor owns it (spec D5, amended 2026-10-10; §5).

A bump changes ``processor_version`` on every fact, and Watcher re-baselines on it, so
it must be a deliberate act: edit ``EXPECTED`` here, in the same commit as the pin,
with the goldens regenerated co-core-direct and every moved digest in the bump note.
"""

import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

from processor.processors.extract import LOCAL_GENERATION, PROCESSOR_VERSION

EXPECTED = "0.19.7"
ROOT = Path(__file__).resolve().parent.parent
PINNED = ("co-core", "co-core-aio", "co-core-sync")


def _lock_versions() -> dict[str, str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    return {p["name"]: p["version"] for p in lock["package"]}


@pytest.mark.parametrize("name", PINNED)
def test_pyproject_pins_exactly(name: str) -> None:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    (spec,) = [d for d in deps if d.split("[")[0].split("=")[0] == name]
    assert spec.endswith(f"=={EXPECTED}"), spec


@pytest.mark.parametrize("name", PINNED)
def test_lock_resolves_the_pin(name: str) -> None:
    assert _lock_versions()[name] == EXPECTED


@pytest.mark.parametrize("name", PINNED)
def test_installed_matches_the_pin(name: str) -> None:
    assert version(name) == EXPECTED


def test_processor_version_is_co_core_plus_generation() -> None:
    assert LOCAL_GENERATION == 1  # Watcher's LOCAL_EXTRACTION_GENERATION at the cutover
    assert PROCESSOR_VERSION == f"{EXPECTED}+1"
