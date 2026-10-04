---
title: Outcome records carry the command's digests (#26)
date: 2026-10-04
status: approved 2026-10-04 (operator: "add your commentary to the issue and proceed")
---

# Outcome digests

## Problem

An outcome record carries `command_id`, `info_source_id`, `reason`, `detail` and timings, and no digests. During shadow, a mismatch has to be explained from Processor's side, so the journal must say what Processor read and what it published without asking Watcher or listing the bucket (#26).

## Approach

Only add keys. The four-key floor and every existing key stay as they are. Review and decisions: [#26 comment](https://github.com/CannObserv/processor/issues/26#issuecomment-5984746918).

- `Disposition` gains `fields: dict[str, object]`, which `_log` spreads alongside `timings`.
- **`input_digest`** comes from `command_fields(command)` in `handler.py`: `command_id`, `info_source_id`, and `input_digest` only once `validate_fingerprint` accepts it. Both `handle()` and `consumer._command_ids` use it, so every outcome record that names a command also names its input: complete, terminal, strike, leave_pending, `strike: escaped` and `dead-lettering`. An `invalid_input` record has no `input_digest`, and its `detail` already carries the rejected value.
- **Output fields** come from the published `ProcessingCompleteEmit`, not from `_derive`: `output_digest` (omitted when empty), `output_size_bytes`, `empty`, `processor_version`. They are set once the fact exists, which includes the publish-failure `leave_pending` (stored, not yet published).
- Digests keep their wire form: `input_digest` is bare hex and `output_digest` is `sha256:<hex>`.
- **`stored` is dropped.** co-core 0.19.7's GCS `_create` swallows the write-if-absent 412, so "new or already present" isn't observable without an extra racy round trip or a lockstep co-core change.

## Steps

1. Handler tests (red): complete, empty, terminal failure, `invalid_input`, strike, and the publish-failure `leave_pending` each carry exactly their fields.
2. `command_fields`, `Disposition.fields` and the fact's output fields (green).
3. Consumer tests (red, then green): the `ack: complete` record, as the formatter emits it, carries the new keys and keeps the old ones; escape and dead-lettering records carry `input_digest`.
4. Docs: spec §4's "Every outcome logs …", DEPLOYMENT's **Logs** bullet, and AGENTS.md's **Logging** convention.
5. PR. After the FF merge, deploy (`uv sync --frozen --no-dev && sudo systemctl restart processor`) and check a live shadow record.

## Out of scope

#28 (a refused dead-letter at the cap), and a cannobserv ask for `store` to report whether it created the object.
