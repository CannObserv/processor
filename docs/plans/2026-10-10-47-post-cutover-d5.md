---
title: Close out the cutover; D5 after watcher#350 (#47)
date: 2026-10-10
status: per #47 and its hand-off from epic #24 (2026-10-10); decisions made by the operator
---

# Post-cutover D5

## Problem

watcher#350 deleted Watcher's extraction. Two rules no longer guard anything: "co-core in lockstep with Watcher" (D5, AGENTS.md line 21) and "goldens come from Watcher's own code" (AGENTS.md line 55). `scripts/gen_parity_goldens.py` imports three Watcher symbols that #350 deleted.

## Decisions (operator, 2026-10-10)

1. **D5: Processor owns the co-core pin.** It coordinates with Watcher only when a bump changes the contract (the `content.process` / `content.derived` models, `canonical_text` semantics, `resolve_dispatch_essence`). It gives Watcher notice whenever `processor_version` moves.
2. **Goldens come from co-core directly.** Today's `goldens.json` is frozen as the v1 record. At a bump, regenerate from co-core's pure extract API, never through `processors/extract.py`, and list every moved digest in the bump note.

## Approach

**Rewrite `scripts/gen_parity_goldens.py` in place**, so the path stays the same and `tests/test_drift.py` needs no change. It runs in Processor's own venv and imports only the stdlib and `co_core.pure.extract.*`: `extractor_for_essence`, `extraction_config_from_spec`, `extraction_overrides_for_essence`, `canonical_text`, `canonical_text_fingerprint`, `spec_fingerprint`, `spec_schema_version`. It follows the path Watcher took at `a5d6f34`:
- config is `{**from_spec, **overrides}`;
- `content_fingerprint` is the sha256 of `canonical_text`, also when that is empty;
- a `spec_fingerprint` that raises `ValueError` becomes `None`.

**Trap 1 is a standing test.** On the pinned co-core, the generator's output equals the committed `goldens` byte for byte. On 0.19.7 it reproduces Watcher's v1 numbers, and after a bump it catches a hand-edited golden.

**Cross-implementation (trap 2).** A test walks the script's AST: nothing from `processor` or `src`, and every third-party import is under `co_core.pure.extract`.

**Metadata (trap 3).** The new shape is `co_core`, `generated_by` (the generator's identity) and `v1_provenance`, which holds the old `generated_by`, `watcher_commit`, `watcher_processor_version` and `co_core`, plus a note that the numbers were reproduced co-core-direct on 2026-10-10. The generator carries `v1_provenance` forward on every run, and refuses to run without it. The v1 block is moved in once by hand, and the digests are not touched. `watcher_processor_version` leaves the top level. The test that read it is re-anchored on `PROCESSOR_VERSION == f"{GOLDENS['co_core']}+{LOCAL_GENERATION}"`.

**The bump note.** The generator prints every moved digest (case, field, old → new), along with added and removed cases, so that the note can be pasted from its output.

**Docs.**
- Spec: D5's row, the §5 bump bullet, §6 step 5 marked done (watcher#326's gate verdict 2026-10-06 and soak verdict 2026-10-09; watcher#350), §7's golden source, and an amendment entry.
- AGENTS.md: lines 21 and 55. Keep "never regenerate to pass".
- DEPLOYMENT.md: the co-core bump procedure.
- `tests/test_pin.py`, `tests/processors/test_extract.py` and `processors/extract.py`: docstrings and comments.

**Off-repo.**
- #29 and #37 get their re-entry tests restated.
- A Watcher notice of decision 1 is drafted and posted only on approval.
- #1 is closed after the merge.

## Not changed

- The real corpus (`real/`, #16).
- `src/`: untouched, so there is no deploy. The drift check counts any `src/` edit as code that runs, comments included. The comments in `extract.py` still hold as history: the v1 numbers are Watcher's, and `LOCAL_GENERATION` matched Watcher's.
