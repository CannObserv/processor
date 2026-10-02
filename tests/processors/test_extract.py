"""The pure ``extract`` core against goldens computed by Watcher's own pipeline (spec §7).

``tests/fixtures/parity/goldens.json`` is produced by ``scripts/gen_parity_goldens.py``
running Watcher's ``_extract_and_fingerprint`` over the same bytes, spec and essence,
so a pass here is cross-implementation parity, not a self-check.
"""

import builtins
import gzip
import hashlib
import json
import socket
from importlib.metadata import version
from pathlib import Path

import pytest

from processor.processors import TRANSFORMS
from processor.processors.extract import PROCESSOR_VERSION, ExtractOutcome, extract

CORPUS = Path(__file__).resolve().parent.parent / "fixtures" / "parity"
CASES = json.loads((CORPUS / "cases.json").read_text())
GOLDENS = json.loads((CORPUS / "goldens.json").read_text())
# Real inputs: Watcher's export (watcher#325 issuecomment-5958475061), verbatim, and
# the raw blobs it names, copied from gs://co-gcs-blobs before Replicator's TTL took
# them (#16). Fingerprints are Watcher's recorded ones, re-extracted by Watcher at
# 0e14f39 (0.19.7+1) — never Processor's.
REAL = CORPUS / "real"
REAL_EXPORT = json.loads((REAL / "export.json").read_text())
REAL_ITEMS = REAL_EXPORT["items"]
EMPTY_SHA256 = "sha256:" + hashlib.sha256(b"").hexdigest()


def _run(case: dict) -> ExtractOutcome:
    raw = (CORPUS / "inputs" / case["input"]).read_bytes()
    return extract(raw, case["media_type"], case["source_spec"])


def test_goldens_were_generated_on_the_pinned_co_core() -> None:
    # A co-core bump must regenerate the goldens from Watcher on the new version.
    assert GOLDENS["co_core"] == version("co-core")


def test_every_case_has_a_golden() -> None:
    assert {c["id"] for c in CASES} == set(GOLDENS["goldens"])


def test_processor_version_matches_watchers() -> None:
    assert PROCESSOR_VERSION == GOLDENS["watcher_processor_version"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_parity_with_watcher(case: dict) -> None:
    golden = GOLDENS["goldens"][case["id"]]
    outcome = _run(case)

    assert outcome.empty is (golden["content_size_bytes"] == 0)
    assert len(outcome.text) == golden["content_size_bytes"]
    if outcome.empty:
        # Watcher fingerprints b"" and then refuses it; the fact carries no digest.
        assert golden["content_fingerprint"] == EMPTY_SHA256
        assert outcome.text == b""
        assert outcome.output_digest is None
    else:
        assert outcome.output_digest == golden["content_fingerprint"]
    assert outcome.spec_fingerprint == golden["spec_fingerprint"]
    assert outcome.spec_schema_version == golden["spec_schema_version"]
    assert outcome.processor_version == PROCESSOR_VERSION


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_digest_covers_the_returned_bytes(case: dict) -> None:
    outcome = _run(case)
    if not outcome.empty:
        assert outcome.output_digest == "sha256:" + hashlib.sha256(outcome.text).hexdigest()


def test_extractor_errors_propagate() -> None:
    # The core does not classify; the child reports a raise as extraction_error.
    with pytest.raises(Exception):
        extract(b"this is not a pdf", "application/pdf", {"extraction": {}})


def test_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = [(CORPUS / "inputs" / c["input"]).read_bytes() for c in CASES]
    for case, raw in zip(CASES, inputs, strict=True):  # warm lazy imports first
        extract(raw, case["media_type"], case["source_spec"])

    def refuse(*_a, **_k):
        raise AssertionError("the pure core performed I/O")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    for case, raw in zip(CASES, inputs, strict=True):
        extract(raw, case["media_type"], case["source_spec"])


def test_registry_routes_extract() -> None:
    assert TRANSFORMS == {"extract": extract}


def _real_raw(item: dict) -> bytes:
    return gzip.decompress((REAL / f"{item['input_digest']}.bin.gz").read_bytes())


def test_real_corpus_is_the_whole_export() -> None:
    assert REAL_EXPORT["processor_version_local"] == PROCESSOR_VERSION
    assert len(REAL_ITEMS) == 9
    blobs = {p.name.removesuffix(".bin.gz") for p in REAL.glob("*.bin.gz")}
    assert blobs == {item["input_digest"] for item in REAL_ITEMS}


@pytest.mark.parametrize("item", REAL_ITEMS, ids=[i["input_digest"][:12] for i in REAL_ITEMS])
def test_parity_with_watcher_on_real_inputs(item: dict) -> None:
    raw = _real_raw(item)
    assert hashlib.sha256(raw).hexdigest() == item["input_digest"]

    outcome = extract(raw, item["media_type"], item["source_spec"])

    assert not outcome.empty
    assert outcome.output_digest == item["recorded_fingerprint"]
    assert outcome.spec_fingerprint == item["spec_fingerprint"]
