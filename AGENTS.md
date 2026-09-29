# processor — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Lead with the answer or action.

## Project Overview

The cohort's **headless discrete-transform service**. Consumes `content.process` from the cohort broker, runs one transform per command, stores the output by hash, publishes `content.derived`. v1 = Watcher's text extraction (`command.processor == "extract"`). No UI, no database. A command is a pure function of (input bytes, spec, `processor_version`).

Design of record: [docs/specs/2026-09-29-processor-service-design.md](docs/specs/2026-09-29-processor-service-design.md) (the spec). Tracking issue: #1. **Read the spec before changing behaviour**; it is self-contained and names every co-core symbol used.

**Owns:** transforms (co-core's HTML/PDF/CSV extractors now; org adapters later), `gs://co-gcs-processor`, draining `content.process.dlq`. **Does not own:** spec authoring or interpretation (issuers), fetching (Replicator), change detection/diffing (Watcher), the registry (Archiver).

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first. Non-trivial work gets a plan in `docs/plans/YYYY-MM-DD-<issue>-<slug>.md`, reviewed before implementation.

## Environment & Tooling

Python 3.12, uv, hatchling src layout, pytest, ruff.

**co-core is pinned exactly (`==0.19.7`), in lockstep with Watcher** (spec D5, §5). `processor_version` = `"<co-core version>+<LOCAL_GENERATION>"`; a pin test makes every bump a deliberate, test-failing act. Never let a dependency bot or `uv lock --upgrade` move co-core; a bump is planned with Watcher and the parity corpus must pass unchanged.

**Cannobserv wheelhouse.** `co-core`, `co-core-aio`, `co-core-sync` resolve from `./.wheelhouse` (git-ignored) via `[tool.uv] find-links`. Populate it before any `uv` command. Until the `co-pypi-reader` key lands, build it from the release tag (needs `GH_TOKEN_CANNOBSERV`):

```bash
git clone -q --depth 1 --branch v0.19.7 "https://x-access-token:${GH_TOKEN_CANNOBSERV}@github.com/CannObserv/cannobserv.git" /tmp/cannobserv
for p in co-core co-core-aio co-core-sync; do (cd /tmp/cannobserv && uv build --package $p --wheel --out-dir ~/processor/.wheelhouse); done
```

find-links locks by filename, not hash, so wheels mirrored from `gs://co-gcs-pypi` swap in later without a lock change.

## Infrastructure

| Thing | Value |
|---|---|
| VM | exe.dev `co-processor` (8 GB, 2 vCPU), tag `processor` |
| Broker | `100.97.91.19:6379` — this VM runs Tailscale `--accept-dns=false`, so **not** the MagicDNS name `broker` |
| Scratch bus | `redis-server` on `localhost:6379` — tests and smoke runs only |
| Input | `gs://co-gcs-blobs` (Replicator's raw blobs), read-only |
| Output | `gs://co-gcs-processor/blobs/<sha256>.bin`, write-if-absent, **never deleted** |
| Service | systemd unit `processor` (not yet installed) |

**Tests never touch the real broker or real buckets.** Use the scratch Redis, co-core's `LocalBlobStore`, and fakes. `processor` cannot and must not `XADD content.process` on the broker.

**The bus.** Group `processor.process` on `content.process`, created with `ensure_group` from `$`. **Hard ordering:** the group must exist before Watcher (watcher#325) issues its first command — a group created later skips earlier entries. Publishes `content.derived`; dead-letters to `content.process.dlq` and drains it.

**Failure classes (spec §4).** Deterministic errors publish `processing_failed` with `terminal=true` and ack. Infrastructure errors — GCS 5xx/429/timeout/auth, broker `NOPERM`, broker `OOM command not allowed`, connection loss — publish **nothing** and leave the entry pending for reclaim. `NOPERM` and `OOM` are `ResponseError`s, not connection errors: keep both transient. Order is always **store → publish → ack**.

**Extraction runs in a killable child process** (spawn, timeout, `RLIMIT_AS`), never a thread.

## Environment Files

1. `/etc/processor/.env` (dir 700, file 600) — production secrets and settings: `CO_PROCESSOR_*`, broker credential, `GOOGLE_APPLICATION_CREDENTIALS`. Managed by the operator.
2. `.env` (repo root, git-ignored) — dev/agent secrets: `GH_TOKEN` (this repo), `GH_TOKEN_<REPO>` (sibling repos, read). **Never commit, never print values.**

Settings via pydantic-settings, prefix `CO_PROCESSOR_` — **never `os.getenv`**. Anything naming a shared external resource takes a service-prefixed name.

## Cross-repo Policy

Never edit sibling repos (`cannobserv`, `broker`, `watcher`, `replicator`, `archiver`, `observo`, …) from here. Identify the gap, draft the issue or comment, get explicit approval **for each post**, then post it. Read access to siblings: `GH_TOKEN=$GH_TOKEN_<REPO> gh …`.

## Conventions

**Commit messages:** `#<number> <type>: <description>` (or `<type>: <description>` without an issue). Types: feat, fix, refactor, docs, test, chore. Code on `main` is the deployed code.

**Logging:** structured JSON, one record per line, cohort four-key floor: `timestamp` (ISO 8601 UTC), `level`, `logger`, `message`. Every command outcome logs `command_id`, `info_source_id`, reason and timings.

**Date & time:** all UTC; `YYYY-MM-DDTHH:MM:SS.ffffffZ`.

**General:** imports at file top; docstrings on public modules, classes, functions; tests mirror source (`src/processor/foo.py` → `tests/test_foo.py`); the pure core (`processors/`) does no I/O.

## Detail Docs

- [docs/specs/2026-09-29-processor-service-design.md](docs/specs/2026-09-29-processor-service-design.md) — the spec: decisions, grants, runtime, failure table, versioning, cutover, testing, contract quick reference
- [docs/plans/](docs/plans/) — implementation plans
