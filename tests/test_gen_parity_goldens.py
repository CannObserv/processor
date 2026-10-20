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


def _append_case(
    corpus: Path, case_id: str, name: str, body: bytes, essence: str, spec: dict
) -> None:
    cases = json.loads((corpus / "cases.json").read_text())
    (corpus / "inputs" / name).write_bytes(body)
    cases.append({"id": case_id, "input": name, "media_type": essence, "source_spec": spec})
    (corpus / "cases.json").write_text(json.dumps(cases))


def test_a_spec_co_core_refuses_is_recorded_as_refused(corpus: Path) -> None:
    # #29: the refusal is co-core's output, so it is a golden like any digest.
    spec = {"extraction": {"algorithm": "regex", "selector": "x"}}
    _append_case(corpus, "regex", "page.html", b"<p>x</p>", "text/html", spec)

    moved = gen.main(corpus)

    assert moved == ["regex: added"]
    doc = json.loads((corpus / "goldens.json").read_text())
    assert doc["goldens"]["regex"] == {"refused": "UnsupportedExtractionError"}


def test_a_case_that_starts_or_stops_being_refused_is_listed(corpus: Path) -> None:
    # The bump note's view of e.g. cannobserv#557, when HTML started honouring xpath (#37).
    doc = json.loads((corpus / "goldens.json").read_text())
    was_refused = doc["goldens"]["refused_css_on_xml"]
    was_extracted = doc["goldens"]["xml_xpath"]
    doc["goldens"]["refused_css_on_xml"] = was_extracted
    doc["goldens"]["xml_xpath"] = was_refused
    (corpus / "goldens.json").write_text(json.dumps(doc))

    moved = gen.main(corpus)

    assert "refused_css_on_xml refused: None -> UnsupportedExtractionError" in moved
    assert "xml_xpath refused: UnsupportedExtractionError -> None" in moved
    assert (
        f"xml_xpath content_fingerprint: None -> {was_extracted['content_fingerprint']}"
    ) in moved


def test_a_malformed_xml_body_is_not_a_refusal(corpus: Path) -> None:
    # A parse failure is a plain ValueError: it names its case, never becomes a golden.
    spec = {"extraction": {"algorithm": "full_page"}}
    _append_case(corpus, "broken_xml", "broken.xml", b"<rss><channel>", "application/xml", spec)
    before = (corpus / "goldens.json").read_text()

    with pytest.raises(RuntimeError, match="case 'broken_xml'"):
        gen.main(corpus)

    assert (corpus / "goldens.json").read_text() == before


def test_an_extractor_that_raises_names_its_case_and_writes_nothing(corpus: Path) -> None:
    cases = json.loads((corpus / "cases.json").read_text())
    (corpus / "inputs" / "broken.pdf").write_bytes(b"this is not a pdf")
    cases.append(
        {
            "id": "broken",
            "input": "broken.pdf",
            "media_type": "application/pdf",
            "source_spec": {"extraction": {"algorithm": "full_page"}},
        }
    )
    (corpus / "cases.json").write_text(json.dumps(cases))
    before = (corpus / "goldens.json").read_text()

    with pytest.raises(RuntimeError, match="case 'broken'") as raised:
        gen.main(corpus)

    assert raised.value.__cause__ is not None
    assert (corpus / "goldens.json").read_text() == before
