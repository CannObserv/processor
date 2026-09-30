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
| `.skills/worktree_venv` | `none`: this checkout is the unit's `WorkingDirectory=`, so a worktree must not share its `.venv` (`using-git-worktrees` → *Venv linking*) |

Every vendor symlink dangles until the submodules are initialised: a clone without `--recurse-submodules`, a fresh `git worktree add`. Run `bash .skills/doctor.sh`. Adding a skill means both entries. [tests/test_skills.py](../tests/test_skills.py) pins the chain, the override frontmatter and both hook halves.

Ruff `extend-exclude`s `skills-vendor/`: upstream's Python fails our lint, and the refresh hook moves it unreviewed. Pytest's `testpaths` never reaches it.

## Refresh

A `SessionStart` hook refreshes `skills-vendor/` at most once per UTC day, **on `main` only**. It commits the pointer bump itself (staging only `skills-vendor/` and `.skills/doctor.sh`) **and pushes it to `main`**. That is the one exception to the PR workflow (AGENTS.md → Conventions). A rejected push is rolled back. Under the PR workflow that is the common case: merges happen on GitHub, so this checkout's `main` lags `origin/main` until someone runs `git pull --ff-only`, and until then every daily bump is rolled back (the refreshed content stays in the working tree; only the pointer stalls). It never pulls, never force-pushes, never touches a commit it didn't write, never blocks a session. Log: `.git/skills-update.log`.

Two artifacts, and only the second makes it run: the `.claude/hooks/skills-submodule-update.sh` symlink and its entry in `.claude/settings.json`. The symlink alone looks installed and refreshes nothing (gregoryfoster/skills#167).

| To | Run |
|---|---|
| Check both halves | `bash skills/managing-skills/scripts/install-refresh.sh --check` |
| Remove both | `bash skills/managing-skills/scripts/install-refresh.sh --uninstall` |
| Refresh by hand | `git submodule update --init --remote --merge -- skills-vendor/` (`--init` is load-bearing: without it an unregistered submodule is skipped with exit 0) |
| Hold one submodule | a `<submodule-path> <commit-ish>` line in `.skills/skills-pin`; never suspend the hook |

The first session in a fresh clone or worktree fails the hook with exit 127 (hooks run in parallel; nothing inits the submodule first). `bash .skills/doctor.sh` once fixes it.

## Selection

**gregoryfoster-skills (11):** `curating-context`, `enforcing-architecture` (`reviewing-architecture` delegates to it), `init-socraticode`, `managing-skills`, `orchestrating-issue-backlog`, `reviewing-architecture`, `reviewing-code-python-fastapi`, `shipping-work-python-fastapi`, `using-git-worktrees`, `using-mayfly-chat`, `writing-plans`.

**obra-superpowers (11 + override):** `dispatching-parallel-agents`, `executing-plans`, `finishing-a-development-branch`, `receiving-code-review`, `requesting-code-review`, `subagent-driven-development`, `systematic-debugging`, `test-driven-development`, `using-superpowers`, `verification-before-completion`, `writing-skills`; `brainstorming` as an override.

- **Review and ship are the `-python-fastapi` variants, though processor isn't FastAPI** (as broker). Their `pre-ship.sh` is processor's gate unmodified: `ruff check`, `ruff format --check`, `pytest` minus `integration`. The Alembic/route steps key on paths processor lacks; the `deploy/` review checks and the post-merge `systemctl restart processor` fit.
- **`using-mayfly-chat` needs Node ≥ 18, which co-processor lacks**; its wrapper exits 4 until installed. Linked because the cohort adopts it together (gregoryfoster/skills#302). A channel URL is read/write/delete access: never commit one.
- **Superpowers' `using-git-worktrees` and `writing-plans` are not linked**: gregoryfoster's own, same-named, win. So brainstorming's hand-off to `writing-plans` lands plans in `docs/plans/`. Its filename is `YYYY-MM-DD-<topic-slug>.md`: start the topic with the issue number, so the slug is AGENTS.md's `<issue>-<slug>` (e.g. `2026-09-29-1-processor-v1.md`).

Not linked: `init-project-fastapi` (scaffolder), `vendoring-openapi-client` (no sibling HTTP API called), `auditing-ci-cost` (no CI), the other stack variants of review/ship, `diagnosing-superpowers` (no sibling links it). The daily refresh never adds symlinks, so a newly published skill is a manual link: diff `ls skills-vendor/*/skills/` against this list.

## Overrides

| Skill | Overrides | Delta |
|---|---|---|
| `brainstorming` | `obra-superpowers/brainstorming` | `docs/superpowers/specs/` → `docs/specs/`, in `SKILL.md` and `spec-document-reviewer-prompt.md`. Otherwise upstream verbatim; `visual-companion.md` and `scripts/` are vendor symlinks. Observo's pattern |

The doctor warns when the vendor moves past `synced-from:`. Re-sync by reapplying the delta onto the new upstream text (never the reverse), then bump `synced-from:`: `managing-skills` → *Updating a local override*. Kept minimal on purpose: the heavier cohort forks each needed re-sync issues.

## Worktrees

A new worktree has no venv (`worktree_venv` is `none`), no `.wheelhouse` (git-ignored; `uv.lock` records co-core as `registry = ".wheelhouse"`) and no submodules. `worktree-create.sh --new` also cuts from local `HEAD`, which lags `origin/main` between pulls. From this checkout:

```bash
git fetch origin && git branch <n>-<slug> origin/main
cd "$(bash skills/using-git-worktrees/scripts/worktree-create.sh <n>-<slug>)"
bash .skills/doctor.sh                                   # initialises both submodules
ln -s "$(git worktree list | head -1 | awk '{print $1}')/.wheelhouse" .wheelhouse && uv sync
```

Destroying one needs `--force` once its submodules are initialised (git refuses otherwise); check `git status` is clean first.

## Workflow vs the vendored ship skill

Changes land as issue → branch → PR (AGENTS.md). Two vendored steps assume otherwise: `shipping-work-python-fastapi` Step 3 (*merge to `main` first*, then push) and `using-git-worktrees` Phase 4 (local merge back to `main`). Here, push the branch and `gh pr create` instead; the rest of both skills applies. Upstream fix proposed as [gregoryfoster/skills#342](https://github.com/gregoryfoster/skills/issues/342): a `pr` mode, on by default; drop this section when it lands. `worktree-destroy.sh` also verifies a merge by ancestry, which a squash or rebase merge fails, so destroy a squash-merged branch's worktree with `--descoped "merged as PR #<n>"` until then.

## Not installed

Separate decisions, as in broker#15: `curating-context`'s weekly cadence workflow and `PostToolUse` write guard, SocratiCode indexing and its two hooks (#5), processor's row on upstream's `.skills/cohort` roster ([gregoryfoster/skills#343](https://github.com/gregoryfoster/skills/issues/343)), and the tailored doc-check lists (`.skills/doc-sensitive-paths`, `.skills/doc-sections`).
