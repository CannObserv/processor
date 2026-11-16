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


def _admits(spec: str, floor: tuple[int, ...]) -> bool:
    # Whether the requirement allows ``floor`` or later: only an upper bound can refuse it.
    width = 4  # pad, so 4.1.0 and 4.1 compare equal
    floor = floor + (0,) * (width - len(floor))
    for op, v in re.findall(r"(<=|<)\s*([\d.]+)", spec):
        cap = _parts(v) + (0,) * (width - len(_parts(v)))
        if cap < floor or (op == "<" and cap == floor):
            return False
    return True


@pytest.mark.parametrize("name", sorted(FLOORS.keys() - OVERRIDDEN))
def test_pyproject_constrains_the_floor(name: str) -> None:
    floor = ".".join(map(str, FLOORS[name]))
    assert f"{name}>={floor}" in _uv()["constraint-dependencies"]


@pytest.mark.parametrize("name", sorted(OVERRIDDEN))
def test_pyproject_overrides_the_floor(name: str) -> None:
    floor = ".".join(map(str, FLOORS[name]))
    (entry,) = [d for d in _uv().get("override-dependencies", []) if _names(d, name)]
    assert f">={floor}" in entry, entry
    assert name not in " ".join(_uv()["constraint-dependencies"])


@pytest.mark.parametrize("name", sorted(OVERRIDDEN))
def test_co_core_still_needs_the_override(name: str) -> None:
    # Fails once co-core's own requirement admits the floor: at co-core >= 0.46.1 for
    # soupsieve (>=4.1,<5), or at a release that only lifts the cap (>=2.0,<5). Then
    # remove the override and its OVERRIDDEN row; if co-core's own floor is lower, keep
    # ours as a constraint-dependencies floor (DEPENDENCIES.md, Bumping co-core).
    spec = _co_core_spec(name)
    assert spec is not None, f"co-core no longer declares {name} under its extract extra"
    assert not _admits(spec, FLOORS[name]), (
        f"co-core's own requirement ({spec}) now admits {name} {FLOORS[name]}: the "
        "override-dependencies entry is redundant. Remove it and its OVERRIDDEN row, "
        "with a constraint floor if co-core's is lower (processor#138)."
    )


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
        ("soupsieve<3,>=2.0", False),  # co-core 0.38.0: the override is needed
        ("soupsieve<4.1,>=2.0", False),  # a cap at the floor excludes it
        ("soupsieve<4.1.0", False),  # however it is spelled
        ("soupsieve<=4.0", False),
        ("soupsieve>=4.1,<5", True),  # co-core 0.46.1
        ("soupsieve>=2.0,<5", True),  # only the cap lifted: a constraint would do
        ("soupsieve<=4.1", True),
        ("soupsieve>=2.0", True),
        ("soupsieve", True),
    ],
)
def test_the_guard_reads_whether_co_core_admits_the_floor(spec: str, admits: bool) -> None:
    assert _admits(spec, (4, 1)) is admits
