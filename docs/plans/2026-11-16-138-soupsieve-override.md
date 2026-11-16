---
title: soupsieve 4.1.1 by override and opentelemetry-api 1.45.2; LOCAL_GENERATION 4 (#138)
date: 2026-11-16
status: per #138 (filed by the burn-down coordinator, #84; pre-flight comment 6923328878; corrections on the issue); procedure per #71 (docs/DEPENDENCIES.md § Extraction dependencies)
---

# soupsieve 4.1.1, opentelemetry-api 1.45.2

## Problem

Four open Dependabot alerts on `uv.lock`, and `main`'s audit lists them all (run 43757382623, `263f3ae`):
- **soupsieve 2.10** has 3: GHSA-mmhr-3c5m-f64f (polynomial-time descendant combinator, fixed in 4.0), GHSA-6655-6qjp-jx7x (ReDoS in selector compilation, 3.0.2) and GHSA-7g44-g9rc-x53w (parser recursion, 3.0.2). co-core's `HtmlExtractor` compiles and runs Watcher's spec selectors over untrusted documents in the extraction child. The child's timeout and `RLIMIT_AS` bound the damage to one command.
- **opentelemetry-api 1.45.1** has 1: GHSA-mf5v-w3w3-f6cg (quadratic `traceparent` parsing). It runs in the parent, through google-api-core.

co-core 0.38.0's `extract` extra declares `soupsieve>=2.0,<3`, so neither a constraint nor Dependabot can reach a fix. co-core v0.46.1 raised it to `>=4.1,<5` (cannobserv#667). `html.py` is byte-identical in v0.38.0, v0.46.1 and the venv.

## Approach

Operator decision (#138): override past co-core's cap. The co-core bump is out of scope and gets its own issue.

1. `[tool.uv] override-dependencies = ["soupsieve>=4.1,<5"]`. A constraint only narrows, and co-core's `<3` would stay. The `opentelemetry-api` floor in `constraint-dependencies` goes to 1.45.2.
2. `bash scripts/co-pypi.sh uv lock --upgrade-package soupsieve --upgrade-package opentelemetry-api`, never `--upgrade`. Expected diff: soupsieve 2.10 → 4.1.1, opentelemetry-api 1.45.1 → 1.45.2, the manifest's `overrides` entry and the floor's specifier. Every co-core `source` stays the index URL.
3. `tests/test_lock_floors.py`:
   - the opentelemetry-api floor moves to 1.45.2;
   - soupsieve gets a lock and venv floor of 4.1, and a check that the override is in `override-dependencies`;
   - a guard reads the installed co-core's `extract` requirement on soupsieve. Once that requirement alone floors at 4.1 or higher, the override is redundant, so the guard fails until the co-core bump removes it.
4. `tests/test_pin.py`: `EXTRACTION_STACK` soupsieve `4.1.1`, `GENERATION` 3 → 4. `LOCAL_GENERATION` 3 → 4, so `processor_version` = `0.38.0+4`.
5. Docs:
   - The spec: an **Amended** entry, §5's generation line, §7's pin-test line.
   - DEPENDENCIES.md:
     - § Extraction dependencies: the "Since" line, plus a step for a package co-core caps (it takes an override, not a floor);
     - § Bumping co-core: drop the override at the bump to co-core ≥ 0.46.1;
     - § The lock audit's "Where a finding goes" and § CI pins' "lands by hand": one clause naming the override route.
   - The audit script's two routing lines stay as they are (pre-flight hazard 1): the section they link carries the override route.

## Evidence

- The parity goldens and the real corpus pass unchanged, and `tests/test_child.py` passes under `required` containment.
- Wider corpus, as #71 and #112 did: co-core 0.38.0's `HtmlExtractor`, then `canonical_text_fingerprint`. Inputs: the parity HTML, the real corpus, and every HTML file over 3 KB on the host, each under a battery of 34 selector configs. It runs in two venvs that differ in soupsieve alone (2.10 and 4.1.1). The PR body has the counts.
- What soupsieve's changelog breaks between 2.10 and 4.1.1, and how each break lands here:
  - 3.0's strict `:has()`: a complex or nested `:has()` now refuses;
  - 4.0's `<template>`: its contents give no text on either version;
  - 4.0's `<iframe>`: lxml escapes its contents.

## Ship

PR (`Refs #138`: live checks remain after the merge) → CI → FF merge → `git pull --ff-only` → notice to Watcher on watcher#394 (approved post) → `scripts/deploy.sh` → the verdict line, `processor_version` `0.38.0+4` in the logs, the new versions in the release venv, drift `ok`, then the audit on `main` reads 0 and alerts 12–15 close. Close PR #135 in favour of this one.
