# #5 — index processor on co-index's shared SocratiCode store

**Issue:** #5. **Brief:** the epic #24 hand-off comment on #5 (2026-10-10), and its refresh. **Prior art:** broker#17 (adoption) and broker#33 (the 1.16.0 pin), whose config and test this mirrors.

## Problem

Agents here have no semantic search, and processor's seams are cross-repo: Watcher issues `content.process`, Replicator's blobs are the input, Archiver/broker carry the bus. The cohort's other repos read each other through `includeLinked` on co-index's shared Qdrant; processor neither reads them nor is readable.

## Approach

`init-socraticode` with `STORE=external`, adapted to broker's shape at the same version:

- `.socraticode.json`: `projectId: processor`; `linkedProjects` the five adopted siblings, relative: `../archiver`, `../broker`, `../notifier`, `../replicator`, `../watcher`. observo links nothing processor reads; cannobserv (co-core), status and index have no collection.
- `.claude/settings.json`: the six-variable client `env` block plus `SOCRATICODE_SPEC=socraticode@1.16.0` (operator decision, 2026-10-10); the reminder and health hooks, symlinked by `managing-skills`' `install-hook.sh`. Same commit as `.socraticode.json`, never before it.
- `.socraticodeignore`: the vendored skill trees (`skills/brainstorming` is an upstream copy with one path changed, so `skills/` goes whole), both worktree roots, `.wheelhouse`, and `docs/plans/` (dated prose; it stays searchable as a context artifact).
- `.socraticodecontextartifacts.json`: AGENTS.md, the spec, DEPLOYMENT.md, SKILLS.md, SOCRATICODE.md, `docs/plans`, `deploy/`, `pyproject.toml`, the CI workflow.
- `AGENTS.md`: the marker-delimited policy block, variant A. `docs/SOCRATICODE.md`: the template, then repo notes (store, link stubs, one host per `projectId`, the pin, the memory decision).
- `tests/test_socraticode_config.py`: broker's guards and config tests, adapted. The env sources are the ones that reach a session's server: `/etc/processor/.env` is read by systemd for the service only, and is root-only, so it is not one.

## Memory: no `MemoryLow=`

Preflight (2026-10-10): cgroup2 is mounted without `memory_recursiveprot` and `system.slice` grants none, so a `MemoryLow=` on `processor.service` would be inert without a host drop-in on `system.slice` too. And reclaim is not the threat: OOM is, and the service already sits at `OOMScoreAdjust=-500` (about 64 MiB resident) while each child volunteers at +1000. The peak that endangered broker (2026-09-16) was the unpinned npm install at session start, which the pin removes. So no unit change and no deploy.

## Steps

1. Tests first (red): the guards, the config, the pin, the hooks.
2. `.socraticode.json`, then the `env` block and hooks (install-hook.sh), the manifest and ignore file. Green.
3. AGENTS.md block, `docs/SOCRATICODE.md`, a line in DEPLOYMENT.md's Node section.
4. PR; FF merge; `git pull --ff-only` in the main checkout.
5. Host, from the main checkout (exedev): the capped pre-install `~/.socraticode/pin` and the npx cache for the exact spec; marketplace and plugin; link stubs under `/home/exedev/`; restart the session.
6. **Operator gate:** the Qdrant key into `.claude/settings.local.json` (`scripts/install_qdrant_key.sh` from a read-only clone of CannObserv/index; prints `installed 64 chars`).
7. Preflight bare (env block ✓), index capped, verify: `Qdrant mode: external`, server command line `socraticode@1.16.0`, a processor search, and `includeLinked` results from **each** sibling, including 1.14.0-format collections from a 1.16.0 client.

## Known limits

- A 1.14.0 client that links `codebase_processor` reads zero rows without an error (notifier#72). No sibling links processor today.
- Worktrees carry no key and resolve `../<sib>` inside `.worktrees/` (observo#654): index and search from the main checkout.

## Open questions

None: the version and the linked set are decided; the key is the operator's.
