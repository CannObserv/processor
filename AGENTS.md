# processor — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Lead with the answer or action.

## Project Overview

The cohort's **headless discrete-transform service**. Consumes `content.process` from the cohort broker, runs one transform per command, stores the output by hash, publishes `content.derived`. v1 = Watcher's text extraction (`command.processor == "extract"`). No UI, no database. A command is a pure function of (input bytes, spec, `processor_version`).

Design of record: [docs/specs/2026-09-29-processor-service-design.md](docs/specs/2026-09-29-processor-service-design.md) (the spec). Tracking issue: #1. **Read the spec before changing behaviour**; it is self-contained and names every co-core symbol used.

**Owns:** transforms (co-core's HTML/PDF/CSV/XML/JSON extractors now; org adapters later), `gs://co-gcs-processor`, draining `content.process.dlq`. **Does not own:** spec authoring or interpretation (issuers), fetching (Replicator), change detection/diffing (Watcher), the registry (Archiver).

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first. Non-trivial work gets a plan in `docs/plans/YYYY-MM-DD-<issue>-<slug>.md`, reviewed before implementation.

**Brainstorming** (`skills/brainstorming`, superpowers' unmodified): these are the partner's preferences its *Where it goes* defers to.
- A change to the service's behaviour amends the spec: a dated **Amended** entry in its header, plus the sections it touches. A design doc of its own only for work the spec doesn't cover: `docs/specs/YYYY-MM-DD-<issue>-<topic>-design.md`, never `docs/superpowers/specs/`.
- `superpowers:<name>` means this repo's `skills/<name>`, whichever vendor it links. `superpowers:writing-plans` is gregoryfoster's `writing-plans`, which writes the plan above to `docs/plans/`; `superpowers:using-git-worktrees` is gregoryfoster's too (§ Conventions → Workflow).
- The builder check runs as written. On an amendment, hand it the spec and name the amended sections.
- Its sizes don't replace the rules above: a "small change" that is non-trivial still gets a plan, and every change goes issue → branch → PR.

## Environment & Tooling

Python 3.12, uv, hatchling src layout, pytest, ruff.

**co-core is pinned exactly (`==0.38.0`); Processor owns the pin** (spec D5, §5; since watcher#350 nothing else extracts). `processor_version` = `"<co-core version>+<LOCAL_GENERATION>"`; a pin test makes every bump a deliberate, test-failing act. Never let a dependency bot or `uv lock --upgrade` move co-core. A bump gives Watcher notice (it re-baselines on `processor_version`). It is coordinated with Watcher only if it changes the contract: the `content.process` / `content.derived` models or `canonical_text` semantics. Procedure: [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md#bumping-co-core). The extraction stack (co-core's `extract` extra, closure: pypdf, lxml, …) is held too: moving it outside a co-core bump bumps `LOCAL_GENERATION` (#71, [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md#extraction-dependencies)).

**The co-pypi index.** `co-core`, `co-core-aio`, `co-core-sync` resolve only from the explicit `co-pypi` Artifact Registry index (`pyproject.toml`, #121). The lock holds each file's sha256, and `uv sync --locked` refuses a miss. `uv lock`, and a `uv sync` that downloads, need the reader's token: `bash scripts/co-pypi.sh uv …` mints it for that one `uv`, and never prints it. After the first `uv sync` in a fresh checkout, `uv sync --reinstall-package processor` if `import processor` fails.

## Infrastructure

| Thing | Value |
|---|---|
| VM | exe.dev `co-processor` (8 GB, 2 vCPU), tag `processor` |
| Broker | `broker:6379`, by MagicDNS name, never by address. Tailscale runs `--accept-dns=true`, with an OOM drop-in for tailscaled: [docs/NETWORK.md](docs/NETWORK.md#tailscale-dns) |
| Scratch bus | `redis-server` on `localhost:6379` — tests and smoke runs only |
| Input | `gs://co-gcs-blobs` (Replicator's raw blobs), read-only |
| Output | `gs://co-gcs-processor/blobs/<sha256>.bin`, write-if-absent, **never deleted** |
| Service | systemd unit `processor` ([deploy/processor.service](deploy/processor.service)), as user `processor` (no sudo, not in `docker`, no home), from `/srv/processor/live` → `releases/<build>`, a read-only release `scripts/deploy.sh` builds from a commit on `origin/main` (#2). **Never from a checkout**. [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| CI | [.github/workflows/ci.yml](.github/workflows/ci.yml): lint (ruff, shellcheck), then the full suite against `redis:7.0.15`, the broker's, pulled from `mirror.gcr.io`, never anonymously from Docker Hub (#43). Pins, keyless WIF to `co-pypi-reader`, Dependabot (`github-actions` only): [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md#ci-pins-and-dependabot); shell lint: [§ Shell lint](docs/DEPENDENCIES.md#shell-lint) |
| Lock audit | [.github/workflows/audit.yml](.github/workflows/audit.yml), report-only, outside the deploy gate: [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md#the-lock-audit) |
| Status | Check-ins: drift (`processor-drift.timer`, hourly), liveness (`processor run`, 5 min), disk headroom ([deploy/disk-gc/disk-gc.conf](deploy/disk-gc/disk-gc.conf)): [docs/MONITORING.md](docs/MONITORING.md) |
| Patching | `automatic` (knob `.skills/patching-hosts`, #113): `-security` daily by unattended-upgrades; the rest in one approved run a week, `Tue 05:00–10:00` UTC. Never a bare `apt-get upgrade`. [docs/PATCHING.md](docs/PATCHING.md#patching) |
| Node.js | 24 LTS from NodeSource apt, agent tooling only: [docs/PATCHING.md](docs/PATCHING.md#nodejs-agent-tooling-only) |

## Hard Rules

**Tests never touch the real broker or real buckets.** Use the scratch Redis, co-core's `LocalBlobStore`, and fakes. Since #8 `broker` resolves on this VM, so a test that connects names `localhost` (or `broker.invalid` for name-resolution cases), never `broker`. `processor` cannot and must not `XADD content.process` on the broker.

**The bus.** Group `processor.process` on `content.process`, created with `ensure_group` from `$`. **Hard ordering:** the group must exist before Watcher (watcher#325) issues its first command — a group created later skips earlier entries. Publishes `content.derived`; dead-letters to `content.process.dlq` and drains it.

**Failure classes (spec §4).** Deterministic errors publish `processing_failed` with `terminal=true` and ack. Infrastructure errors — GCS 5xx/429/timeout/auth, broker `NOPERM`, broker `OOM command not allowed`, connection loss — publish **nothing** and leave the entry pending for reclaim. `NOPERM` and `OOM` are `ResponseError`s, not connection errors: keep both transient. Order is always **store → publish → ack**.

**Extraction runs in a killable child process** (`python -I -m processor._child`: timeout, `RLIMIT_AS`, `oom_score_adj` 1000, scrubbed env, allowlisting unpickler for its result), never a thread. The child is untrusted: it parses untrusted documents. **It contains itself** (`processor._contain`, #2, spec §3): Landlock (read-only on an allowlist derived from `sys.path`, no TCP, scoped signals) and seccomp (no socket at all). `processor run` is undumpable, and refuses to start where the child can't be contained (`CO_PROCESSOR_CHILD_CONTAINMENT=required`, the default). Keep the child's imports inside the allowlist: a new extractor that reads a data file outside it fails under containment, and the whole-corpus test in `tests/test_child.py` catches that. The test suite runs `required` where the kernel allows it (the pytest header says which), and never skips containment on `co-processor`.

**Parity goldens** come from co-core's pure extract API directly (`scripts/gen_parity_goldens.py`), never from Processor's code. Regenerate only at a co-core bump; a failing parity test means the output diverged, so never regenerate to make it pass. Never edit or regenerate the real corpus, `tests/fixtures/parity/real/`: the only copy of its blobs. Detail: [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md#parity-goldens).

## Agent Skills

`gregoryfoster/skills` and `obra/superpowers` vendored under `skills-vendor/`, symlinked into `skills/` (agentskills.io) and `.claude/skills/` (Claude Code). Symlinks dangle until the submodules are initialised: `bash .skills/doctor.sh`. A `SessionStart` hook proposes a daily bump as one PR. Inventory, refresh, worktrees, `.skills/` knobs: [docs/SKILLS.md](docs/SKILLS.md).

## Environment Files

1. `/etc/processor/` (root, dir 700, files 600) — production: `.env` (`CO_PROCESSOR_*`, the broker credential), read by systemd, never by the service user; the GCS key and the Status key, handed over by `LoadCredential=` (#2, #35, #39). Managed by the operator. Commands needing it run as the service: `processor_cli` in [docs/PROVISIONING.md](docs/PROVISIONING.md#etcprocessor).
2. `/etc/processor-deploy/co-pypi-reader.json` (`root:exedev 640`, dir 750) — the index reader's key, read by `scripts/co-pypi.sh` alone (#121).
3. `.env` (repo root, git-ignored) — dev/agent secrets: `GH_TOKEN` (this repo; `.claude/hooks/gh-env.sh` exports it), `GH_TOKEN_<REPO>` (sibling repos, read). **Never commit, never print values.**

Settings via pydantic-settings, prefix `CO_PROCESSOR_` — **never `os.getenv`**. Anything naming a shared external resource takes a service-prefixed name.

## Cross-repo Policy

Never edit sibling repos (`cannobserv`, `broker`, `watcher`, `replicator`, `archiver`, `observo`, …) from here. Identify the gap, draft the issue or comment, get explicit approval **for each post**, then post it. Read access to siblings: `GH_TOKEN=$GH_TOKEN_<REPO> gh …`.

## Common Commands

```bash
bash scripts/co-pypi.sh uv sync          # install deps (co-core from the co-pypi index)
uv run pytest                            # tests
uv run pytest -m integration             # bus tests against the scratch redis-server
uv run ruff check . && uv run ruff format --check .
bash scripts/shellcheck.sh               # our shell scripts, as CI's lint step
scripts/deploy.sh [<build>]              # ship: CI green, then build a release from origin/main, switch, verify (as exedev)
```

## Conventions

**Commit messages:** `#<number> <type>: <description>` (or `<type>: <description>` without an issue). Types: feat, fix, refactor, docs, test, chore. **Ship** with `shipping-work`: `merge_mode` `pr`, and its deploy is `scripts/deploy.sh` (`deploy_command`). What runs is `/srv/processor/live/REVISION`.

**Workflow:** every change is issue → branch → PR; never commit to `main` directly. Branch in a worktree (`using-git-worktrees`), not by switching this checkout: `scripts/deploy.sh` and the skills hook both run from it. Provisioning a worktree (venv, submodules): [docs/SKILLS.md](docs/SKILLS.md#worktrees). Merge by fast-forward push from this checkout ([docs/SKILLS.md](docs/SKILLS.md#merging-under-pr)); deploy only if the merge touched `src/`, `deploy/`, `scripts/deploy.sh`, `pyproject.toml`, `uv.lock` or `.skills/patching-hosts` (the drift check alerts 8 h after such a push).

**Logging:** structured JSON, one record per line, cohort four-key floor: `timestamp` (ISO 8601 UTC), `level`, `logger`, `message`. Every command outcome logs `command_id`, `info_source_id`, reason, timings and the command's digests (fields: spec §4).

**Date & time:** all UTC; `YYYY-MM-DDTHH:MM:SS.ffffffZ`.

**General:** imports at file top; docstrings on public modules, classes, functions; tests mirror source (`src/processor/foo.py` → `tests/test_foo.py`); the pure core (`processors/`) does no I/O.

## Detail Docs

- [docs/specs/2026-09-29-processor-service-design.md](docs/specs/2026-09-29-processor-service-design.md) — the spec: decisions, grants, runtime, failure table, versioning, cutover, testing, contract quick reference
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — releases and `scripts/deploy.sh`, the CI gate, rollback, operate
- [docs/PATCHING.md](docs/PATCHING.md) — patching (posture, window, the run, the patch-due check), Node.js
- [docs/PROVISIONING.md](docs/PROVISIONING.md) — prerequisites, the broker credential, GCP, `/etc/processor/` and `processor_cli`, containment, install
- [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) — CI pins and Dependabot, shell lint, the lock audit, co-core bumps, extraction dependencies, TLS trust
- [docs/MONITORING.md](docs/MONITORING.md) — the drift check, liveness, the disk GC, memory (no earlyoom)
- [docs/NETWORK.md](docs/NETWORK.md) — Tailscale DNS, tailscaled at boot, tailnet flaps (signature, rule)
- [docs/plans/](docs/plans/) — implementation plans
- [docs/SKILLS.md](docs/SKILLS.md) — vendored agent skills: inventory, overrides (none), the refresh hook, worktrees
- [docs/SOCRATICODE.md](docs/SOCRATICODE.md) — semantic search: tools, the shared store, link stubs, the pin, the memory decision

<!-- BEGIN socraticode-policy -->
## Code Exploration Policy

SocratiCode is the preferred semantic-search tool here once indexed (manifest
`.socraticodecontextartifacts.json`). Its MCP tools are **deferred** — schemas
load only after the `ToolSearch` prefetch that
`.claude/hooks/socraticode-reminder.sh` prints each session.

**Negative rule.** Use SocratiCode MCP tools first for semantic questions
("where is X", "how does Y work", "what depends on Z"). Reach for `grep`/`rg`
only on exact strings (error messages, log lines, known symbols). Reserve the
Explore subagent for path-pattern walks (`*.py` under `src/processor/`), not
semantic search.

| Goal | Tool |
|------|------|
| Where is X defined / how does Y work / what touches Z | `codebase_search` |
| Exact string or regex (errors, log lines, known symbols) | `grep` / `rg` |
| Imports/dependents of a file · blast radius of a change | `codebase_graph_query` / `codebase_impact` |

Full tool table, prefetch hook, per-tool guidance: [`docs/SOCRATICODE.md`](docs/SOCRATICODE.md).
<!-- END socraticode-policy -->
