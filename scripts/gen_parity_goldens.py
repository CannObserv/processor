"""Generate the parity goldens by running **Watcher's own** extraction path.

Parity must be cross-implementation, not self-referential: Processor's tests assert
its pure core reproduces what Watcher's local pipeline computes for the same bytes,
spec and essence (spec §7). This script therefore imports nothing from Processor. It
runs inside a Watcher checkout, whose ``src`` package it drives exactly as Watcher's
``process_watched_item`` does (essence → ``ServiceRegistry.get_extractor`` +
``extraction_overrides_for_essence`` → ``_extract_and_fingerprint``).

Watcher's ``pipeline`` imports procrastinate, hence psycopg; ``psycopg-binary``
supplies libpq without touching the host. No database is contacted.

    cd <watcher checkout>          # its .wheelhouse holding the pinned co-core wheels
    uv sync --frozen
    uv run --frozen --with 'psycopg-binary>=3,<4' \\
        python <processor>/scripts/gen_parity_goldens.py <processor>/tests/fixtures/parity
"""

import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from src.core.media_type import extraction_overrides_for_essence
from src.core.registry import ServiceRegistry
from src.core.validators import extraction_generation
from src.workers.pipeline import _extract_and_fingerprint


def main(corpus: Path) -> None:
    cases = json.loads((corpus / "cases.json").read_text())
    registry = ServiceRegistry()
    goldens: dict[str, dict] = {}
    for case in cases:
        raw = (corpus / "inputs" / case["input"]).read_bytes()
        essence = case["media_type"]
        outcome = _extract_and_fingerprint(
            raw,
            [case["source_spec"]],
            extractor=registry.get_extractor(essence),
            extra_config=extraction_overrides_for_essence(essence),
        )
        goldens[case["id"]] = {
            "content_fingerprint": outcome.content_fingerprint,
            "content_size_bytes": outcome.content_size_bytes,
            "spec_fingerprint": outcome.spec_fingerprint,
            "spec_schema_version": outcome.schema_version,
        }
    watcher_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    doc = {
        "generated_by": "scripts/gen_parity_goldens.py (Watcher's _extract_and_fingerprint)",
        "watcher_commit": watcher_commit,
        "co_core": version("co-core"),
        "watcher_processor_version": extraction_generation(),
        "goldens": goldens,
    }
    (corpus / "goldens.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"{len(goldens)} goldens from watcher@{watcher_commit[:7]} on co-core {doc['co_core']}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
