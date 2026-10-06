---
title: scripts/deploy.sh gates a deploy on the commit's CI (#34)
date: 2026-10-06
status: per #34 and its hand-off from epic #24 (2026-10-06); review on the PR
---

# Deploy CI gate

## Problem

Since #2, nothing reaches the unit until `scripts/deploy.sh` runs, and it deploys any commit on `origin/main` whether CI passed or not. CI is the only place the suite runs fully contained on a second kernel (Landlock ABI 7).

## Approach

Port Status's gate (CannObserv/status `scripts/deploy.sh`, status#11) between `build=` and the build, so a refusal builds nothing: `github()`, `push_run`, `finished_run` and `ci_gate`, with their comments where they still apply, credited to status#11.

- **The run.** The newest `push` run of `ci.yml` on `main` for exactly this SHA. Its conclusion and every job's must be `success`, and `CI_JOBS=(lint test)` is a floor.
- **Pending: it waits.** Up to `PROCESSOR_DEPLOY_CI_WAIT_SECONDS` (600), polling every `PROCESSOR_DEPLOY_CI_POLL_SECONDS` (30).
- **No run.** A commit behind the tip is refused at once. FF merges push many commits, and only the tip gets a run (#33: 21 commits, one run). The tip itself waits.
- **Cancelled** keeps Status's remedy: re-run it (a re-run counts), or deploy a newer commit. Here a cancel also comes from a run that timed out unscheduled (#23).
- **GitHub refusing** (403 rate limit, 5xx, non-JSON) refuses with GitHub's message and the `--skip-ci` hint. It never passes.
- **`--skip-ci`** skips the gate and is logged first.
- **Logging tag:** `processor-deploy`.

Differences from Status: no `--dev` target, so no `live`/`dev` branching and no "put it on dev" remedy.

**Rollbacks: option (a).** The gate applies to every deploy, rollbacks included, and `--skip-ci` is the logged escape. That gives one rule. Every earlier deploy was a tip with a push run, so a normal rollback passes, and a slow or rate-limited GitHub is what `--skip-ci` is for. Option (b), skipping the gate for a kept release that `live` has run, would need a deploy ledger the script doesn't keep. A release on disk proves it was built, not that it was verified.

## Steps

1. Tests (red), in `tests/test_deploy.py`. A `curl` stub answers GitHub's Actions API from `<state>/ci/`, as Status's does. Never the real API.
   - Success, and the run URL logged before the build.
   - A failed job.
   - A skipped required job, where the run says `success` and the job doesn't.
   - A required job missing from the run.
   - A job the checkout doesn't know about, failed.
   - `cancelled`, with the re-run remedy.
   - No run for a non-tip commit: refused at once, with one API call.
   - No run for the tip: waits, bounded, then refused.
   - Pending, then done within the wait.
   - A `pull_request` run alone doesn't count. Neither does another kind's run, newer, either way.
   - Of two push runs, the newest decides.
   - GitHub refusing: 403 rate limit, 5xx, non-JSON runs, non-JSON jobs. Each refused, nothing built.
   - `--skip-ci`: logged before the build, with no API call.
   - `--help` names `--skip-ci`.
   - `CI_JOBS` names jobs in `ci.yml` that run on a push to `main`.
2. Port the gate (green). Then the full suite and ruff.
3. Docs:
   - DEPLOYMENT's "Deploy a change" gets a new § The CI gate, which states the intermediate-commit refusal and the rollback rule.
   - The variables table.
   - The "not adopted yet" line.
   - The script's header.
4. PR, then the FF merge, then `git pull --ff-only`, then `scripts/deploy.sh` with the new script. The evidence:
   - `journalctl -t processor-deploy` shows "CI passed for <build>: <run url>";
   - the usual verify;
   - one harmless refusal, of an intermediate commit of this PR.

## Out of scope

#35 (the drift signal).

## Amended in review

- **CR 1:** `ci.yml`'s comment said runs on `main` are never cancelled, but GitHub keeps only one *pending* run per concurrency group, whatever `cancel-in-progress` says. Two pushes to `main` while a run is in progress cancel the earlier pending run. The gate's `cancelled` remedy (re-run it) already covered that case. The comment, `ci_gate`'s comment and DEPLOYMENT now say so.
- **CR 5:** an empty 200 answer for the jobs passed the gate, because jq reads an empty body as no input: no output, exit 0. `github()` now accepts only a JSON object and refuses anything else. Status has the same gate, and this is filed there as status#21.
