"""The co-core pin is exact, and Processor owns it (spec D5, amended 2026-10-10; §5).

A bump changes ``processor_version`` on every fact, and Watcher re-baselines on it, so
it must be a deliberate act: edit ``EXPECTED`` here, in the same commit as the pin,
``scripts/build_wheelhouse.sh``'s default tag and the wheelhouse digests (#87), with
the goldens regenerated co-core-direct and every moved digest in the bump note.

Whether a bump is notice or coordination turns on the contract (§5). Essence
resolution is outside it only because Processor never calls co-core's resolvers
(``resolve_dispatch_essence``, ``extension_media_type``), and a test holds that
premise (processor#57).

``processor_version`` is blind to everything else in the lock, so a fact is a pure
function of (bytes, spec, ``processor_version``) only while the extraction stack holds
still between co-core bumps. ``EXTRACTION_STACK`` makes moving it deliberate too: a
re-lock that moves it outside a co-core bump also bumps ``LOCAL_GENERATION`` (§5,
amended 2026-10-24, processor#71).
"""

import ast
import re
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

from processor.processors.extract import LOCAL_GENERATION, PROCESSOR_VERSION

EXPECTED = "0.38.0"
ROOT = Path(__file__).resolve().parent.parent
PINNED = ("co-core", "co-core-aio", "co-core-sync")
SRC = ROOT / "src" / "processor"
RESOLVERS = frozenset({"resolve_dispatch_essence", "extension_media_type"})
GENERATION = 2  # 1 was Watcher's LOCAL_EXTRACTION_GENERATION at the cutover; 2: #71
# The transitive closure of co-core's ``extract`` extra in uv.lock: what turns bytes
# into text beside co-core itself. Moved together with GENERATION (#71: pypdf 6.21.2).
EXTRACTION_STACK = {
    "beautifulsoup4": "4.15.0",
    "et-xmlfile": "2.0.0",
    "html5lib": "1.1",
    "lxml": "6.1.3",
    "openpyxl": "3.1.5",
    "pypdf": "6.21.2",
    "python-jsonpath": "2.2.2",
    "six": "1.17.0",
    "soupsieve": "2.10",
    "typing-extensions": "4.16.0",
    "webencodings": "0.6.1",
}
STACK_MOVED = (
    "The extraction stack moved: {moved}. processor_version can't see it, and Watcher "
    "reads a moved fingerprint under an unchanged version as a content change. Outside "
    "a co-core bump, bump LOCAL_GENERATION and GENERATION; either way update "
    "EXTRACTION_STACK, and give Watcher notice (spec §5; DEPLOYMENT.md, Extraction "
    "dependencies; processor#71)."
)


def _resolver_uses(source: str) -> list[str]:
    # Code, not text: an import, a name or an attribute. A docstring may quote them.
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.alias):
            name = node.name.rsplit(".", 1)[-1]
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        else:
            continue
        if name in RESOLVERS:
            found.append(f"line {node.lineno}: {name}")
    return found


def _lock_packages() -> dict[str, dict]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    return {p["name"]: p for p in lock["package"]}


def _lock_versions() -> dict[str, str]:
    return {name: p["version"] for name, p in _lock_packages().items()}


def _extraction_stack() -> dict[str, str]:
    # Walk the lock from co-core's extract extra, so a new transitive dependency counts.
    packages = _lock_packages()
    todo = [d["name"] for d in packages["co-core"]["optional-dependencies"]["extract"]]
    seen: set[str] = set()
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo.extend(d["name"] for d in packages[name].get("dependencies", []))
    return {name: packages[name]["version"] for name in sorted(seen)}


def _moved(actual: dict[str, str | None]) -> dict[str, tuple[str | None, str | None]]:
    names = EXTRACTION_STACK.keys() | actual.keys()
    return {
        n: (EXTRACTION_STACK.get(n), actual.get(n))
        for n in sorted(names)
        if EXTRACTION_STACK.get(n) != actual.get(n)
    }


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


def test_the_wheelhouse_digests_move_with_the_pin() -> None:
    # #87: a bump that moves the pin and not [tool.processor.wheelhouse-sha256] would
    # fail every sync and deploy; one that keeps an old row pins a file no lock names.
    # tests/test_sync_wheelhouse.py holds the table to uv.lock; this, to EXPECTED.
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    table = pyproject["tool"]["processor"]["wheelhouse-sha256"]
    assert sorted(table) == sorted(
        f"{name.replace('-', '_')}-{EXPECTED}-py3-none-any.whl" for name in PINNED
    )


def test_the_wheelhouse_builder_defaults_to_the_pin() -> None:
    # A fresh checkout runs it with no tag (AGENTS.md); a stale default builds wheels
    # the lock can't resolve.
    script = (ROOT / "scripts" / "build_wheelhouse.sh").read_text()
    (default,) = re.findall(r'^tag="\$\{1:-(v[^}]+)\}"$', script, re.MULTILINE)
    assert default == f"v{EXPECTED}"


def test_processor_version_is_co_core_plus_generation() -> None:
    assert LOCAL_GENERATION == GENERATION
    assert PROCESSOR_VERSION == f"{EXPECTED}+{GENERATION}"


def test_the_lock_holds_the_extraction_stack() -> None:
    moved = _moved(dict(_extraction_stack()))
    assert not moved, STACK_MOVED.format(moved=moved)


def test_the_venv_holds_the_extraction_stack() -> None:
    moved = _moved({name: version(name) for name in EXTRACTION_STACK})
    assert not moved, STACK_MOVED.format(moved=moved)


def test_the_stack_walk_sees_the_extract_extra_and_its_dependencies() -> None:
    # Control: the walk reaches co-core's direct extras and a dependency of theirs
    # (html5lib -> webencodings), and leaves co-core's base deps (dateparser) out.
    stack = _extraction_stack()
    assert {"pypdf", "lxml", "html5lib", "webencodings"} <= stack.keys()
    assert "dateparser" not in stack


@pytest.mark.parametrize(
    ("source", "caught"),
    [
        ("from co_core.pure.extract import resolve_dispatch_essence", True),
        ("from co_core.pure.extract.media_type import extension_media_type as ext", True),
        ("import co_core.pure.extract.media_type as m\nm.resolve_dispatch_essence(t, u)", True),
        ("essence = resolve_dispatch_essence", True),
        ('"""Watcher calls ``resolve_dispatch_essence``; Processor never does."""', False),
        ("# resolve_dispatch_essence is the issuer's", False),
        ("from co_core.pure.extract.dispatch import extractor_for_essence", False),
    ],
    ids=[
        "from-import",
        "from-import-as",
        "attribute",
        "name",
        "docstring",
        "comment",
        "dispatch-call",
    ],
)
def test_the_guard_catches_code_not_text(source: str, caught: bool) -> None:
    assert bool(_resolver_uses(source)) is caught


def test_processor_never_resolves_a_dispatch_essence() -> None:
    # The premise of §5's rule: the issuer resolves on its own pin, Processor dispatches
    # what arrives. If this fails, a change to essence resolution is a contract change
    # again, and the coordination rule comes back with it.
    files = sorted(SRC.rglob("*.py"))
    assert SRC / "processors" / "extract.py" in files
    uses = {str(f.relative_to(ROOT)): u for f in files if (u := _resolver_uses(f.read_text()))}
    assert not uses, (
        f"Processor resolves a dispatch essence itself: {uses}. A change to essence "
        "resolution is then a contract change, coordinated with Watcher, not notice: "
        "restore it to the contract (spec §5, D5; AGENTS.md; DEPLOYMENT.md step 4; "
        "processor#57)."
    )
