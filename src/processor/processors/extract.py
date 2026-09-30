"""The ``extract`` transform: raw bytes + one ``source_spec`` → canonical text (spec §3).

Pure: no I/O. The golden-digest parity target against Watcher's local extraction.
"""

from co_core.pure.extract.canonical import processor_version

# Bump by hand only when Processor's own logic (config merging, dispatch) changes
# output in a way co-core's version cannot see (spec §5). Matches Watcher's
# LOCAL_EXTRACTION_GENERATION: the dispatch here is Watcher's, lifted into co-core.
LOCAL_GENERATION = 1
PROCESSOR_VERSION = processor_version(LOCAL_GENERATION)
