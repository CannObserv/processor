"""Security floors on the lock that a re-lock must not undo (#71).

pypdf 6.21.2 fixes 11 advisories against 6.19.0 (infinite loops, long runtimes, large
memory, unvalidated JPEG loading), on attacker-reachable PDFs; opentelemetry-api 1.45.1
fixes unbounded tracestate parsing (GHSA-xcq7-p3pf-p88h). ``[tool.uv]
constraint-dependencies`` holds both floors, beside certifi's (``test_tls_trust.py``).
"""

import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FLOORS = {"pypdf": (6, 21, 2), "opentelemetry-api": (1, 45, 1)}


def _parts(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def _locked(name: str) -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    (v,) = [p["version"] for p in lock["package"] if p["name"] == name]
    return v


def test_parts_compare_numerically() -> None:
    assert _parts("6.19.0") < _parts("6.21.2") < _parts("6.100.0")


@pytest.mark.parametrize("name", FLOORS)
def test_the_lock_is_at_the_floor(name: str) -> None:
    assert _parts(_locked(name)) >= FLOORS[name], _locked(name)


@pytest.mark.parametrize("name", FLOORS)
def test_the_venv_is_at_the_floor(name: str) -> None:
    assert _parts(version(name)) >= FLOORS[name], version(name)


@pytest.mark.parametrize("name", FLOORS)
def test_pyproject_constrains_the_floor(name: str) -> None:
    uv = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"]
    floor = ".".join(map(str, FLOORS[name]))
    assert f"{name}>={floor}" in uv["constraint-dependencies"]
