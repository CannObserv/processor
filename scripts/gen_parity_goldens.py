"""Generate the parity goldens from **co-core's pure extract API, directly** (#47).

Parity must not be self-referential: Processor's tests assert its pure core
(``processors/extract.py``) reproduces what co-core's extractors compute for the same
bytes, spec and essence (spec §7). This script therefore imports nothing from
Processor, only the stdlib and ``co_core.pure.extract``, and spells the path itself.
That path is the one Watcher took at ``a5d6f34``: essence → ``extractor_for_essence`` with
``{**extraction_config_from_spec, **extraction_overrides_for_essence}`` →
``canonical_text``. Since watcher#350 nothing else extracts, so co-core is the oracle.

The goldens v1 were Watcher's (``_extract_and_fingerprint``, Watcher ``a5d6f34``,
co-core 0.19.7). This generator reproduced every one byte for byte. ``v1_provenance``
records that, and every run carries it forward. Run it at a co-core bump only, never to
make a failing parity test pass. It prints each moved digest for the bump note.

    uv run python scripts/gen_parity_goldens.py tests/fixtures/parity
"""

import json
import sys
from importlib.metadata import version
from pathlib import Path

from co_core.pure.extract.canonical import canonical_text, canonical_text_fingerprint
from co_core.pure.extract.dispatch import extractor_for_essence
from co_core.pure.extract.extraction_defaults import extraction_config_from_spec
from co_core.pure.extract.media_type import extraction_overrides_for_essence
from co_core.pure.extract.spec_fingerprint import spec_fingerprint, spec_schema_version

GENERATED_BY = "scripts/gen_parity_goldens.py (co-core's pure extract API, direct; #47)"


def golden(raw: bytes, essence: str | None, spec: dict) -> dict:
    """One case's golden: the text's digest and size, and the spec's identity."""
    config = {**extraction_config_from_spec(spec), **extraction_overrides_for_essence(essence)}
    chunks = extractor_for_essence(essence).extract(raw, config=config).chunks
    try:
        spec_fp = spec_fingerprint(spec)
    except ValueError:  # SpecFingerprintError: a diagnostic, reported as None
        spec_fp = None
    return {
        # Empty text still fingerprints (sha256 of b""), as Watcher's did.
        "content_fingerprint": canonical_text_fingerprint(chunks),
        "content_size_bytes": len(canonical_text(chunks)),
        "spec_fingerprint": spec_fp,
        "spec_schema_version": spec_schema_version(spec),
    }


def goldens(corpus: Path) -> dict[str, dict]:
    """Every case in ``corpus/cases.json``, by id."""
    cases = json.loads((corpus / "cases.json").read_text())
    return {
        case["id"]: golden(
            (corpus / "inputs" / case["input"]).read_bytes(),
            case["media_type"],
            case["source_spec"],
        )
        for case in cases
    }


def moved(old: dict[str, dict], new: dict[str, dict]) -> list[str]:
    """Each field that moved, each case removed or added: the bump note's list."""
    lines = []
    for case_id in sorted(old.keys() | new.keys()):
        if case_id not in new:
            lines.append(f"{case_id}: removed")
        elif case_id not in old:
            lines.append(f"{case_id}: added")
        else:
            for field in sorted(old[case_id].keys() | new[case_id].keys()):
                before, after = old[case_id].get(field), new[case_id].get(field)
                if before != after:
                    lines.append(f"{case_id} {field}: {before} -> {after}")
    return lines


def main(corpus: Path) -> list[str]:
    """Rewrite ``corpus/goldens.json`` on the installed co-core; return what moved."""
    path = corpus / "goldens.json"
    previous = json.loads(path.read_text())
    if "v1_provenance" not in previous:
        raise SystemExit(f"{path} has no v1_provenance: refusing to lose the v1 record")
    new = goldens(corpus)
    doc = {
        "co_core": version("co-core"),
        "generated_by": GENERATED_BY,
        "goldens": new,
        "v1_provenance": previous["v1_provenance"],
    }
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return moved(previous["goldens"], new)


if __name__ == "__main__":
    changes = main(Path(sys.argv[1]).resolve())
    print(f"co-core {version('co-core')}: {len(changes)} moved")
    for line in changes:
        print(f"  {line}")
