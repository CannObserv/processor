---
title: The co-core bump rule — a dispatch-essence change is notice (#57)
date: 2026-10-21
status: per #57's hand-off from epic #24 (2026-10-21)
---

# A dispatch-essence change is notice, not coordination

## Problem

The bump rule lists `resolve_dispatch_essence` in the contract, so a change to it needs coordination with Watcher. Processor never calls it: the issuer resolves the essence on its own pin and puts it on the command, and Processor dispatches what arrives (spec §3, cannobserv#486 D1). `extractor_for_essence` and `extraction_overrides_for_essence`, which Processor does call, don't call it either (co-core 0.38.0). #29 coordinated (watcher#366) where notice sufficed; #37 used notice, against the rule as written.

## Approach

- Drop `resolve_dispatch_essence` from the contract list at the four rule sites: `AGENTS.md:21` (the contract list only), `docs/DEPLOYMENT.md` "Bumping co-core" step 4, spec D5, spec §5's bump list.
- §5 says why, once: resolution is the issuer's, on its pin; Processor never re-resolves; so a change is notice, with the either-order reasoning and the moved digests; coordination again only if Processor starts resolving essences itself, which the guard test catches.
- Spec amendment entry, 2026-10-21 (processor#57): #29's coordination on watcher#366 was more than the rule needed; #37 used notice. The 2026-10-12 / 2026-10-20 entries and §5's per-bump notes stay as written.
- Untouched: notice on every `processor_version` move; `EXTRACTOR_BY_ESSENCE`.

## Test

`tests/test_pin.py` (the bump rule's test home): walk every `src/processor/**/*.py` AST, as `test_gen_parity_goldens.py` does, and fail on an `import`/`from … import` of, or a `Name` / `Attribute` use of, `resolve_dispatch_essence` or `extension_media_type`. Code, not text: a docstring mentioning them passes. The message points at §5. A parametrised self-test holds the walker to catching each form and passing a docstring.

Red: the guard on a scratch copy of `src/processor/` with `from co_core.pure.extract import resolve_dispatch_essence` planted, output in the PR.

Known limit: a string-keyed lookup (`getattr(m, "resolve_dispatch_essence")`) isn't caught; that is deliberate evasion, not drift.

## Ship

PR (`Refs #57` until the Watcher notice is posted or declined) → FF merge → `git pull --ff-only`. No deploy: nothing under `src/`, `deploy/`, `scripts/deploy.sh`, `pyproject.toml`, `uv.lock` changes. Then the notice on CannObserv/watcher#368, drafted in the PR, posted only with the operator's approval.

## Open questions

None.
