---
title: Adopt the unified shipping-work skill; the refresh hook's token (#115)
date: 2026-11-04
status: per #115 (body, the coordinator's pre-flight, the implementer's corrections); refresh-hook token per the operator, in-session 2026-11-04 (replicate gregoryfoster/skills#334 locally until it ships)
---

# shipping-work, and a token for the refresh hook

## Problem

Upstream folded the `shipping-work-*` variants into one `shipping-work` (gregoryfoster/skills#537). Processor is the last cohort repo still linked to its FastAPI variant. The doctor at `2026-11-03-1` repoints the link on its own; the docs and knobs have to follow.

`.skills/merge_mode pr` also moves the daily refresh hook onto a PR path (gregoryfoster/skills#560): it pushes `skills/submodule-refresh` and opens one PR with `gh`. Two things stop that here:
1. `gh` has no token in the hook's environment. `GH_TOKEN` lives in `.env`, which nothing loads automatically, and SessionStart hooks run in parallel, so no export from another hook reaches it (#334, comment of 2026-09-28).
2. The hook takes `--repo` from the configured remote URL. Ours is the SSH alias `git@github-processor:…`, so `gh` dials host `github-processor` and fails. That is an upstream bug. The workaround is local config, and it needs the operator's approval.

## Approach

1. **Links:** commit the doctor's repoint (`skills/shipping-work`, `.claude/skills/shipping-work`).
2. **Knobs:**
   - `merge_mode` `pr`;
   - `deploy_command` `setsid -w scripts/deploy.sh` (broker#123, archiver);
   - `doc-sensitive-paths` with `doc-sections`, tailored;
   - `gate-stamp-inputs` `.env` plus `.wheelhouse/*`.
3. **The #334 replica, local:**
   - `scripts/env-value.sh`: reads one name from the primary checkout's `.env`, as data, never executing it.
   - `.claude/hooks/gh-env.sh`: a SessionStart hook. It writes `export GH_TOKEN=<%q>` to `$CLAUDE_ENV_FILE`, leaves a `GH_TOKEN` that is already set alone, warns when `gh` is older than 2.78.0, never prints the token, and always exits 0.
   - The refresh hook's `settings.json` entry runs `GH_TOKEN="${GH_TOKEN:-$(bash scripts/env-value.sh GH_TOKEN)}" bash …/skills-submodule-update.sh` (a token already in the environment wins). `install-refresh.sh --check` still exits 0 on that entry (measured). An install-mode re-run puts the canonical command back: a known limit.
4. **Docs:**
   - AGENTS.md *Agent Skills* and *Conventions → Workflow*: shorter, within the budget.
   - `docs/SKILLS.md`: the inventory, § Refresh, the `:68` section (it becomes "Merging under `pr`", where the FF merge is recorded), § Not installed, and a doc-check section.
5. **Tests first:**
   - `tests/test_shipping_knobs.py`: the knobs through the skill's own `merge-mode.sh` and `run-deploy-command.sh --check`, run in a scratch repo; every doc-check entry is live.
   - `tests/test_gh_env.py`: the hook and the parser, including `%q` quoting, `2.9.0 < 2.78.0`, a `.env` that is never executed, and a token that is never printed.
   - `test_skills.py`'s inventory test is already red on the repoint.

## Tradeoffs

- **A local hook ahead of upstream.** It is retired by adopting `using-github-cli` once #334 ships, as the operator asked. It doesn't override a vendored skill, so #115's done-when holds.
- **Not done:** a stored `gh` login, or a token in `settings.local.json`. Both make a second copy of the PAT, which #334 rejects.

## Ship

PR (`Refs #115`: the refresh hook's PR path and the first `deploy_command` run are checked after the merge) → CI → FF merge → `git pull --ff-only`. No deploy: no `RUNTIME_*` path changes.
