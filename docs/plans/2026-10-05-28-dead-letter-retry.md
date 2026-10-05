---
title: A refused dead-letter at the cap retries only the give-up (#28)
date: 2026-10-05
status: per the #28 decision (2026-10-04) and its hand-off from epic #24 (2026-10-05); review on the PR
---

# Dead-letter retry at the cap

## Problem

At the cap, a non-transient escape publishes the give-up fact, then dead-letters the entry. If the dead-letter is refused (transiently, or non-transiently such as `WRONGTYPE` on `content.process.dlq`), `_strikes` stays at `max_attempts - 1`. The next reclaim then runs `handle` again at the cap. That puts the untrusted parser (#2) back on an input that has already escaped three times. A success from that re-run publishes a fact that Watcher drops (first fact wins), and it leaves an orphaned output. A DLQ refused every time re-runs the extraction on every reclaim, about every 11 min.

Decided on #28 (2026-10-04): once an entry has given up, a later step skips `handle` and goes straight to the dead-letter.

## Approach

- **Key the skip on a mark, not on `_fact_out`.** `_fact_out` also holds an entry whose *own* fact went out before its ack failed. Keying on it breaks `test_a_failed_ack_at_the_cap_keeps_the_count` and `test_a_refused_ack_at_the_cap_adds_no_failure_fact`. A throwaway run on 2026-10-05 confirmed both failures.
- `Consumer._gave_up: dict[str, str]` maps a message id to its give-up reason (`gave up on attempt 3: <detail>`). The cap branch of `_process` sets it before it publishes anything.
- At the start of `_process`, a marked entry skips `_act`. It re-runs the give-up only: `_publish_gave_up` (which already skips a fact that is out, and retries one that was refused), then `_dead_letter` with the stored reason. The DLQ entry and the published `dead-lettered: …` detail therefore agree. An exception on this path propagates as it is and adds no strike.
- **Forgetting an entry** (`_strikes`, `_fact_out`, `_gave_up`) moves into one `_forget(message_id)`. It is called on a landed dead-letter, a landed ack, a handler `dead_letter` disposition, and an entry `XAUTOCLAIM` reports deleted.
- **Logging.** Each retry logs the existing `dead-lettering` record at ERROR, with `input_digest` (#26). A new key, `handle_skipped`, is `false` on the give-up attempt and `true` on a retry. No new message string is added.
- **A restart** clears the mark along with `_strikes` and `_fact_out`. The entry starts again from attempt 1, which spec §4 already says.

## Steps

1. Tests (red), on the scratch Redis:
   1. Rework `test_a_failed_dead_letter_at_the_cap_keeps_the_count`. `handle` raises 3 times, then would succeed, and counts its calls. The first dead-letter is refused transiently. Expect 3 calls, DLQ 1, pending 0, facts `[extraction_error]`.
   2. The same, with the first dead-letter refused non-transiently (`ResponseError`).
   3. A dead-letter refused every time, over 2 more steps. Expect `handle` still called 3 times, pending 1, no new fact, one `dead-lettering` ERROR per step with `handle_skipped` true and the original reason.
   4. Unchanged: the tests at lines 502, 533, 563 and 613.
2. `_gave_up`, the skip and `_forget` (green). Then the full suite and ruff.
3. Docs: the module docstring; spec §4's "Failure, then success." sub-bullet and the two cap rows of the failure table; DEPLOYMENT's **Dead letters** bullet for `handle_skipped`.
4. PR. After the FF merge, deploy with `uv sync --frozen --no-dev && sudo systemctl restart processor`. A live check can't reach this path, so the evidence is the tests and the first `ack: complete` record after the restart.

## Out of scope

#21 (a dedicated give-up reason, blocked on cannobserv). No change to `handler.py`.
