"""``scripts/gen_parity_goldens.py``: the goldens, from co-core's pure extract API (#47).

Since watcher#350 nothing else extracts, so the oracle is co-core itself, driven by a
script that shares no code with Processor. On the pinned co-core the generator must
reproduce the committed goldens byte for byte. On 0.19.7 those are Watcher's v1
numbers; after a bump, they are what the bump note listed.
"""

import ast
import importlib.util
import json
import shutil
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "gen_parity_goldens.py"
CORPUS = ROOT / "tests" / "fixtures" / "parity"
GOLDENS = json.loads((CORPUS / "goldens.json").read_text())
_spec = importlib.util.spec_from_file_location("gen_parity_goldens", SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _imported_modules() -> set[str]:
    tree = ast.parse(SCRIPT.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            names.add(node.module)
    return names


def test_imports_nothing_but_the_stdlib_and_co_cores_pure_extract() -> None:
    # Cross-implementation: the goldens can't pass by Processor agreeing with itself.
    for name in _imported_modules():
        root = name.split(".")[0]
        if root in sys.stdlib_module_names:
            continue
        assert name.startswith("co_core.pure.extract"), name


def test_reproduces_every_committed_golden() -> None:
    # Trap 1 (#47): on 0.19.7 this is Watcher's a5d6f34 output, byte for byte.
    assert gen.goldens(CORPUS) == GOLDENS["goldens"]


def test_metadata_names_co_core_the_generator_and_the_v1_record() -> None:
    assert GOLDENS["co_core"] == version("co-core")
    assert GOLDENS["generated_by"] == gen.GENERATED_BY
    v1 = GOLDENS["v1_provenance"]
    assert v1["watcher_commit"] == "a5d6f3417f4b559449a3f5d82f5c40438a4cc18d"
    assert v1["watcher_processor_version"] == "0.19.7+1"
    assert v1["co_core"] == "0.19.7"
    assert "watcher_processor_version" not in GOLDENS


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    copy = tmp_path / "parity"
    shutil.copytree(CORPUS / "inputs", copy / "inputs")
    shutil.copy(CORPUS / "cases.json", copy)
    shutil.copy(CORPUS / "goldens.json", copy)
    return copy


def test_regenerating_on_the_pin_moves_nothing_and_keeps_the_file(corpus: Path) -> None:
    assert gen.main(corpus) == []
    assert (corpus / "goldens.json").read_text() == (CORPUS / "goldens.json").read_text()


def test_lists_every_moved_digest(corpus: Path) -> None:
    doc = json.loads((corpus / "goldens.json").read_text())
    doc["goldens"]["csv"]["content_fingerprint"] = "sha256:old"
    doc["goldens"]["csv"]["content_size_bytes"] = 1
    doc["goldens"]["gone"] = doc["goldens"].pop("xlsx")
    (corpus / "goldens.json").write_text(json.dumps(doc))

    moved = gen.main(corpus)

    real = GOLDENS["goldens"]["csv"]
    assert moved == [
        f"csv content_fingerprint: sha256:old -> {real['content_fingerprint']}",
        f"csv content_size_bytes: 1 -> {real['content_size_bytes']}",
        "gone: removed",
        "xlsx: added",
    ]
    rewritten = json.loads((corpus / "goldens.json").read_text())
    assert rewritten["goldens"] == GOLDENS["goldens"]
    assert rewritten["v1_provenance"] == GOLDENS["v1_provenance"]


def test_refuses_to_drop_the_v1_record(corpus: Path) -> None:
    doc = json.loads((corpus / "goldens.json").read_text())
    del doc["v1_provenance"]
    (corpus / "goldens.json").write_text(json.dumps(doc))

    with pytest.raises(SystemExit, match="v1_provenance"):
        gen.main(corpus)
