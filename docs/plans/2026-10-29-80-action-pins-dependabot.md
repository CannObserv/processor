---
title: actions pinned by SHA, Dependabot for github-actions only (#80)
date: 2026-10-29
status: per #80's body, the coordinator's brief (2026-10-29), the implementer's corrections and the operator's answers on #80 (2026-10-29)
---

# Actions pinned by SHA; Dependabot moves them

A port of CannObserv/broker#90 (`.github/dependabot.yml`, `tests/deploy/test_dependabot.py`) and the SHA-pinned `ci.yml` it rests on (broker#88).

**Problem.** `ci.yml` (6 sites) and `audit.yml` (2) name `actions/checkout@v5`, `astral-sh/setup-uv@v7` and `google-github-actions/auth@v3`, all mutable tags, and nothing proposes updates.

**Approach.**
- **Pins: broker's majors, now** (operator, 2026-10-29). Each `uses:` becomes `owner/action@<40-hex> # vX.Y.Z`:
  - checkout v7.0.1 `3d3c42e…`;
  - setup-uv v10.3.2 `07a047e…`, broker's exact pin (v10.3.3 is a day old, and will be Dependabot's first PR);
  - auth v3.0.0 `7c6bc77…`.
  Each tag is lightweight, on a commit.
- **The majors' breaking changes, against our usage:**
  - checkout v6 moved its persisted credentials to a separate file; we never push.
  - checkout v7 blocks fork checkouts on `pull_request_target`/`workflow_run`; we use neither.
  - setup-uv v8 publishes no floating tags (we pin SHAs) and dropped the old manifest-file format (we have none).
  - setup-uv v9 makes `prune-cache` default false. **The repo is public**, so unpruned, `main`'s saved cache would hold co-core unpacked from the private wheelhouse, restorable by a fork's PR. `ci.yml` sets `prune-cache: true`.
  - setup-uv v10's `enable-cache: auto` turns the cache off on some events; we set `enable-cache` explicitly.
- **`.github/dependabot.yml`:**
  - `github-actions` only, `directory: "/"`, weekly;
  - `open-pull-requests-limit: 10`, commit prefix `chore`;
  - no `exclude-paths`, since no workflow is generated here.
  - **Never `uv`/`pip`:** the co-core pin and the held extraction stack (AGENTS.md, #71), and the wheelhouse is private.
- **Code security, read by the operator:**
  - graph on, alerts on, security updates **off**;
  - recorded on #80 and in the docs, since no test can see it.
- **Docs:**
  - `docs/DEPLOYMENT.md` § CI pins and Dependabot: the pin check, how a Dependabot PR lands (fast-forward in the deploy checkout, no deploy), and what to read on a red WIF step;
  - § Patching's language line;
  - AGENTS.md's CI row.

**Tests (red first).**
- `tests/test_dependabot.py` (new, mirrors broker's, cut to what we run):
  - the config exists and is version 2;
  - `github-actions` is the only ecosystem;
  - `directory` is `/`, the interval weekly, and the limit above the action count;
  - the prefix is one of AGENTS.md's types (its `Types:` list is mid-paragraph, unlike broker's);
  - every `uses:` in every workflow is `@<sha> # vX.Y.Z`.
- `tests/test_ci.py`:
  - `test_actions_are_on_node_24_majors` becomes a floor on the tag comment's major (7 / 10 / 3), so a Dependabot major PR passes and a downgrade fails;
  - `ci.yml` sets `enable-cache` and `prune-cache` true on each setup-uv step;
  - every `.wheelhouse` package is locked to a wheel (the prune keeps a source-built one).
- `tests/test_lock_audit.py::test_actions_are_pinned_as_ci_pins_them` already holds `audit.yml` equal to `ci.yml`; unchanged.

**Deploy.** None. `.github/`, `tests/` and `docs/` are outside `RUNTIME_*` (`src/processor/drift.py:63-64`).

**Verified after the merge.** CI is green on the merge's push run, with checkout v7 bringing the submodules and setup-uv v10 pruning. `audit.yml` passes on the same push.
