---
title: pypdf 6.21.2 and opentelemetry-api 1.45.1; an extraction-stack move is a generation bump (#71)
date: 2026-10-24
status: per #71 (filed by archiver's coordinator, archiver#357; cohort half of archiver#420)
---

# pypdf 6.21.2, opentelemetry-api 1.45.1

## Problem

`uv audit --frozen --no-dev` on `main` `a23970b`: 12 advisories in 51 runtime packages, 11 in pypdf 6.19.0 (loops, long runtimes, large memory, unvalidated JPEG), 1 in opentelemetry-api 1.45.0 (unbounded tracestate). Both fixes are in-cap: `uv lock --dry-run --upgrade-package pypdf --upgrade-package opentelemetry-api` moves those two alone. Processor parses attacker-reachable PDFs.

The child already bounds what the advisories can do: `RLIMIT_AS` ends the memory cases in a killed child; the parent's wall-clock kill (`extraction_timeout_s`, 120 s, `SIGKILL`) ends the loops and long runtimes, and the 3rd attempt publishes `extraction_error`, terminal. So the advisories cost a command and up to 3 × 120 s of the one consumer, not the host. The re-lock still removes that cost.

**The catch: pypdf moves output and `processor_version` doesn't.** 6.20–6.21.2 change text extraction (UniGB-UTF16 CMaps decode as UTF-16BE; NUL is whitespace in arrays; invalid font-width codes ignored; `%%EOF` off line start and null `/Kids` now parse) and add work limits that can turn a slow success into a raise. `processor_version` is `<co-core>+<LOCAL_GENERATION>`, blind to pypdf. Watcher's Option A compares it as an opaque string (`extraction_change`, watcher `src/workers/pipeline.py`): a moved fingerprint under the same version is a *content change*, notified. So an unmarked re-lock can notify users of PDF changes that are pypdf's, and breaks the invariant that a fact is a pure function of (bytes, spec, `processor_version`).

## Approach

1. **Re-lock** `uv lock --upgrade-package pypdf --upgrade-package opentelemetry-api`; the diff moves those two alone. Never `uv lock --upgrade`.
2. **Floors** that survive a re-lock, beside certifi's: `constraint-dependencies` gains `pypdf>=6.21.2` and `opentelemetry-api>=1.45.1`, commented with #71.
3. **`LOCAL_GENERATION` 1 → 2**, so `processor_version` is `0.38.0+2` and Watcher re-baselines silently, as at a co-core bump. Spec §5 amended: the generation also moves when the locked extraction stack moves outside a co-core bump.
4. **A test makes the next such move deliberate**, as the pin test does for co-core: `tests/test_pin.py` holds `EXTRACTION_STACK`, the versions of the transitive closure of co-core's `extract` extra in `uv.lock` (bs4, et-xmlfile, html5lib, lxml, openpyxl, pypdf, python-jsonpath, six, soupsieve, typing-extensions, webencodings), and the installed ones. A re-lock that moves any of them, or adds one, fails with the rule: bump the generation unless a co-core bump moves `processor_version` anyway. co-core's base deps (dateparser, pydantic) are out: the extract package doesn't import dateparser.
5. `tests/test_lock_floors.py`: the lock and the venv resolve pypdf ≥ 6.21.2 and opentelemetry-api ≥ 1.45.1.
6. Docs: spec header **Amended** entry, §5 and §7's pin-test line; DEPLOYMENT.md `## Extraction dependencies` (the rule, the procedure); `extract.py`'s generation comment.

## Evidence

- Goldens (`tests/fixtures/parity`, co-core-direct) and the real corpus (all HTML) unchanged on 6.21.2: the parity tests pass as committed.
- Wider: co-core 0.38.0's `PdfExtractor` over py-pdf's `sample-files` corpus on 6.19.0 vs 6.21.2, fingerprints compared, moved ones listed in the PR as the notice's evidence.

## Tradeoffs

- Bumping costs one silent re-baseline of every watched item: a real change coinciding with the first post-deploy check is absorbed, as at any co-core bump. Not bumping risks false change notices on PDFs and breaks the purity invariant. Bump.
- Alternative: wait for a co-core release that floors pypdf, so the co-core bump moves the version. Leaves the advisories live meanwhile; and the stack test is still wanted.

## Ship

PR (`Closes #71`) → CI → FF merge → `git pull --ff-only` → `scripts/deploy.sh`. Notice to Watcher before deploy (`0.38.0+2`, the reason, the sample-files evidence): needs operator approval to post. Re-run `uv audit` at ship time. Live checks: `starting` shows `processor_version` `0.38.0+2`, `child_containment: required`; pypdf 6.21.2 in `/srv/processor/live/.venv`; next command acks; drift `ok`.

## Open questions

1. Generation bump vs not: recommended bump (above); operator's call.
2. The report-only lock-audit workflow (#71 scope item 2): its own issue if wanted.
