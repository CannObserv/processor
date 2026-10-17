---
title: Dead-letter give-ups publish dead_lettered; co-core 0.32.0 → 0.37.0 (#21)
date: 2026-10-17
status: per #21's hand-off from epic #24 (2026-10-17) and the v0.37.0 release comment
---

# dead_lettered

## Problem

Since #17 a command dead-lettered at the cap gets a terminal `extraction_error`, so Watcher blames the item's spec for Processor's fault. cannobserv v0.37.0 (`49d42aca`) adds `dead_lettered` to `ProcessingFailureReason` (cannobserv#536): terminal, no re-fetch, scoped to a command the processor dead-letters. Upstream's scope:
- **`handler.publish_gave_up` moves to it** (the consumer's cap: an escape past the handler, or a non-transient publish refusal).
- **The handler's own cap stays `extraction_error`** (every attempt timed out, crashed, or raised outside the parser). Upstream's accepted limit.
- After a refused ack, the fact that went out stands; no `dead_lettered` follows.

## The catch-up reading (0.32.0 → 0.37.0)

Checked in code (`git diff v0.32.0 v0.37.0 -- packages/`), not only the CHANGELOG:
- **Contract models** (`changes.py`): `ProcessingFailureReason` gains `"dead_lettered"`; `ProcessingFailedEvent`'s docstring documents it and the cap's `extraction_error`. `ContentProcessCommand`, `ProcessingCompleteEmit`, `ProcessingFailedEmit`'s fields and validators: unchanged. The rest is the watch pair (#563/#564), `FetchPolicyState.generation`, `WatchStatusState` levels: none of it read by Processor.
- **Bus**: `envelope.py` gains five event types and their key rules (additive); `streams.py` gains `INFO_WATCH_EVENTS` / `INFO_WATCH_CHECK` and comments. `dead_letter`, `exceptions`, `effects.bus`, `co_core_aio.bus`: unchanged.
- **Extract** (whole package), `canonical_text`, `spec_fingerprint`, `resolve_dispatch_essence`: unchanged.
- **Blob store** (`util.blobstore`, `util.hashing`, `co_core_sync.drivers.blobstore`): unchanged.
- **Dependencies**: each package's `pyproject.toml` moves only its version and the sibling floor (`>=0.37,<0.38`).
- **CHANGELOG** 0.33.0–0.36.0: Downstream lines are cli-only. 0.37.0: additive; no security entries in range.

So the only code change is the token.

## Approach

**Pin.** `pyproject.toml` (three packages), `EXPECTED` in `tests/test_pin.py`, `scripts/build_wheelhouse.sh`'s default tag, `uv lock`. `LOCAL_GENERATION` stays 1, so `processor_version` = `0.37.0+1`.

**Token.** `publish_gave_up` builds its fact with `"dead_lettered"`. `detail` keeps its `dead-lettered: <DLQ reason>` prefix (the DLQ and the fact agree; Watcher stores `detail` as written).

**Tests** (red first):
- `test_handler.py`: `test_publish_gave_up_publishes_one_terminal_extraction_error` → `…_dead_lettered`.
- `test_consumer.py`: give-ups through `publish_gave_up` (the escape cap, the transiently refused fact, the retried dead-letter, the failed dead-letter at the cap, the dead-letter refused every time) assert `dead_lettered`. The handler's cap (`test_three_strikes_then_a_terminal_failure`, `test_a_failed_ack_at_the_cap_keeps_the_count`) stays `extraction_error`. The refused-ack case still adds no fact.

**Goldens.** Regenerate from co-core; expect no move (the extract package is byte-identical). A move means stop and explain. Real corpus passes unchanged.

**Docs.**
- Spec: an amendment entry; §4 rows 313 and 315 and the #17 give-up prose; D5 "(0.32.0 today)"; §2's dependency row; §5's pin bullet plus a 0.32.0 → 0.37.0 sub-bullet; §7's pin-test line; the contract quick reference heading (and `dead_lettered` noted under `ProcessingFailureReason`); Open Question 2's range.
- Docstrings: `handler.py` module and `publish_gave_up`; `consumer.py` module.
- DEPLOYMENT.md: the dead-letter line (`extraction_error` → `dead_lettered`); the bump procedure's catch-up example.
- AGENTS.md: the pin line.

**Off-repo (operator approval for each post).** Watcher notice on CannObserv/watcher#386 before the deploy: `processor_version` `0.37.0+1`, goldens moved: none, `dead_lettered` now published on the dead-letter path, an un-upgraded Watcher decodes it (`reason: str`). Then the bump note on #21.

## Steps

1. Red: pin test, handler/consumer token tests.
2. Green: wheelhouse at v0.37.0, pin, `uv lock`, the token. Regenerate goldens; record moved list.
3. Docs.
4. Full gate (`ruff check`, `ruff format --check`, `pytest` incl. integration on the scratch Redis, `uv lock --locked`); PR with `Refs #21`.
5. After FF merge: `git pull --ff-only`; Watcher notice (approved); `scripts/deploy.sh`; live checks: `starting` shows `processor_version: 0.37.0+1`, each source's digest unchanged across the bump, `NRestarts=0`.

## Open questions

None blocking.
