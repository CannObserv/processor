---
title: Adopt shipping-work's local-gate attestation (#147)
date: 2026-11-24
status: per #147's brief (comment 7141925251, epic #84; its three recommended decisions approved by the operator); corrections on the issue (comment 7142241797)
---

# Local-gate attestation

## Problem

Since gregoryfoster/skills#688, `shipping-work`'s `push.sh` can attest the local gate. With `.skills/attest-local-gate` committed as `on`, it posts the commit status `gate/local` = `success` on the SHA it pushed, and CI may then skip its heavy tier. The repo is public, so this saves no billed minutes. It is conformance with the cohort's adopters (status, broker, address-validator). The reference is `skills/shipping-work/references/attestation.md` at the vendored `9443df67`.

Two of this repo's own constraints shape the CI side:
- **The deploy gate.** `scripts/deploy.sh` needs `lint` and `test` to be `success` on `main`'s push run. A skipped *job* reads `skipped` there and blocks the deploy. A skipped *step* leaves its job `success`.
- **The integration tier.** `pre-ship.sh` deselects every `integration` test (60 of 1,124), so the gate never ran them. The reference's skip of `uv run pytest` would drop them from an attested PR.

## Approach

The brief's decisions:
1. **CannObserv/status's shape.** The `attest` step runs on `pull_request` only, after `uv sync`. `pytest` takes the reference's `if:` verbatim, at step level. A push to `main` reads nothing and runs the full suite, so `deploy.sh`, `CI_JOBS` and the gate's meaning don't change.
2. **The integration tier still runs on an attested PR.** A step `uv run pytest -m integration` takes `!(<pytest's if>)`: exactly one of the two runs.
3. **No schedule.** `merge_mode` is `pr`, and every commit that reaches `main` gets a full push run.

`statuses: read` goes on the `test` job's own `permissions`, since a job's `permissions` replace the workflow's.

## Steps (TDD)

1. `tests/test_ci.py`, red first:
   - the exact permissions: `lint` unchanged, `test` adds `statuses: read`;
   - the attest step: `if: github.event_name == 'pull_request'`, reads the combined status with `per_page=100`, keyed to the PR's head;
   - both `if:`s, evaluated over an event matrix by a small evaluator for GitHub's expression syntax. The full suite never skips on a push, `workflow_dispatch`, a tag, a bot's PR, a fork's PR or an unattested PR, and skips on an attested PR. The integration step's `if:` is `!(<pytest's>)`;
   - the full suite still has no `-m`; the integration step is exactly `-m integration`.
2. `tests/test_shipping_knobs.py`: the knob reads `on` through the skill's own `attest_read_knob`, in the scratch repo.
3. `.github/workflows/ci.yml` and `.skills/attest-local-gate` (`on`); green.
4. `.skills/doc-sensitive-paths` gains `attest-local-gate`.
5. Docs:
   - `docs/SKILLS.md`: the knob row at `:14`, and § Shipping's table;
   - `docs/DEPENDENCIES.md`: the setup-uv pin row (v10.4.0, #148) and a short § Local-gate attestation;
   - `docs/DEPLOYMENT.md` § The CI gate: one line;
   - `AGENTS.md`'s CI row, within its budget.

## Ship

PR (`Refs #147`: the Dependabot case is the live check) via `shipping-work`'s `push.sh`, whose first push prints `Attestation: gate/local = success posted on <sha>`. On the PR's run: `attested=yes`, `pytest` skipped, the integration step ran 60 tests, `lint` ran in full. After the FF merge, `main`'s push run on the same SHA: `attest` skipped, `pytest` ran the full suite. No deploy: nothing in `processor.drift.RUNTIME_*` moves.
