# Agent Skills

Vendored by [`managing-skills`](../skills/managing-skills/SKILL.md): one git submodule per upstream, one symlink per skill. [agentskills.io](https://agentskills.io) layout. Adopted in #3; broker#15 is the cohort precedent.

## Layout

| Path | What it is |
|---|---|
| `skills-vendor/gregoryfoster-skills/` | Submodule of [gregoryfoster/skills](https://github.com/gregoryfoster/skills). Read-only: change upstream, bump the pointer |
| `skills-vendor/obra-superpowers/` | Submodule of [obra/superpowers](https://github.com/obra/superpowers). Same rule |
| `skills/<name>` | agentskills.io discovery: a symlink `../skills-vendor/<owner>-<repo>/skills/<name>`, or a committed override |
| `.claude/skills/<name>` | Claude Code discovery: always `../../skills/<name>`, so an override shadows the vendor copy in both systems |
| `.skills/doctor.sh` | A real file, not a symlink (it would dangle exactly when needed). Re-links dangling vendor symlinks by initialising the submodules; `reviewing-*` / `shipping-*` run it as preflight |
| `.skills/merge_mode`, `deploy_command`, `doc-sensitive-paths`, `doc-sections`, `gate-stamp-inputs` | `shipping-work`'s knobs (#115): [§ Shipping](#shipping-shipping-work). [tests/test_shipping_knobs.py](../tests/test_shipping_knobs.py) reads the first two through the skill's own readers |
| `.claude/hooks/gh-env.sh`, `scripts/env-value.sh` | Ours, not vendored: `GH_TOKEN` from `.env`, as data, for the session's `gh` calls (the refresh hook finds its own: [§ Refresh](#refresh)). A replica of [gregoryfoster/skills#334](https://github.com/gregoryfoster/skills/issues/334)'s `using-github-cli`; adopt that once it ships, and the hook becomes its vendor symlink |
| `.claude/hooks/context-budget-guard.sh`, `.github/workflows/context-cadence.yml`, `.gitattributes`, `.skills/context-*` | `curating-context`'s write guard, weekly measurement and ledger (#126): [§ Context budget](#context-budget) |
| `.skills/worktree_venv` | `none`: each worktree provisions its own `.venv` (`using-git-worktrees` → *Venv linking*). Set when this checkout was the unit's `WorkingDirectory=`; since #2 the unit runs a release, and the knob stays only so worktrees never share the checkout's venv |

Every vendor symlink dangles until the submodules are initialised: a clone without `--recurse-submodules`, a fresh `git worktree add`. Run `bash .skills/doctor.sh`. Adding a skill means both entries. [tests/test_skills.py](../tests/test_skills.py) pins the chain, the override frontmatter and both hook halves.

Ruff `extend-exclude`s `skills-vendor/`: upstream's Python fails our lint, and the refresh hook moves it unreviewed. Pytest's `testpaths` never reaches it.

## Refresh

A `SessionStart` hook refreshes `skills-vendor/` at most once per UTC day, **on `main` only**. Since #115 `.skills/merge_mode` is `pr`, and the hook reads it ([gregoryfoster/skills#560](https://github.com/gregoryfoster/skills/issues/560)), so it **never pushes to `main`**. It builds the bump (`skills-vendor/` and `.skills/doctor.sh`) in a private index, pushes it to `skills/submodule-refresh`, and opens or updates one PR with `gh`. That PR merges like any other ([§ Merging under `pr`](#merging-under-pr)). Local `main` never moves, and the content is put back on `main`'s pointer. A refusal (no `gh`, `gh` unauthenticated, a rejected push) is reported once and spends the day. It never pulls, never force-pushes, never blocks a session. Log: `.git/skills-update.log`.

**Its token.** The hook finds `gh`'s token itself ([gregoryfoster/skills#576](https://github.com/gregoryfoster/skills/issues/576), fixed in `38f378dc`): `GH_TOKEN` in its environment, else `GH_TOKEN` in the primary checkout's `.env` (parsed as data, never sourced), else `gh`'s stored login. A token from the `.env` goes to its `gh` calls and the https remote's git calls only, never into its environment, so the bumped pointer's own `install-doctor.sh` never sees it. SessionStart hooks run in parallel, so `gh-env.sh`'s export never reaches it either. Its registration is the canonical command, with no token ([tests/test_gh_env.py](../tests/test_gh_env.py)); #115's prefix (`GH_TOKEN="${GH_TOKEN:-$(bash scripts/env-value.sh GH_TOKEN)}"`) bridged the old hook and is retired: don't put it back. Each run that reaches the PR path logs `pr: gh's token comes from <primary checkout>/.env; --repo github.com/CannObserv/processor`. If `.skills/doctor.sh` prints `the refresh hook may have no token` at session start, the `.env` and `gh`'s login both came up empty. There is no stored `gh` login and no second copy of the token, as #334 decides.

**Its remote.** The hook passes `gh --repo <host>/<owner>/<repo>`, built from the configured `remote.origin.url`; an SSH host goes through `ssh -G` to its `HostName` (#576), so the deploy-key alias `github-processor` gives `--repo github.com/CannObserv/processor`, with or without a user part. This checkout's origin stays `github-processor:CannObserv/processor.git`, without the user (operator, 2026-11-05 and 2026-11-06, #115). The old hook needed that, and nothing depends on it now: `~/.ssh/config`'s `Host github-processor` sets `User git`, so git still pushes over the deploy key. A `set-url` back to `git@github-processor:…` would also work, but no real run has used that form. Check with `git config --get remote.origin.url`.

Two artifacts, and only the second makes it run: the `.claude/hooks/skills-submodule-update.sh` symlink and its entry in `.claude/settings.json`. The symlink alone looks installed and refreshes nothing (gregoryfoster/skills#167).

| To | Run |
|---|---|
| Check both halves | `bash skills/managing-skills/scripts/install-refresh.sh --check` |
| Remove both | `bash skills/managing-skills/scripts/install-refresh.sh --uninstall` |
| Refresh by hand | on a branch in a worktree, landed as a PR (`merge_mode pr`): `git submodule update --init --remote --merge -- skills-vendor/` (`--init` is load-bearing: without it an unregistered submodule is skipped with exit 0) |
| Hold one submodule | a `<submodule-path> <commit-ish>` line in `.skills/skills-pin`; never suspend the hook |

The first session in a fresh clone or worktree fails the hook with exit 127 (hooks run in parallel; nothing inits the submodule first). `bash .skills/doctor.sh` once fixes it.

## Selection

**gregoryfoster-skills (16):** `auditing-ci-cost` (CI since #12), `coordinating-epics` (#24 since #52), `curating-context`, `enforcing-architecture` (`reviewing-architecture` delegates to it), `init-socraticode`, `maintaining-host-disk` (the host's disk GC and headroom sensor since #82: [MONITORING.md § Disk GC](MONITORING.md#disk-gc)), `managing-skills`, `orchestrating-issue-backlog`, `patching-hosts` (the host's patch posture since #77: `.skills/patching-hosts`, [DEPLOYMENT.md § Patching](DEPLOYMENT.md#patching)), `reviewing-architecture`, `reviewing-code` (the stack-agnostic fallback, linked in #77 for #76's CR 11), `reviewing-code-python-fastapi`, `shipping-work` (since #115; was its FastAPI variant), `using-git-worktrees`, `using-mayfly-chat`, `writing-plans`.

**obra-superpowers (12):** `brainstorming`, `dispatching-parallel-agents`, `executing-plans`, `finishing-a-development-branch`, `receiving-code-review`, `requesting-code-review`, `subagent-driven-development`, `systematic-debugging`, `test-driven-development`, `using-superpowers`, `verification-before-completion`, `writing-skills`.

- **Review is the `-python-fastapi` variant, though processor isn't FastAPI** (as broker). `reviewing-code` triggers on `CR` too, but it is upstream's fallback for a stack with no variant, so `CR` here is still `reviewing-code-python-fastapi`. Its Alembic/route steps key on paths processor lacks; the `deploy/` review checks fit.
- **Ship is the unified `shipping-work`** ([gregoryfoster/skills#537](https://github.com/gregoryfoster/skills/issues/537)): [§ Shipping](#shipping-shipping-work).
- **`using-mayfly-chat` needs Node ≥ 18**; its wrapper exits 4 without it. co-processor has Node 24 LTS from NodeSource apt, installed 2026-09-30 (#6). Linked because the cohort adopts it together (gregoryfoster/skills#302). A channel URL is read/write/delete access: never commit one.
- **Superpowers' `using-git-worktrees` and `writing-plans` are not linked**: gregoryfoster's own, same-named, win. So brainstorming's hand-off to `superpowers:writing-plans` lands plans in `docs/plans/` (AGENTS.md → *Brainstorming* maps the name; a test holds every `superpowers:<name>` to a link or a mapping). Its filename is `YYYY-MM-DD-<topic-slug>.md`: start the topic with the issue number, so the slug is AGENTS.md's `<issue>-<slug>` (e.g. `2026-09-29-1-processor-v1.md`).

Not linked: `init-project-fastapi` (scaffolder), `vendoring-openapi-client` (no sibling HTTP API called), the other stack variants of review, the deprecated `shipping-work-*` variants, `diagnosing-superpowers` (no sibling links it). The daily refresh never adds symlinks, so a newly published skill is a manual link: diff `ls skills-vendor/*/skills/` against this list.

## Overrides

None since #62. `brainstorming` was one, synced from v6.4.2 to move the design doc to `docs/specs/`, until v7 deferred that path to the partner's preferences; AGENTS.md → *Brainstorming* states them. Where upstream defers, a stated preference beats an override: it needs no re-sync.

An override is a regular directory in `skills/` whose `SKILL.md` names `overrides:` and `synced-from:` (tests/test_skills.py holds it to both). The doctor warns when the vendor moves past `synced-from:`. Re-sync by reapplying the delta onto the new upstream text (never the reverse), then bump `synced-from:`: `managing-skills` → *Updating a local override*. Kept minimal on purpose: the heavier cohort forks each needed re-sync issues.

## Worktrees

A new worktree has no venv (`worktree_venv` is `none`) and no submodules. co-core comes from the co-pypi index (#121): uv's cache is shared, so a sync usually needs no token, and when it needs to download, the wrapper mints one. `worktree-create.sh --new` also cuts from local `HEAD`, which lags `origin/main` between pulls. From this checkout:

```bash
git fetch origin && git branch <n>-<slug> origin/main
cd "$(bash skills/using-git-worktrees/scripts/worktree-create.sh <n>-<slug>)"
bash .skills/doctor.sh                                   # initialises both submodules
bash scripts/co-pypi.sh uv sync
```

Destroying one needs `--force` once its submodules are initialised (git refuses otherwise); check `git status` is clean first.

## Shipping: `shipping-work`

One skill since #115, which detects the stack: `Detected: toolchain uv`, with no framework, since `pyproject.toml` declares neither FastAPI nor Click. Its gate runs what the old variant's did (`ruff check`, `ruff format --check`, `pytest` minus `integration`, here by the per-run plugin), plus an import of `processor` (row 7 of upstream's `references/gate-topology.md`) and `--no-cov` (row 13). It has no `-x` (row 10) and runs serial on 2 vCPU (row 14; no xdist here, so `.skills/test-workers` stays uncommitted). A pass is stamped on the exact tree (row 15). Integration tests are CI's: the gate never runs them.

| Knob | Value | Why |
|---|---|---|
| `merge_mode` | `pr` | Work lands through a PR, never a local merge. Steps 2.5–8 take their `pr` lines, and `using-git-worktrees` Phase 4 is skipped |
| `deploy_command` | `setsid -w scripts/deploy.sh` | The release deploy (#2). `-w` keeps the deploy's exit code (broker#123). The skill appends the pushed SHA |
| `doc-sensitive-paths`, `doc-sections` | tailored | Committed together, or every hit says which half is the default. A dead entry, a missing doc or a missing heading fails `tests/test_shipping_knobs.py` |
| `gate-stamp-inputs` | `.env` | Replaces the default, so `.env` is named again (`tests/test_child.py` probes it). `.wheelhouse/*` left with #121: no untracked file decides which co-core installs |
| `import-targets`, `pre-ship-uv-args` | absent | The default `processor` is right, and CI's `uv run pytest` takes no group |

### Merging under `pr`

The PR pass (Steps 1–7) ends with the PR open. `Closes #<n>` goes in the PR body, or `Refs #<n>` while a live check remains (the rule since #35, which upstream's `pr-mode.md` now writes for production-facing issues). The merge is **not** GitHub's button: rebase-and-merge rewrites the SHAs, so CI's tested SHA wouldn't be what ships, and squashing loses the `CR N` commits (#9). Once CI is green on the PR's head, from this checkout: `git merge --ff-only origin/<branch> && git push origin main` (a merge commit if `main` moved, #7). GitHub then marks the PR merged.

The post-merge pass runs as written. Its `git pull --ff-only` is a no-op after the push. Step 8 then offers `run-deploy-command.sh`, which runs only on `main` equal to `origin/main`. **It offers after every merge, whatever it touched.** Decline unless the merge touched a `processor.drift.RUNTIME_*` path (AGENTS.md → Workflow). A needless deploy is safe, but it restarts `processor`. The deploy can outlast a 10-minute tool call (it waits up to 600 s on CI), so run it in the background and read `journalctl -t processor-deploy` for the verdict. `scripts/deploy.sh` uses a bare `sudo`: exedev's is passwordless, and under `setsid` with no tty it fails rather than prompts. `worktree-destroy.sh` accepts the fast-forward by ancestry.

## Context budget

`curating-context` holds `AGENTS.md` to 6,000 tokens and each live doc under `docs/` (not `plans/`, `specs/`) to 10,000 (`.skills/context-budget`, `.skills/context-doc-budget`). Two surfaces keep a curation's ground, installed by #126 (operator, 2026-11-08):

- **The write guard.** A `PostToolUse` hook on `Edit|Write|MultiEdit`, symlinked into the vendored skill. It never blocks. It speaks only when an edit grows a file past its budget (`WARN`), or into its last 10% (`NEAR`). Every decision goes to `$(git rev-parse --absolute-git-dir)/context-budget.log`, one per worktree. A shell redirect (`cat >>`, `sed -i`) escapes it; `reviewing-code*`'s `context-delta.sh` sees the branch whole.
- **The weekly measurement.** `context-cadence.yml`, Tuesdays 16:14 UTC and on dispatch. It measures and never curates. It records one `baseline:scheduled` row in `.skills/context-metrics.jsonl`, and warns (`::warning::`) when a file is over budget, or seams or counts accrued. Under `merge_mode pr` it pushes `context/weekly-measurement` and keeps one standing PR open, merged like any other ([§ Merging under `pr`](#merging-under-pr)). A PR opened with `GITHUB_TOKEN` starts no CI: close and reopen it to run the checks. It needs the `ANTHROPIC_API_KEY` repository secret, and *Allow GitHub Actions to create and approve pull requests* (Settings → Actions → General). Without the secret it fails at its first step and records nothing.

The workflow is generated: re-run `install-cadence.sh`, don't edit it. A re-render keeps the `actions/checkout` SHA pin ([DEPENDENCIES.md § CI pins and Dependabot](DEPENDENCIES.md#ci-pins-and-dependabot)), and Dependabot moves it. Its `runs-on: ubuntu-latest` is the template's, and a re-render writes it back. The job reads Markdown with bash, curl and python3 and builds nothing, so the node's image doesn't apply.

`.gitattributes` merges the ledger by `union`, the ratio file by `ours`, and the counts file by `context-counts` (newest row wins). The last two name drivers in git config, which doesn't travel with a clone: the installer sets them, from the main checkout only (worktrees share its config).

| To | Run |
|---|---|
| Check the guard / the cadence | `bash skills/curating-context/scripts/install-guard.sh --check` / `install-cadence.sh --check` |
| Measure by hand | `bash skills/curating-context/scripts/measure-context.sh --exact --no-write` |
| Run the week's job now | `gh workflow run context-cadence.yml` |
| Remove either | `--uninstall` on its installer; the ledger stays |

## Not installed

Separate decisions, as in broker#15: SocratiCode indexing and its two hooks (#5), and processor's row on upstream's `.skills/cohort` roster ([gregoryfoster/skills#343](https://github.com/gregoryfoster/skills/issues/343)). The tailored doc-check lists were one until #115.
