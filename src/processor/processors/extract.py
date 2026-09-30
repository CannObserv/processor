"""The ``extract`` transform: raw bytes + one ``source_spec`` → canonical text (spec §3).

Pure: no I/O. The golden-digest parity target against Watcher's local extraction
(``tests/fixtures/parity``). Extractor exceptions propagate unclassified; the child
runner reports them as ``extraction_error``.
"""

from dataclasses import dataclass

from co_core.pure.extract.canonical import (
    canonical_text,
    canonical_text_fingerprint,
    processor_version,
)
from co_core.pure.extract.dispatch import extractor_for_essence
from co_core.pure.extract.extraction_defaults import extraction_config_from_spec
from co_core.pure.extract.media_type import extraction_overrides_for_essence
from co_core.pure.extract.spec_fingerprint import spec_fingerprint, spec_schema_version

# Bump by hand only when Processor's own logic (config merging, dispatch) changes
# output in a way co-core's version cannot see (spec §5). Matches Watcher's
# LOCAL_EXTRACTION_GENERATION: the dispatch here is Watcher's, lifted into co-core.
LOCAL_GENERATION = 1
PROCESSOR_VERSION = processor_version(LOCAL_GENERATION)


@dataclass(frozen=True)
class ExtractOutcome:
    """What a ``processing_complete`` fact reports, plus the bytes to store."""

    text: bytes
    output_digest: str | None  # sha256:<hex>; None when empty
    empty: bool  # canonical_text(chunks) == b"", not "no chunks" (a blank PDF page is one)
    spec_fingerprint: str | None  # None when co-core cannot derive one
    spec_schema_version: int
    processor_version: str


def extract(raw: bytes, media_type: str | None, source_spec: dict) -> ExtractOutcome:
    """Extract canonical text from ``raw`` under one spec, dispatched on ``media_type``.

    ``media_type`` is the essence the issuer already resolved (cannobserv#486 D1):
    never re-resolved or sniffed here. Unknown or ``None`` dispatches to HTML.
    """
    config = {
        **extraction_config_from_spec(source_spec),
        **extraction_overrides_for_essence(media_type),
    }
    chunks = extractor_for_essence(media_type).extract(raw, config=config).chunks
    text = canonical_text(chunks)
    empty = text == b""
    return ExtractOutcome(
        text=text,
        output_digest=None if empty else canonical_text_fingerprint(chunks),
        empty=empty,
        spec_fingerprint=_spec_fingerprint_or_none(source_spec),
        spec_schema_version=spec_schema_version(source_spec),
        processor_version=PROCESSOR_VERSION,
    )


def _spec_fingerprint_or_none(source_spec: dict) -> str | None:
    """co-core's spec identity, or ``None`` if it raises (``SpecFingerprintError`` is a
    ``ValueError``) — the field is a diagnostic, as on Watcher's side."""
    try:
        return spec_fingerprint(source_spec)
    except ValueError:
        return None
