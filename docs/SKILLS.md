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
| `.claude/hooks/gh-env.sh`, `scripts/env-value.sh` | Ours, not vendored: `GH_TOKEN` from `.env`, as data, for `gh` ([§ Refresh](#refresh)). A replica of [gregoryfoster/skills#334](https://github.com/gregoryfoster/skills/issues/334)'s `using-github-cli`; adopt that once it ships, and the hook becomes its vendor symlink |
| `.skills/worktree_venv` | `none`: each worktree provisions its own `.venv` (`using-git-worktrees` → *Venv linking*). Set when this checkout was the unit's `WorkingDirectory=`; since #2 the unit runs a release, and the knob stays only so worktrees never share the checkout's venv |

Every vendor symlink dangles until the submodules are initialised: a clone without `--recurse-submodules`, a fresh `git worktree add`. Run `bash .skills/doctor.sh`. Adding a skill means both entries. [tests/test_skills.py](../tests/test_skills.py) pins the chain, the override frontmatter and both hook halves.

Ruff `extend-exclude`s `skills-vendor/`: upstream's Python fails our lint, and the refresh hook moves it unreviewed. Pytest's `testpaths` never reaches it.

## Refresh

A `SessionStart` hook refreshes `skills-vendor/` at most once per UTC day, **on `main` only**. Since #115 `.skills/merge_mode` is `pr`, and the hook reads it ([gregoryfoster/skills#560](https://github.com/gregoryfoster/skills/issues/560)), so it **never pushes to `main`**. It builds the bump (`skills-vendor/` and `.skills/doctor.sh`) in a private index, pushes it to `skills/submodule-refresh`, and opens or updates one PR with `gh`. That PR merges like any other ([§ Merging under `pr`](#merging-under-pr)). Local `main` never moves, and the content is put back on `main`'s pointer. A refusal (no `gh`, `gh` unauthenticated, a rejected push) is reported once and spends the day. It never pulls, never force-pushes, never blocks a session. Log: `.git/skills-update.log`.

**Its token.** `gh` reads `GH_TOKEN` from the environment, and SessionStart hooks run in parallel, so `gh-env.sh`'s export never reaches this one. Its registration reads the token itself: `GH_TOKEN="${GH_TOKEN:-$(bash scripts/env-value.sh GH_TOKEN)}"` before the vendored script ([tests/test_gh_env.py](../tests/test_gh_env.py)). `install-refresh.sh --check` accepts that entry. An install-mode re-run rewrites it to the canonical command, which drops the token: put the prefix back. There is no stored `gh` login and no second copy of the token, as #334 decides.

**Its remote.** The hook passes `gh --repo <host>/<owner>/<repo>`, built from the configured `remote.origin.url` (not the `insteadOf`-expanded one), but only for a URL with a user part (`user@host:path`) or a scheme. An SSH alias written `git@github-processor:…` gives `gh` host `github-processor`, which it can't reach ("error connecting to github-processor"). So this checkout's origin is the alias **without** the user, `github-processor:CannObserv/processor.git` (operator, 2026-11-05, #115; archiver's shape). The hook then omits `--repo`, and `gh` resolves `CannObserv/processor` from the checkout. `~/.ssh/config`'s `Host github-processor` sets `User git`, so git still pushes over the deploy key. This is machine-local config: a re-clone or a `set-url` back to `git@…` brings the failure back. Check with `git config --get remote.origin.url`.

Two artifacts, and only the second makes it run: the `.claude/hooks/skills-submodule-update.sh` symlink and its entry in `.claude/settings.json`. The symlink alone looks installed and refreshes nothing (gregoryfoster/skills#167).

| To | Run |
|---|---|
| Check both halves | `bash skills/managing-skills/scripts/install-refresh.sh --check` |
| Remove both | `bash skills/managing-skills/scripts/install-refresh.sh --uninstall` |
| Refresh by hand | on a branch in a worktree, landed as a PR (`merge_mode pr`): `git submodule update --init --remote --merge -- skills-vendor/` (`--init` is load-bearing: without it an unregistered submodule is skipped with exit 0) |
| Hold one submodule | a `<submodule-path> <commit-ish>` line in `.skills/skills-pin`; never suspend the hook |

The first session in a fresh clone or worktree fails the hook with exit 127 (hooks run in parallel; nothing inits the submodule first). `bash .skills/doctor.sh` once fixes it.

## Selection

**gregoryfoster-skills (16):** `auditing-ci-cost` (CI since #12), `coordinating-epics` (#24 since #52), `curating-context`, `enforcing-architecture` (`reviewing-architecture` delegates to it), `init-socraticode`, `maintaining-host-disk` (the host's disk GC and headroom sensor since #82: [DEPLOYMENT.md § Disk GC](DEPLOYMENT.md#disk-gc)), `managing-skills`, `orchestrating-issue-backlog`, `patching-hosts` (the host's patch posture since #77: `.skills/patching-hosts`, [DEPLOYMENT.md § Patching](DEPLOYMENT.md#patching)), `reviewing-architecture`, `reviewing-code` (the stack-agnostic fallback, linked in #77 for #76's CR 11), `reviewing-code-python-fastapi`, `shipping-work` (since #115; was its FastAPI variant), `using-git-worktrees`, `using-mayfly-chat`, `writing-plans`.

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

A new worktree has no venv (`worktree_venv` is `none`), no `.wheelhouse` (git-ignored; `uv.lock` records co-core as `registry = ".wheelhouse"`) and no submodules. `worktree-create.sh --new` also cuts from local `HEAD`, which lags `origin/main` between pulls. From this checkout:

```bash
git fetch origin && git branch <n>-<slug> origin/main
cd "$(bash skills/using-git-worktrees/scripts/worktree-create.sh <n>-<slug>)"
bash .skills/doctor.sh                                   # initialises both submodules
ln -s "$(git worktree list | head -1 | awk '{print $1}')/.wheelhouse" .wheelhouse && uv sync
```

Destroying one needs `--force` once its submodules are initialised (git refuses otherwise); check `git status` is clean first.

## Shipping: `shipping-work`

One skill since #115, which detects the stack: `Detected: toolchain uv`, with no framework, since `pyproject.toml` declares neither FastAPI nor Click. Its gate runs what the old variant's did (`ruff check`, `ruff format --check`, `pytest` minus `integration`, here by the per-run plugin), plus an import of `processor` (row 7 of upstream's `references/gate-topology.md`) and `--no-cov` (row 13). It has no `-x` (row 10) and runs serial on 2 vCPU (row 14; no xdist here, so `.skills/test-workers` stays uncommitted). A pass is stamped on the exact tree (row 15). Integration tests are CI's: the gate never runs them.

| Knob | Value | Why |
|---|---|---|
| `merge_mode` | `pr` | Work lands through a PR, never a local merge. Steps 2.5–8 take their `pr` lines, and `using-git-worktrees` Phase 4 is skipped |
| `deploy_command` | `setsid -w scripts/deploy.sh` | The release deploy (#2). `-w` keeps the deploy's exit code (broker#123). The skill appends the pushed SHA |
| `doc-sensitive-paths`, `doc-sections` | tailored | Committed together, or every hit says which half is the default. A dead entry, a missing doc or a missing heading fails `tests/test_shipping_knobs.py` |
| `gate-stamp-inputs` | `.env`, `.wheelhouse/*` | Replaces the default, so `.env` is named again (`tests/test_child.py` probes it). The wheelhouse is untracked and decides which co-core installs |
| `import-targets`, `pre-ship-uv-args` | absent | The default `processor` is right, and CI's `uv run pytest` takes no group |

### Merging under `pr`

The PR pass (Steps 1–7) ends with the PR open. `Closes #<n>` goes in the PR body, or `Refs #<n>` while a live check remains (the rule since #35, which upstream's `pr-mode.md` now writes for production-facing issues). The merge is **not** GitHub's button: rebase-and-merge rewrites the SHAs, so CI's tested SHA wouldn't be what ships, and squashing loses the `CR N` commits (#9). Once CI is green on the PR's head, from this checkout: `git merge --ff-only origin/<branch> && git push origin main` (a merge commit if `main` moved, #7). GitHub then marks the PR merged.

The post-merge pass runs as written. Its `git pull --ff-only` is a no-op after the push. Step 8 then offers `run-deploy-command.sh`, which runs only on `main` equal to `origin/main`. **It offers after every merge, whatever it touched.** Decline unless the merge touched a `processor.drift.RUNTIME_*` path (AGENTS.md → Workflow). A needless deploy is safe, but it restarts `processor`. The deploy can outlast a 10-minute tool call (it waits up to 600 s on CI), so run it in the background and read `journalctl -t processor-deploy` for the verdict. `scripts/deploy.sh` uses a bare `sudo`: exedev's is passwordless, and under `setsid` with no tty it fails rather than prompts. `worktree-destroy.sh` accepts the fast-forward by ancestry.

## Not installed

Separate decisions, as in broker#15: `curating-context`'s weekly cadence workflow and `PostToolUse` write guard, SocratiCode indexing and its two hooks (#5), and processor's row on upstream's `.skills/cohort` roster ([gregoryfoster/skills#343](https://github.com/gregoryfoster/skills/issues/343)). The tailored doc-check lists were one until #115.
