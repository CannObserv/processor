---
title: A drift signal when live lags origin/main, through a Status monitor (#35)
date: 2026-10-06
status: per #35 and its hand-off from epic #24 (2026-10-06); review before TDD
---

# Drift signal

## Problem

Since #2, a merge changes nothing that runs until `scripts/deploy.sh`, and nothing reports a merge that was never deployed. Since the cutover (2026-10-06T21:35:24Z, watcher#326) Processor's output decides production results, so a fix merged but not deployed is a real gap.

Operator decision (2026-10-06): Status's job. Processor checks in to a Status monitor (`co-processor-drift`); no healthchecks.io of its own.

## Approach

Port Status's drift check (status#12: `src/core/drift.py`, `scripts/check_drift.py`) and its unit install (status#18).

**The check: `processor drift`**, a new subcommand. Hourly from `processor-drift.timer` → `processor-drift.service` (oneshot, `User=processor`).

1. Read the live release's `REVISION` (`processor.build.build_id`).
2. Ask GitHub, unauthenticated: `compare/<live>...main`; when the diff counts, CI's push runs on `main` (when each push landed); past the grace, walk up to 8 compares to find the push that brought runtime code. Status's logic and its CR fixes, ported.
3. Check in to Status once: `POST http://status:9000/api/v1/monitors/<id>/checkin`, `X-API-Key` from a credential, body `{"status": "ok"|"alert", "variables": {...}}`.

| Situation | Check-in | Exit |
|---|---|---|
| live is `main` | `ok` | 0 |
| behind only in paths that don't run | `ok` | 0 |
| behind in runtime code, the clock (from the push) ≤ 8 h | `ok` | 0 |
| behind in runtime code past 8 h | `alert`, naming main's CI result and the remedy | 0 |
| live not on `main` (diverged; GitHub 404 on live) or unstamped (`dev`) | `alert` | 0 |
| GitHub silent (rate limit, 5xx, timeout, wrong shape) | **none**: log WARNING | 1 |
| Status refuses or is unreachable, or not configured (no key, no monitor id) | (attempted or none): log ERROR | 1 |

GitHub silent never sends `ok`: a long outage ends in Status's `missing` (trap 3). No retry loop; the next hour is the retry (trap 4). Logged as one JSON record, `drift check`, with `build`, `main`, `verdict`, the body and Status's answer code.

**"Runs" is an allowlist** (#35, AGENTS.md's deploy rule): `src/`, `deploy/`, `scripts/deploy.sh`, `pyproject.toml`, `uv.lock`. One definition, in `processor.drift`, which AGENTS.md and DEPLOYMENT point at. Status uses a denylist; here the rule already exists and the src layout holds all package code. A compare cut short (≥ 300 files, or fewer commits than `total_commits`) counts, as Status's does.

**Trap 2 (is the check itself drift?): yes, with no special case.** The check lives in `src/processor/drift.py` and its units in `deploy/`, both runtime paths. The unit runs the venv's own entry point (`/srv/processor/live/.venv/bin/processor drift`), as `processor.service` does (R5), so there is **no `scripts/drift.sh`** (a deviation from the hand-off's shape: Status needs the wrapper because it has no entry point). A change to the check needs a deploy, and a lag in it alerts like any other.

**Trap 4 (a Status outage never touches `processor run`):** separate oneshot unit, its own `TimeoutStartSec=90` (GitHub ≤ 60 s total, the check-in 10 s), no `Restart=`. Nothing in `processor run`'s path imports it. `processor drift` loads its own settings, so it never needs the broker credential.

**Configuration:**
- `DriftSettings` (pydantic-settings, `CO_PROCESSOR_`): `status_url` (default `http://status:9000`, by MagicDNS name), `drift_monitor_id` (a ULID, not secret).
- The monitor id goes in `processor-drift.service` as `Environment=`, so it is reviewed and versioned. That needs gate 1 before the merge.
- The key: `LoadCredential=status-checkin-key:/etc/processor/status-checkin.key` (root 600, as #2 set up), with a `SetCredential=` empty fallback so a missing file runs the check, logs "no key", exits 1. No `EnvironmentFile=`: the drift unit never sees the broker credential.
- `processor drift --test-alert` sends one `alert` with `kind: test`, nothing else: the deliberate alert for the live check.

**Variables** (every check-in carries all of them, so the template never fails to render): `kind` (`ok` | `lag` | `off_main` | `unstamped` | `test`), `live`, `main` (or `unknown`), `body` (the verdict sentence). Proposed templates: title `co-processor drift: {{ kind }} (live {{ live }})`, body `{{ body }}`.

**Trap 1 (`deploy.sh` installs only `processor.service`):** port status#18.
- `install_units`: every `deploy/*.service` and `deploy/*.timer` in the release that differs from the installed copy, each copy kept aside; one `daemon-reload`; a changed timer `try-restart`ed.
- **A timer the deploy installs new is enabled** (`systemctl enable --now`), as the hand-off asks. Differs from Status, which never enables ("enabling is a decision"): here the decision is this PR. A timer already installed is never re-enabled, so an operator's `disable` sticks.
- `restore_units` on a switch back: replaced copies go back, added units are disabled (timers) and removed, timers `try-restart`ed.
- Only `processor` is restarted and verified, as now. The drift unit's outcome never fails a deploy.
- `HOST_CONFIGS` unchanged; `tests/test_units.py`'s accounting gains the two units.

## Decisions for review

1. **An `alert` every hour while the lag lasts.** Stateless, and survives a missed run. Each alert check-in is a Status report (email + Slack), so a lag nobody deploys nags hourly. Alternative: alert once at the crossing (edge-triggered), which needs state (a `StateDirectory=`) or a fragile time window. **Recommended: hourly**, since the cure is a deploy and an intentional hold is rare; `systemctl stop processor-drift.timer` silences it, and the monitor then goes `missing`, which is honest.
2. **Grace 8 h**, Status's. Deploys are by hand; CI takes about 3 minutes.
3. **Monitor id in the unit**, so gate 1 comes before the merge (alternative: an `/etc/processor/drift.env` the operator writes).
4. **The liveness monitor** (`processor run` checks in every N minutes) is raised in the Status draft as optional, same tenant and key. Its Processor half is a separate issue if wanted.

## Steps

1. **Tests (red).**
   - `tests/test_drift.py`: verdicts from GitHub JSON (pure), ported from Status's `tests/core/test_drift.py` and narrowed to the allowlist: in sync, docs-only, `tests/`/CI/skills-only, each runtime path, a cut-short compare, within/past the grace, the clock at the push not the commit, the walk (limit, early stop inside the grace), `[skip ci]` main, main's CI named, off-main, 404, unstamped, GitHub silent and wrong-shaped.
   - `tests/test_checkin.py` (the Status client) and `tests/test_main.py` (`processor drift` end to end), with stubbed GitHub and stubbed Status as local HTTP servers: a runtime lag past the grace sends `alert`; a docs-only lag sends `ok`; GitHub silent sends nothing and exits 1; Status 5xx/unreachable/422 exits 1; no key and no monitor id exit 1 without a request; the key goes only in the header, never in a log; `--test-alert`.
   - `tests/test_units.py`: the drift service (oneshot, `User=processor`, the entry point, no `EnvironmentFile=`, the credential and its fallback, the hardening, `TimeoutStartSec`), the timer (hourly, `WantedBy=timers.target`), every file accounted for.
   - `tests/test_deploy.py`: a deploy installs both drift units and enables the timer; an unchanged unit is not reinstalled; an operator-disabled timer is not re-enabled; a changed timer is `try-restart`ed; a failed verify restores replaced units and removes/disables added ones.
2. **Implement (green):** `src/processor/drift.py`, `src/processor/checkin.py`, the subcommand, `DriftSettings`, the two units, `deploy.sh`'s `install_units`/`restore_units`. Full suite, ruff.
3. **Docs:** DEPLOYMENT § The drift check (the units, the credential install, the alert's meaning and what to do, `missing`'s meaning, the rate-limit budget, `--test-alert`), the deploy steps (every unit, the timer enable), "not adopted yet" line; spec §2 (Status in identities, tailnet, grants); AGENTS.md infra row.
4. **Gates, in parallel with 1–3:** the Status issue (gate 1), the tailnet ask (gate 2). Both drafted, approved by the operator, then posted / applied.
5. **Ship:** PR, FF merge, `git pull --ff-only`, the key (gate 3; installing it before the deploy makes the timer's first run check in), `scripts/deploy.sh`.
6. **Live:** `systemctl list-timers processor-drift.timer`; the first run's `drift check` record and 202; the monitor `pending` → `ok`; `--test-alert` reaches the operator, confirmed by them.

## Out of scope

The liveness monitor's Processor half (unless folded in), #29, #37.
