"""Transforms, keyed by ``command.processor`` (spec §3).

Each entry is a pure, importable function (the spawn child imports it by name).
Org adapters join later as entries here or as spec-selected variants.
"""

from collections.abc import Callable

from processor.processors.extract import ExtractOutcome, extract

TRANSFORMS: dict[str, Callable[[bytes, str | None, dict], ExtractOutcome]] = {
    "extract": extract,
}
