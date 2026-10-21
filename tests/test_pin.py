"""The co-core pin is exact, and Processor owns it (spec D5, amended 2026-10-10; §5).

A bump changes ``processor_version`` on every fact, and Watcher re-baselines on it, so
it must be a deliberate act: edit ``EXPECTED`` here, in the same commit as the pin
and ``scripts/build_wheelhouse.sh``'s default tag, with the goldens regenerated
co-core-direct and every moved digest in the bump note.

Whether a bump is notice or coordination turns on the contract (§5). Essence
resolution is outside it only because Processor never calls co-core's resolvers
(``resolve_dispatch_essence``, ``extension_media_type``), and a test holds that
premise (processor#57).
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


def test_the_wheelhouse_builder_defaults_to_the_pin() -> None:
    # A fresh checkout runs it with no tag (AGENTS.md); a stale default builds wheels
    # the lock can't resolve.
    script = (ROOT / "scripts" / "build_wheelhouse.sh").read_text()
    (default,) = re.findall(r'^tag="\$\{1:-(v[^}]+)\}"$', script, re.MULTILINE)
    assert default == f"v{EXPECTED}"


def test_processor_version_is_co_core_plus_generation() -> None:
    assert LOCAL_GENERATION == 1  # Watcher's LOCAL_EXTRACTION_GENERATION at the cutover
    assert PROCESSOR_VERSION == f"{EXPECTED}+1"


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
