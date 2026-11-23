"""Security floors on the lock that a re-lock must not undo (#71, #112, #138).

pypdf 6.21.2 fixed 11 advisories against 6.19.0 (infinite loops, long runtimes, large
memory, unvalidated JPEG loading), and 6.21.4 7 more against 6.21.2 (memory and runtime
blowups, two infinite loops), on attacker-reachable PDFs; opentelemetry-api 1.45.1
fixes unbounded tracestate parsing (GHSA-xcq7-p3pf-p88h) and 1.45.2 quadratic-time
traceparent parsing (GHSA-mf5v-w3w3-f6cg, #138); pyasn1 0.6.5 fixes quadratic-time
BER/DER decoding (#112). ``[tool.uv] constraint-dependencies`` holds those floors,
beside certifi's (``test_tls_trust.py``).

soupsieve 4.1 fixes three selector DoS advisories against 2.10 (GHSA-mmhr-3c5m-f64f,
GHSA-6655-6qjp-jx7x, GHSA-7g44-g9rc-x53w; #138). co-core 0.38.0's ``extract`` extra
capped it ``<3``, and a constraint only narrows, so #138 put its floor in
``override-dependencies``, which replaces co-core's requirement. co-core 0.46.1 and later
declare ``>=4.1,<5`` themselves, so the bump to 0.51.1 (#140) removed the override: the
floor is co-core's own (``CO_CORE_FLOORED``), and a test holds it there. The override
route stays for the next floor co-core caps below: an ``OVERRIDDEN`` row, whose guard
fails once co-core's own requirement admits the floor.
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
# Floors co-core's own requirement caps below, so they override it. None since #140
# (soupsieve was #138's).
OVERRIDDEN: set[str] = set()
# Floors co-core's own ``extract`` requirement already holds, so neither a constraint nor
# an override names them (soupsieve: co-core >= 0.46.1 declares >=4.1,<5; #140).
CO_CORE_FLOORED = {"soupsieve"}


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


def _names(spec: str, name: str) -> bool:
    # The name ends where a version, extras or marker starts: not at a ``-`` or a letter.
    return re.match(rf"{re.escape(name)}\s*(?:[<>=!~\[(;]|$)", spec.strip()) is not None


def _co_core_spec(name: str) -> str | None:
    # co-core's own ``extract`` requirement on ``name``, as installed, without its marker.
    for req in requires("co-core") or []:
        spec, _, marker = req.partition(";")
        if _names(spec, name) and re.search(r"extra\s*==\s*['\"]extract['\"]", marker):
            return spec.strip()
    return None


def _pad(v: tuple[int, ...]) -> tuple[int, ...]:
    # So 4.1.0 and 4.1 compare equal.
    return v + (0,) * (4 - len(v))


def _admits(spec: str, floor: tuple[int, ...]) -> bool:
    # Whether the requirement allows ``floor`` or later: only an upper bound can refuse it.
    for op, v in re.findall(r"(<=|<)\s*([\d.]+)", spec):
        cap = _pad(_parts(v))
        if cap < _pad(floor) or (op == "<" and cap == _pad(floor)):
            return False
    return True


def _floors(spec: str, floor: tuple[int, ...]) -> bool:
    # Whether the requirement alone refuses everything below ``floor``: a lower bound at
    # or above it. ``>v`` counts only when v is at least the floor (>4.0 admits 4.0.1).
    return any(
        _pad(_parts(v)) >= _pad(floor) for _, v in re.findall(r"(>=|==|~=|>)\s*([\d.]+)", spec)
    )


def _requirement_name(spec: str) -> str:
    return re.match(r"\s*([A-Za-z0-9._-]+)", spec).group(1).lower()


@pytest.mark.parametrize("name", sorted(FLOORS.keys() - OVERRIDDEN - CO_CORE_FLOORED))
def test_pyproject_constrains_the_floor(name: str) -> None:
    floor = ".".join(map(str, FLOORS[name]))
    assert f"{name}>={floor}" in _uv()["constraint-dependencies"]


def test_each_override_is_an_overridden_floor_co_core_still_needs() -> None:
    # One test, not one per row: OVERRIDDEN is empty between overrides, and a
    # parametrization over nothing would be a skip, which reads as something unrun.
    entries = {_requirement_name(d): d for d in _uv().get("override-dependencies", [])}
    assert entries.keys() == OVERRIDDEN, (
        f"override-dependencies names {sorted(entries)}, OVERRIDDEN {sorted(OVERRIDDEN)}: "
        "an override replaces co-core's requirement, so each one is a floor here, and is "
        "removed with its row once co-core admits it (DEPENDENCIES.md, Bumping co-core)."
    )
    constraints = " ".join(_uv()["constraint-dependencies"])
    for name, entry in entries.items():
        assert f">={'.'.join(map(str, FLOORS[name]))}" in entry, entry
        assert name not in constraints
        # Fails once co-core's own requirement admits the floor (a raised floor, or only
        # a lifted cap). Then remove the override and its row; if co-core's own floor is
        # lower, keep ours as a constraint (as soupsieve's override went at #140).
        spec = _co_core_spec(name)
        assert spec is not None, f"co-core no longer declares {name} under its extract extra"
        assert not _admits(spec, FLOORS[name]), (
            f"co-core's own requirement ({spec}) now admits {name} {FLOORS[name]}: the "
            "override-dependencies entry is redundant. Remove it and its OVERRIDDEN row, "
            "with a constraint floor if co-core's is lower (processor#138, #140)."
        )


@pytest.mark.parametrize("name", sorted(CO_CORE_FLOORED))
def test_co_core_holds_the_floor_itself(name: str) -> None:
    # Why no constraint names it. If a co-core bump lowers its own floor, add one.
    spec = _co_core_spec(name)
    assert spec is not None, f"co-core no longer declares {name} under its extract extra"
    assert _floors(spec, FLOORS[name]), (
        f"co-core's own requirement ({spec}) no longer floors {name} at {FLOORS[name]}: "
        "put the floor in constraint-dependencies and move the name out of "
        "CO_CORE_FLOORED (DEPENDENCIES.md, Bumping co-core; processor#140)."
    )
    assert name not in " ".join(_uv()["constraint-dependencies"])
    assert name not in " ".join(_uv().get("override-dependencies", []))


def test_the_guard_finds_co_core_s_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    # A name that only starts with soupsieve, and soupsieve under another extra, even
    # one that only starts with extract: none counts.
    others = [
        "soupsieve-extra>=9.0; extra == 'extract'",
        "soupsieve>=1; extra == 'x'",
        "soupsieve>=1; extra == 'extraction'",
    ]
    req = "soupsieve<3,>=2.0; extra == 'extract'"
    monkeypatch.setattr(f"{__name__}.requires", lambda _: [*others, req])
    assert _co_core_spec("soupsieve") == "soupsieve<3,>=2.0"
    monkeypatch.setattr(f"{__name__}.requires", lambda _: others)
    assert _co_core_spec("soupsieve") is None


@pytest.mark.parametrize(
    ("spec", "named"),
    [
        ("soupsieve>=4.1,<5", True),
        ("soupsieve [x] >=4.1", True),
        ("soupsieve", True),
        ("soupsieve-extra>=4.1", False),
        ("soupsieves>=4.1", False),
    ],
)
def test_a_requirement_names_the_package_exactly(spec: str, named: bool) -> None:
    assert _names(spec, "soupsieve") is named


@pytest.mark.parametrize(
    ("spec", "admits"),
    [
        ("soupsieve<3,>=2.0", False),  # co-core 0.38.0: the override was needed (#138)
        ("soupsieve<4.1,>=2.0", False),  # a cap at the floor excludes it
        ("soupsieve<4.1.0", False),  # however it is spelled
        ("soupsieve<=4.0", False),
        ("soupsieve>=4.1,<5", True),  # co-core 0.46.1 to 0.51.1 (#140)
        ("soupsieve>=2.0,<5", True),  # only the cap lifted: a constraint would do
        ("soupsieve<=4.1", True),
        ("soupsieve>=2.0", True),
        ("soupsieve", True),
    ],
)
def test_the_guard_reads_whether_co_core_admits_the_floor(spec: str, admits: bool) -> None:
    assert _admits(spec, (4, 1)) is admits


@pytest.mark.parametrize(
    ("spec", "floors"),
    [
        ("soupsieve>=4.1,<5", True),  # co-core 0.51.1 (#140)
        ("soupsieve<5,>=4.1.0", True),  # however it is spelled
        ("soupsieve>=4.2", True),
        ("soupsieve==4.1.3", True),
        ("soupsieve~=4.1", True),
        ("soupsieve>4.1", True),
        ("soupsieve<3,>=2.0", False),  # co-core 0.38.0
        ("soupsieve>=4.0,<5", False),  # a floor below ours: add a constraint
        ("soupsieve>4.0", False),  # admits 4.0.1
        ("soupsieve<=4.1", False),  # an upper bound only
        ("soupsieve!=4.1", False),
        ("soupsieve", False),
    ],
)
def test_the_guard_reads_whether_co_core_floors_it(spec: str, floors: bool) -> None:
    assert _floors(spec, (4, 1)) is floors


@pytest.mark.parametrize(
    ("spec", "name"),
    [
        ("soupsieve>=4.1,<5", "soupsieve"),
        ("  SoupSieve [x] >=4.1", "soupsieve"),
        ("opentelemetry-api>=1.45.2", "opentelemetry-api"),
        ("zope.interface", "zope.interface"),
    ],
)
def test_an_override_s_name_is_its_requirement_s(spec: str, name: str) -> None:
    assert _requirement_name(spec) == name
