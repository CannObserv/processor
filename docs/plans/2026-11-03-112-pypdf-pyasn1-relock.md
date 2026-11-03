---
title: pypdf 6.21.4 and pyasn1 0.6.5; LOCAL_GENERATION 3 (#112)
date: 2026-11-03
status: per #112 (filed by the burn-down coordinator, #84); procedure per #71 (docs/DEPLOYMENT.md § Extraction dependencies)
---

# pypdf 6.21.4, pyasn1 0.6.5

## Problem

`uv audit --frozen` on `main` `312552a`: 11 advisories in 2 packages, all published 2026-10-30. pypdf 6.21.2 has 7 (memory or runtime blowups, two infinite loops on crafted PDFs; fixed in 6.21.4). It runs in the extraction child, on untrusted documents. pyasn1 0.6.4 has 4 (quadratic-time BER/DER decoding; fixed in 0.6.5). It runs in the parent, through google-auth's `pyasn1-modules`. The child's timeout and `RLIMIT_AS` bound the pypdf cases to one command, at most 3 × 120 s of the one consumer. The re-lock removes that cost.

pypdf is in the extraction stack, and `processor_version` can't see it move (spec §5, #71).

## Approach

1. `uv lock --upgrade-package pypdf --upgrade-package pyasn1`, never `--upgrade`. The diff moves those two alone. co-core stays `==0.38.0`.
2. Floors in `[tool.uv] constraint-dependencies`: `pypdf>=6.21.4` (was 6.21.2), and a new `pyasn1>=0.6.5`. `tests/test_lock_floors.py` checks both.
3. `EXTRACTION_STACK` pypdf → 6.21.4, `LOCAL_GENERATION` and `GENERATION` 2 → 3, so `processor_version` = `0.38.0+3`. pyasn1 isn't in the stack, so it gets a floor and nothing else.
4. Docs: spec header **Amended** entry, §5's generation line, §7's pin-test line. DEPLOYMENT.md § Extraction dependencies gets its "Since" line. § CI pins and Dependabot gets the operator's re-reading of the security-updates setting (#112 item 1).

## Evidence

- The parity goldens and the real corpus pass unchanged.
- Wider corpus, as #71 did: co-core 0.38.0's `PdfExtractor` → `canonical_text_fingerprint` over py-pdf's `sample-files` and pypdf's `resources/` at HEAD, on 6.21.2 and 6.21.4, in two venvs that differ in pypdf alone. Result: 0 of 110 moved (the PR body has the counts).

## Ship

PR (`Refs #112`: live checks remain after the merge) → CI → FF merge → `git pull --ff-only` → notice to Watcher (approved post) → `scripts/deploy.sh` → verdict line, drift `ok`, `uv audit` on `main` reads 0.
