---
title: The weekly window reads the live lock (#75)
date: 2026-10-27
status: per #75's brief (option 4, 2026-10-26) and its review comment, K1–K8 (2026-10-27)
---

# The weekly window reads the live lock

## Problem

`audit.yml`'s weekly run reports a finding as a warning on a green run, which notifies nobody. The operator chose option 4: the weekly patch window's language reading (`probe.sh --refresh-into`, `uv audit --frozen` on `/srv/processor/live/uv.lock`) is the human read. Two claims say nothing else reads the lock against OSV (`tests/test_lock_audit.py:5`, `DEPLOYMENT.md:251`); since #77 that's false. And the read itself is not in the runbook (K1): § Patching's steps never pass `--refresh-into`, the one flag that takes it.

## Approach

- `DEPLOYMENT.md` § The lock audit: two readers. `audit.yml` is the push/PR/`main` signal between windows, report-only; the window's after-probe is the weekly human read of the **live** lock. Limits: a mid-week disclosure waits up to 7 days; a skipped window leaves no reading, and a run without the after-probe leaves the class in `not_read`; a unit moved off the lock reads clean, which the guard catches (CR 2). A finding there takes "Where a finding goes", outside the run (K8). The reopen path: option 2.
- § Patching: a numbered step 9, the after-probe, `probe.sh --refresh-into <dir>`, last in every run, after the reboot or the no-reboot restart (K1, CR 3). The record paragraph names the language line and the owning repo's mechanism, `audit.yml` (K6).
- `tests/test_lock_audit.py` docstring: the claim corrected (K7). No contract change.
- No `audit.yml`, `src/` or `deploy/` change.

## Tests (red first), in `tests/test_patching_knob.py`

- `test_the_language_read_finds_the_live_lock`: each `deploy/*.service` with a `WorkingDirectory=` names `deploy.sh`'s live link (`$ROOT/live`, `ROOT`'s default), and the knob's `owner` path is that link too (K2, K3). Green today: a guard, not a red one; checked by breaking each side.
- `test_the_runbook_takes_the_language_read`: § Patching's run steps include a `probe.sh --refresh-into` step. Red until step 9 lands.

## Ship

PR `Refs #75` → FF merge → `git pull --ff-only`. No deploy: docs and tests only, nothing under the drift runtime paths.

## Open questions

None.
