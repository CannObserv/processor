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

**co-core is pinned exactly (`==0.19.7`); Processor owns the pin** (spec D5, §5; since watcher#350 nothing else extracts). `processor_version` = `"<co-core version>+<LOCAL_GENERATION>"`; a pin test makes every bump a deliberate, test-failing act. Never let a dependency bot or `uv lock --upgrade` move co-core. A bump gives Watcher notice (it re-baselines on `processor_version`). It is coordinated with Watcher only if it changes the contract: the `content.process` / `content.derived` models, `canonical_text` semantics, or `resolve_dispatch_essence`. Procedure: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#bumping-co-core).

**Cannobserv wheelhouse.** `co-core`, `co-core-aio`, `co-core-sync` resolve from `./.wheelhouse` (git-ignored) via `[tool.uv] find-links`. Populate it before any `uv` command:

```bash
uv run --no-project --no-config --with 'google-cloud-storage>=2,<4' python scripts/sync_wheelhouse.py  # mirror gs://co-gcs-pypi (needs co-pypi-reader ADC)
set -a; . ./.env; set +a; scripts/build_wheelhouse.sh                                                  # or: build from the cannobserv tag
```

find-links locks by filename, not hash, so either source satisfies the same `uv.lock`. After the first `uv sync` in a fresh checkout, `uv sync --reinstall-package processor` if `import processor` fails.

## Infrastructure

| Thing | Value |
|---|---|
| VM | exe.dev `co-processor` (8 GB, 2 vCPU), tag `processor` |
| Broker | `broker:6379`, by MagicDNS name, never by address. Tailscale runs `--accept-dns=true`, with an OOM drop-in for tailscaled: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#tailscale-dns) |
| Scratch bus | `redis-server` on `localhost:6379` — tests and smoke runs only |
| Input | `gs://co-gcs-blobs` (Replicator's raw blobs), read-only |
| Output | `gs://co-gcs-processor/blobs/<sha256>.bin`, write-if-absent, **never deleted** |
| Node.js | 24 LTS from NodeSource apt, agent tooling only (mayfly, SocratiCode): `deploy/nodesource.sh install\|check`, [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#nodejs-agent-tooling-only) |
| CI | GitHub Actions [.github/workflows/ci.yml](.github/workflows/ci.yml): lint, then the full suite against `redis:7.0.15` (the broker's, not 7.2: CLIENT SETINFO; upstream EOL, the next is broker#85's call), pulled from `mirror.gcr.io`, never anonymously from Docker Hub (#43). Keyless WIF to `co-pypi-reader` needs the org variable `GCP_WIF_PROVIDER` shared with this repo and a `roles/iam.workloadIdentityUser` binding for `principalSet://…/attribute.repository/CannObserv/processor` |
| Drift check | `processor-drift.timer`, hourly: does live lag `origin/main` in code that runs (`processor.drift.RUNTIME_*`)? Checks in to Status's `co-processor-drift` (`http://status:9000`, status#24); `alert` past 8 h. [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#the-drift-check) (#35) |
| Liveness | `processor run` checks in `ok` to Status's `co-processor-live` every 5 min, only while the consume loop progressed within 605 s; silence past the 900 s grace pages. Daemon thread, 10 s bound: Status never touches consumption. [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#liveness) (#39) |
| Service | systemd unit `processor` — [deploy/processor.service](deploy/processor.service), runbook [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md); live since 2026-10-02. Runs as user `processor` (no sudo, not in `docker`, no home) from `/srv/processor/live` → `releases/<build>`, a read-only release `scripts/deploy.sh` builds from a commit on `origin/main` (#2; the cohort standard, broker#22 / status#9). **Never from a checkout** |

**Tests never touch the real broker or real buckets.** Use the scratch Redis, co-core's `LocalBlobStore`, and fakes. Since #8 `broker` resolves on this VM, so a test that connects names `localhost` (or `broker.invalid` for name-resolution cases), never `broker`. `processor` cannot and must not `XADD content.process` on the broker.

**The bus.** Group `processor.process` on `content.process`, created with `ensure_group` from `$`. **Hard ordering:** the group must exist before Watcher (watcher#325) issues its first command — a group created later skips earlier entries. Publishes `content.derived`; dead-letters to `content.process.dlq` and drains it.

**Failure classes (spec §4).** Deterministic errors publish `processing_failed` with `terminal=true` and ack. Infrastructure errors — GCS 5xx/429/timeout/auth, broker `NOPERM`, broker `OOM command not allowed`, connection loss — publish **nothing** and leave the entry pending for reclaim. `NOPERM` and `OOM` are `ResponseError`s, not connection errors: keep both transient. Order is always **store → publish → ack**.

**Extraction runs in a killable child process** (`python -I -m processor._child`: timeout, `RLIMIT_AS`, `oom_score_adj` 1000, scrubbed env, allowlisting unpickler for its result), never a thread. The child is untrusted: it parses untrusted documents. **It contains itself** (`processor._contain`, #2, spec §3): Landlock (read-only on an allowlist derived from `sys.path`, no TCP, scoped signals) and seccomp (no socket at all). `processor run` is undumpable, and refuses to start where the child can't be contained (`CO_PROCESSOR_CHILD_CONTAINMENT=required`, the default). Keep the child's imports inside the allowlist: a new extractor that reads a data file outside it fails under containment, and the whole-corpus test in `tests/test_child.py` catches that. The test suite runs `required` where the kernel allows it (the pytest header says which), and never skips containment on `co-processor`.

**Parity goldens** come from co-core's pure extract API directly (`scripts/gen_parity_goldens.py`), never from Processor's code. The script imports nothing from `processor`, and a test checks that. The v1 set was Watcher's, and the generator reproduces it byte for byte (`v1_provenance`, #47). Regenerate only at a co-core bump, and list every moved digest in the bump note. A failing parity test means the output diverged; do not regenerate the goldens to make it pass. The real corpus, `tests/fixtures/parity/real/` (#16), is Watcher's export verbatim and the only copy of its blobs (gone from `gs://co-gcs-blobs` after the TTL): never edit or regenerate it; a bump that moves its output says so in the bump note.

## Agent Skills

`gregoryfoster/skills` and `obra/superpowers` vendored under `skills-vendor/`, symlinked into `skills/` (agentskills.io) and `.claude/skills/` (Claude Code). Symlinks dangle until the submodules are initialised: `bash .skills/doctor.sh`. A `SessionStart` hook bumps them daily on `main` and pushes. Inventory, override, refresh: [docs/SKILLS.md](docs/SKILLS.md).

## Environment Files

1. `/etc/processor/` (root, dir 700, files 600) — production: `.env` (`CO_PROCESSOR_*`, the broker credential), read by systemd, never by the service user; the GCS key and the Status key, handed over by `LoadCredential=` (#2, #35, #39). Managed by the operator. Commands needing it run as the service: `processor_cli` in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
2. `.env` (repo root, git-ignored) — dev/agent secrets: `GH_TOKEN` (this repo), `GH_TOKEN_<REPO>` (sibling repos, read). **Never commit, never print values.**

Settings via pydantic-settings, prefix `CO_PROCESSOR_` — **never `os.getenv`**. Anything naming a shared external resource takes a service-prefixed name.

## Cross-repo Policy

Never edit sibling repos (`cannobserv`, `broker`, `watcher`, `replicator`, `archiver`, `observo`, …) from here. Identify the gap, draft the issue or comment, get explicit approval **for each post**, then post it. Read access to siblings: `GH_TOKEN=$GH_TOKEN_<REPO> gh …`.

## Common Commands

```bash
uv sync                                  # install deps (wheelhouse first)
uv run pytest                            # tests
uv run pytest -m integration             # bus tests against the scratch redis-server
uv run ruff check . && uv run ruff format --check .
scripts/deploy.sh [<build>]              # ship: CI green, then build a release from origin/main, switch, verify (as exedev)
```

## Conventions

**Commit messages:** `#<number> <type>: <description>` (or `<type>: <description>` without an issue). Types: feat, fix, refactor, docs, test, chore. **Shipping is `scripts/deploy.sh`** after the merge: it replaces the vendored shipping skill's "restart the service" step. What runs is `/srv/processor/live/REVISION`.

**Workflow:** every change is issue → branch → PR; never commit to `main` directly. Branch in a worktree (`using-git-worktrees`), not by switching this checkout: `scripts/deploy.sh` and the skills hook both run from it. Provisioning a worktree (venv, wheelhouse, submodules): [docs/SKILLS.md](docs/SKILLS.md#worktrees). The vendored ship/worktree skills' local merge to `main` does not apply here ([docs/SKILLS.md](docs/SKILLS.md)). Sole exception: the skills refresh hook's daily submodule bump. After a PR merges, `git pull --ff-only` in this checkout (while it lags `origin/main` the hook's push is rejected), then `scripts/deploy.sh` if the merge touched `src/`, `deploy/`, `scripts/deploy.sh`, `pyproject.toml` or `uv.lock` (the drift check alerts 8 h after such a push).

**Logging:** structured JSON, one record per line, cohort four-key floor: `timestamp` (ISO 8601 UTC), `level`, `logger`, `message`. Every command outcome logs `command_id`, `info_source_id`, reason, timings and the command's digests (fields: spec §4).

**Date & time:** all UTC; `YYYY-MM-DDTHH:MM:SS.ffffffZ`.

**General:** imports at file top; docstrings on public modules, classes, functions; tests mirror source (`src/processor/foo.py` → `tests/test_foo.py`); the pure core (`processors/`) does no I/O.

## Detail Docs

- [docs/specs/2026-09-29-processor-service-design.md](docs/specs/2026-09-29-processor-service-design.md) — the spec: decisions, grants, runtime, failure table, versioning, cutover, testing, contract quick reference
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — prerequisites, the broker credential handoff, `/etc/processor/`, releases and `scripts/deploy.sh`, the drift check, liveness, containment, install and rollback, operate, co-core bumps, Tailscale DNS, Node.js
- [docs/plans/](docs/plans/) — implementation plans
- [docs/SKILLS.md](docs/SKILLS.md) — vendored agent skills, the brainstorming override, the refresh hook
