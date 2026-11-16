"""Security floors on the lock that a re-lock must not undo (#71, #112, #138).

pypdf 6.21.2 fixed 11 advisories against 6.19.0 (infinite loops, long runtimes, large
memory, unvalidated JPEG loading), and 6.21.4 7 more against 6.21.2 (memory and runtime
blowups, two infinite loops), on attacker-reachable PDFs; opentelemetry-api 1.45.1
fixes unbounded tracestate parsing (GHSA-xcq7-p3pf-p88h) and 1.45.2 quadratic-time
traceparent parsing (GHSA-mf5v-w3w3-f6cg, #138); pyasn1 0.6.5 fixes quadratic-time
BER/DER decoding (#112). ``[tool.uv] constraint-dependencies`` holds those floors,
beside certifi's (``test_tls_trust.py``).

soupsieve 4.1 fixes three selector DoS advisories against 2.10 (GHSA-mmhr-3c5m-f64f,
GHSA-6655-6qjp-jx7x, GHSA-7g44-g9rc-x53w; #138), but co-core 0.38.0's ``extract`` extra
caps it ``<3``, and a constraint only narrows. So its floor is an
``override-dependencies`` entry, which replaces co-core's requirement until a co-core
bump to 0.46.1 or later declares ``>=4.1`` itself. The guard below fails then, so the
bump removes the override instead of carrying it.
"""

import re
import tomllib
from importlib.metadata import requires, version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FLOORS = {
    "pypdf": (6, 21, 4),
    "opentelemetry-api": (1, 45, 2),
    "pyasn1": (0, 6, 5),
    "soupsieve": (4, 1),
}
# Floors co-core's own requirement caps below, so they override it (#138).
OVERRIDDEN = {"soupsieve"}


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


def _uv() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"]


def _co_core_floor(name: str) -> tuple[int, ...] | None:
    # co-core's own ``extract`` requirement on ``name``, as installed: its ``>=`` floor.
    for req in requires("co-core") or []:
        spec, _, marker = req.partition(";")
        # The name ends where a version, extras or marker starts: not at a ``-``.
        if re.match(rf"{re.escape(name)}\s*(?:[<>=!~\[(]|$)", spec) and "extract" in marker:
            floor = re.search(r">=\s*([\d.]+)", spec)
            return _parts(floor.group(1)) if floor else None
    return None


@pytest.mark.parametrize("name", sorted(FLOORS.keys() - OVERRIDDEN))
def test_pyproject_constrains_the_floor(name: str) -> None:
    floor = ".".join(map(str, FLOORS[name]))
    assert f"{name}>={floor}" in _uv()["constraint-dependencies"]


@pytest.mark.parametrize("name", sorted(OVERRIDDEN))
def test_pyproject_overrides_the_floor(name: str) -> None:
    floor = ".".join(map(str, FLOORS[name]))
    (entry,) = [d for d in _uv().get("override-dependencies", []) if d.startswith(name)]
    assert f">={floor}" in entry, entry
    assert name not in " ".join(_uv()["constraint-dependencies"])


@pytest.mark.parametrize("name", sorted(OVERRIDDEN))
def test_co_core_still_needs_the_override(name: str) -> None:
    # Fails once co-core's own requirement reaches the floor (co-core >= 0.46.1 for
    # soupsieve): remove the override and this row, and keep the floor as a constraint
    # if it should outlive co-core's (DEPENDENCIES.md, Bumping co-core).
    own = _co_core_floor(name)
    assert own is not None, f"co-core no longer declares {name} under its extract extra"
    assert own < FLOORS[name], (
        f"co-core now floors {name} at {own} itself: the override-dependencies entry "
        "is redundant. Remove it, and its OVERRIDDEN row (processor#138)."
    )


@pytest.mark.parametrize(
    ("req", "floor"),
    [
        ("soupsieve<3,>=2.0; extra == 'extract'", (2, 0)),
        ("soupsieve>=4.1,<5; extra == 'extract'", (4, 1)),
        ("soupsieve<5; extra == 'extract'", None),
    ],
)
def test_the_guard_reads_co_core_s_floor(
    monkeypatch: pytest.MonkeyPatch, req: str, floor: tuple[int, ...] | None
) -> None:
    # A name that only starts with soupsieve, and soupsieve under another extra: neither counts.
    others = ["soupsieve-extra>=9.0; extra == 'extract'", "soupsieve>=1; extra == 'x'"]
    monkeypatch.setattr(f"{__name__}.requires", lambda _: [*others, req])
    assert _co_core_floor("soupsieve") == floor
