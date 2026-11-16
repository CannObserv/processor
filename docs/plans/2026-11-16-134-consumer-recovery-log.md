---
title: The consumer loop logs its recovery (#134)
date: 2026-11-16
status: per #134's body, the brief (6928735158) and its check (6928995289)
---

# The consumer loop logs its recovery

## Problem

`Consumer.run()` logs every failed step (`consumer loop error; backing off`), but the next good step resets `backoff` and `backing_off` silently (`consumer.py:139-140`). After a broker outage the journal shows when failures stopped, never that the loop came back.

## Approach (Replicator's shape, replicator#193)

- `run()`: a `failures` count beside `backoff`. `+1` in the `except`. After a good step, if non-zero: one INFO `consumer loop recovered`, `extra={"after_failures": n}`, then reset to 0.
- Logged at the reset (after `step()` returns), not earlier: a step is the loop's unit of having come back. Ordering after an outage: WARNINGs, maybe `consuming` (boot path) and `ack: complete`, then `recovered`.
- No record on a good step after none, and none when a stop ends a backoff.
- Name: `consumer loop recovered`, so `consumer loop` matches both ends; never `consuming` (`scripts/deploy.sh`'s readiness proof). Shared key with Replicator: `after_failures`.
- No change to `last_progress`, `backing_off`, liveness, backoff timing, `is_transient`, the formatter.
- Docs: `consumer.py` docstring (`run` bullet); spec Amended entry + §3 Health clause; NETWORK.md § Tailnet flaps (signature, query block: an INFO line `-p warning` hides, the window's end); MONITORING.md § Liveness one line.

## Tests (red first, `tests/test_consumer.py`, integration-marked)

Scripted `step` over the real one, as `test_a_backoff_is_progress_and_says_so`:
- fail ×3 then succeed → exactly one record, `after_failures == 3`, INFO;
- fail ×2, succeed, fail ×1, succeed → two records, counts 2 then 1;
- succeed only → none;
- fail, then stop during the backoff → none.

Boot path: `test_a_broker_name_that_does_not_resolve_yet_is_retried` at INFO asserts one record, `after_failures == 2`, after `consuming`.

## Ship

PR `Refs #134` → FF merge → `scripts/deploy.sh` (a restart; no generation bump, no Watcher notice). Pre-deploy proof: CI's integration job, and optionally a scratch-bus run (`processor run` on local redis, stop/start it, read one record). Live proof waits for the next real failure run: verification debt on epic #84.

## Open questions

None.
