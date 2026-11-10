# Dependencies and CI

What Processor pins and how each pin moves: the CI workflow's actions and images, shellcheck, the lock audit, co-core, the extraction stack, certifi. The deploy's CI gate: [DEPLOYMENT.md](DEPLOYMENT.md#the-ci-gate).

## At a glance

| Thing | Value |
|---|---|
| CI | GitHub Actions [.github/workflows/ci.yml](../.github/workflows/ci.yml) on `ubuntu-24.04`, the node's image: lint (ruff, shellcheck), then the full suite against `redis:7.0.15` (the broker's, not 7.2: CLIENT SETINFO; upstream EOL, the next is broker#85's call), pulled from `mirror.gcr.io`, never anonymously from Docker Hub (#43). Keyless WIF to `co-pypi-reader`, whose token only `uv sync` gets (#121), needs the org variable `GCP_WIF_PROVIDER` shared with this repo and a `roles/iam.workloadIdentityUser` binding for `principalSet://…/attribute.repository/CannObserv/processor`. Actions SHA-pinned; Dependabot version updates `github-actions` only, `uv` security updates landed by hand ([§ below](#ci-pins-and-dependabot), #80). Shell lint, with an opt-in pre-commit hook: [§ below](#shell-lint) (#81) |
| Lock audit | [.github/workflows/audit.yml](../.github/workflows/audit.yml): `uv audit --frozen`, report-only, outside the deploy gate (#73). The weekly patch window reads the live lock (#75): [§ below](#the-lock-audit) |

## CI pins and Dependabot

Ported from broker#88 and broker#90 (#80).

**Pins.** Every `uses:` in `ci.yml` and `audit.yml` takes the form `owner/action@<40-hex sha> # vX.Y.Z`. A tag can be moved, and setup-uv publishes no floating major since v8. As of #80, with setup-uv moved by #110 (Dependabot's first PR here):

| Action | Pin |
|---|---|
| `actions/checkout` | v7.0.1 |
| `astral-sh/setup-uv` | v10.3.3 |
| `google-github-actions/auth` | v3.0.0 |

These are broker's majors (operator, 2026-10-29). `tests/test_dependabot.py` holds the form. `tests/test_ci.py` holds each major to a floor, so a Dependabot major passes and a downgrade fails. `tests/test_lock_audit.py` holds `audit.yml`'s pins equal to `ci.yml`'s.

The generated `context-cadence.yml` pins `actions/checkout` the same way, by hand; its installer keeps the pin on a re-render ([SKILLS.md § Context budget](SKILLS.md#context-budget)). `tests/test_dependabot.py` reads every workflow.

That a SHA *is* its tag needs the network, so it is checked by hand whenever a pin moves:

```bash
gh api repos/<owner>/<action>/git/ref/tags/<tag> --jq '.object.type + " " + .object.sha'
```

- `commit`: that SHA is the pin.
- `tag`: an annotated tag, whose SHA names the tag object rather than a commit. The pin is the commit it points at: `gh api repos/<owner>/<action>/git/tags/<sha> --jq .object.sha`. setup-uv's old `v7` was one.

**The cache.** `ci.yml` sets `prune-cache: true` on both setup-uv steps, because setup-uv v9 flipped the default to false. The repo is public, and a fork's PR can restore a cache saved on `main`. Unpruned, that cache holds co-core from the private co-pypi index. The prune drops downloaded wheels and keeps wheels built from source, so it covers co-core only while `uv.lock` locks each co-core to a wheel. `tests/test_ci.py` checks the prune, `tests/test_dependency_index.py` the wheels.

**The index token** (#121). Each WIF step also mints an access token (`token_format: access_token`). The `uv sync` step alone takes it, as `UV_INDEX_CO_PYPI_USERNAME` / `_PASSWORD`, from the action's masked output. `uv lock --locked` resolves from the lock alone (measured offline from an empty cache), so it needs none. `tests/test_ci.py` holds it to that one step.

**Dependabot** (`.github/dependabot.yml`) proposes action bumps only: `github-actions`, weekly, at most 10 open PRs, with `chore:` subjects.
- **A 3-day cooldown, GitHub's default.** `dependabot.yml` sets no `cooldown`, so Dependabot skips a release less than 3 days old (`cooldown days 3` in its log). setup-uv v10.3.3, published 2026-10-28, was held back from the first run on 2026-10-30 ("Found acceptable version outside cooldown: 10.3.2"). It arrived as #110 on 2026-11-02.
- **No `uv` or `pip` version updates.** Processor owns the exact co-core pin and holds the extraction stack ([§ Extraction dependencies](#extraction-dependencies)). `uv.lock` has its own signal ([§ The lock audit](#the-lock-audit)).
- **Settings → Code security** (operator reading, 2026-10-29, #80; security updates re-read 2026-11-03, #112):
  - Dependency graph: **on**. It is the `Graph Update: uv in /.` run on each push, and opens nothing.
  - Dependabot alerts: **on**. These are the operator's.
  - Dependabot security updates: **on**, and left on (operator, 2026-11-03, #112). The 2026-10-29 reading said off. A security update ignores `dependabot.yml`: it is a `uv` job (`"command":"security"`) per advisory. Until #121 every one failed before it opened anything (`PathDependenciesNotReachable: --find-links`; the first two on the push of `312552a`, 2026-11-03T01:27Z). Since #121, co-core resolves from the co-pypi index, which the updater should read over OIDC through the org registry `PYTHON_INDEX_OIDC_GCP` once this repository (ID `1396635239`) is admitted (cannobserv `docs/DEPENDABOT.md`, step 3). Proven 2026-11-07, after admission: a temporary `uv` version entry allowing co-core alone, run [42338249411](https://github.com/CannObserv/processor/actions/runs/42338249411), logged `registered gcp OIDC credentials for python index`, PyPI's 404, `authenticating request with OIDC token (host: us-west1-python.pkg.dev)`, `200` and `Latest version is 0.41.0`; its `uv lock` fetched all three co-core wheels the same way. It opened nothing: `co-core-aio==0.38.0 depends on co-core>=0.38,<0.39`, so a proposal must move the three names together (#121). The entry was then removed.
  - **A security PR lands by hand,** never as proposed. Add the `[tool.uv] constraint-dependencies` floor and its `tests/test_lock_floors.py` row in the same merge; in the extraction stack, bump `LOCAL_GENERATION` too ([§ The lock audit](#the-lock-audit)'s "Where a finding goes"). Then check the proposed `uv.lock`: every co-core `source` is still the index URL. Dependabot's lookup asks PyPI first, and the co-core names are unclaimed there (DEPENDABOT.md's accepted limit; CannObserv/replicator#169).

  No test can see these settings, and our token gets 403 on both APIs. Re-read them in the UI whenever the repo's security settings change.

**A Dependabot run reads its own credential context.** GitHub runs it as if from a fork: a read-only `GITHUB_TOKEN`, and Dependabot secrets only.
- `ci.yml` needs no secret. It needs `vars.GCP_WIF_PROVIDER` to resolve, plus the OIDC token that `id-token: write` asks for.
- Both held on CannObserv/watcher's Dependabot PRs (same provider, same SA; watcher#356, watcher#364, per broker's `docs/CI.md`).
- Confirmed here on the first Dependabot PR, #110 (setup-uv 10.3.2 → 10.3.3): run 41426874011, 2026-11-02, a `pull_request` run triggered by `dependabot[bot]`. **Authenticate to Google Cloud (WIF, read-only)** and **Sync cannobserv wheelhouse** (retired by #121) passed in both `lint` and `test`.
- On a red one, read **Authenticate to Google Cloud** first:
  - *"must specify exactly one of workload_identity_provider or credentials_json"*: the variable didn't reach the run.
  - *"did not inject $ACTIONS_ID_TOKEN_REQUEST_TOKEN"*: no OIDC token.
  - Past both, the cause is the provider's attribute condition or the SA binding.

  None of these is the bump's fault, so the fix belongs on the CI side. Don't take the bump to make it green.

**How a Dependabot PR lands:** fast-forwarded in this checkout, on `main`, like every other PR. Never squashed, and never by switching this checkout's branch.

```bash
# Read the action's release notes, not just the diff: a major can change inputs.
gh pr comment <N> --body "@dependabot rebase"   # only if main moved; wait for the force-push
gh pr checks <N> --watch                        # green on the commit that lands
# check the PR's new SHA against its tag, as under Pins above
git fetch origin "$(gh pr view <N> --json headRefName -q .headRefName)"
git merge --ff-only FETCH_HEAD
git push origin main                            # GitHub marks the PR merged
```

- The commit CI tested is the commit that lands, and its subject (`chore: bump …`) already follows AGENTS.md.
- Land one PR at a time. After each push, the next may need `@dependabot rebase`.
- A major passes `tests/test_ci.py`'s floor (`ACTIONS`) as it is. Raising the floor is a PR of its own: never push to Dependabot's branch, since Dependabot stops rebasing a PR someone else has pushed to.
- **No deploy:** an action bump touches `.github/` alone, which is outside `RUNTIME_*` ([MONITORING.md § The drift check](MONITORING.md#the-drift-check)).

**The runner image.** Every job in `ci.yml` and `audit.yml` runs on `ubuntu-24.04`, co-processor's image, not `ubuntu-latest`. That is 26.04 now: main's run 41003391205 (2026-10-30) ran on `ubuntu-26.04` (#81, as broker#88). The shell lint and the suite then run on the node's userland, and `test`'s Redis fallback, 24.04's apt `redis-server`, is 7.0.15. `tests/test_ci.py` holds every `runs-on` to one image, and on co-processor holds that image to `/etc/os-release`. So a node upgrade fails the suite there until the pins move with it.

## Shell lint

Ported from broker#112 (#81). `scripts/shellcheck.sh` lints this repo's own shell scripts: every tracked `*.sh` and `.githooks/*`.
- **Not a symlink:** the `.claude/hooks/` ones point into `skills-vendor/`.
- **Not `.skills/`:** the vendored refresh hook rewrites `doctor.sh` there.
- A finding in either is an issue on gregoryfoster/skills.

`ci.yml` runs the script over the tree as a step of `lint`. A step, not a job: `deploy.sh`'s `CI_JOBS` gate covers it as is, and a new job would need a `CI_JOBS` entry.

A commit runs it over what is staged, along with `ruff check` and `ruff format --check` over staged Python, once the clone opts in:

```bash
git config core.hooksPath .githooks
```

The hook is per clone, and `--no-verify` skips it, so CI is the enforcement.

**The version.** Both run shellcheck 0.11.0 from `shellcheck-py` in `uv.lock`'s `lint` group, a hash-pinned wheel:
- no root install on the node, and nothing fetched at run time;
- `audit_lock.sh` reads it;
- the release's `uv sync --no-dev` installs neither tool.

They call `uv run --frozen --only-group lint`, without the project, so a clone needs no co-pypi token to commit. `--no-config` stood in for `find-links` until #121, which measured the group from an empty cache with no token.

**A bump** changes what the gate finds, so it lands with its fixes: `uv lock --upgrade-package shellcheck-py`, then `bash scripts/shellcheck.sh`, then this paragraph's version. Never `uv lock --upgrade` (the co-core rule). It moves `pyproject.toml` or `uv.lock`, so it is a deploy.

**Disables.** Fix a finding rather than disable it. A `# shellcheck disable=` that stays sits on the line before the command, with its reason on the same line: `# shellcheck disable=SC2064 # expanded now: …`. A trailing directive after the command doesn't suppress.

`tests/test_shellcheck.py` pins the selector, the hook, the group and the version named here.

## The lock audit

Disclosures arrive without commits: #71's 12 advisories surfaced only because someone ran `uv audit` by hand. Two things read the lock against OSV now:

- **`audit.yml`, the signal.** `.github/workflows/audit.yml` runs `scripts/audit_lock.sh` on every push to `main`, every PR, weekly (`audit.yml`'s cron), and on dispatch (#73, ported from broker#86). A push or PR run is seen by whoever made the change. A scheduled run that finds something is still green, so it notifies nobody (#75).
- **The weekly window, the human read** (#75, operator 2026-10-26). Each run's after-probe (PATCHING.md § Patching, step 9) runs `uv audit --frozen` on `/srv/processor/live/uv.lock`, the **live** lock, as the lock file's owner, `exedev`, on a copy, never in the tree. It asks OSV live, so a disclosure `audit.yml`'s Monday 14:37Z run saw is read again in the window, which opens 14 h later. The count goes in the run's record, and the operator reads it. `tests/test_patching_knob.py` keeps the units' `WorkingDirectory=`, the knob's `owner` line and `scripts/deploy.sh`'s live link equal, so a refactor can't move the probe off the lock unnoticed.

The script and its workflow:

- **Report-only.** The script always exits 0. A finding (uv exit 1) is a `uv.lock advisories` warning, with the audit and where it goes in the job summary. Any other failure is a `uv audit did not run` warning, never a clean result. A red gate would turn `main` red on someone else's disclosure date.
- **Outside the deploy gate.** The gate reads `ci.yml`'s push run alone (DEPLOYMENT.md § The CI gate); `tests/test_lock_audit.py` holds both workflows to that.
- **No secret, no WIF.** `uv audit --frozen` reads the lock without fetching a package (measured with no co-pypi token, #121). OSV's text is echoed between `::stop-commands::` and a random token, so it can't issue workflow commands.
- **By hand:** `bash scripts/audit_lock.sh` from any directory audits this checkout's lock.
- **Where a finding goes:** every finding is `uv lock --upgrade-package <name>` plus a `[tool.uv] constraint-dependencies` floor with its check in `tests/test_lock_floors.py`, so a later re-lock can't undo it. A package in the extraction stack also bumps `LOCAL_GENERATION`, with the notice: § Extraction dependencies (#71 did both for pypdf). Re-locks touch `uv.lock`, so deploy after the merge.

What the window doesn't cover:
- A disclosure mid-week waits for the next window, at most 7 days. Between windows, `audit.yml` alone sees it.
- A skipped window means no read that week, and no reading says so. A run without step 9 leaves the class in its other probes' `not_read`.
- A unit moved off the lock would read clean: the probe finds trees only through `WorkingDirectory=`, and one with no lockfile still reads complete. That is the test's case.
- The run never re-locks. A finding there goes to "Where a finding goes", above, outside the window.

**Reopen path:** if a window is skipped twice running, or an advisory that matters reaches production between windows, take #75's option 2: the scheduled run opens or updates an issue, with `issues: write` on that job only.

## Bumping co-core

co-core is pinned `==`, and Processor owns the pin (spec D5, §5; amended 2026-10-10, #47). Until watcher#350 it was in lockstep with Watcher. A bump changes `processor_version` on every fact, and Watcher re-baselines on that:

1. Update the pin in `pyproject.toml` (all three names) and `EXPECTED` in `tests/test_pin.py`, then `bash scripts/co-pypi.sh uv lock --upgrade-package co-core --upgrade-package co-core-aio --upgrade-package co-core-sync`, never `--upgrade`. Nothing else moves: no wheelhouse, no digest table (#121).
   - **The lock carries the digests.** Each co-core file gets its sha256 from the index, and `uv sync --locked` refuses a miss. cannobserv's publish run fails unless the index lists every file under its local sha256 (from v0.41.0, CannObserv/cannobserv#554).
   - **Check the diff:** each co-core `source` is the index URL, and nothing outside the bump moved. A moved extraction-stack package belongs to the bump (§ Extraction dependencies).
2. A release that adds an essence or an extraction rule gets its cases in `tests/fixtures/parity/cases.json` first, so their goldens come from co-core. Then regenerate the goldens from co-core directly: `uv run python scripts/gen_parity_goldens.py tests/fixtures/parity`. It prints every digest that moved, and every case that started or stopped being refused. Paste that list into the bump note: each line is a deliberate re-baseline. Never regenerate to make a parity test pass outside a bump. A catch-up across releases reads each CHANGELOG's Downstream lines for the extract, bus and blob-store surfaces, and diffs those packages' code between the tags (#29 did 0.19.7 → 0.32.0).
3. The real corpus (`tests/fixtures/parity/real/`) is never regenerated. It passes unchanged, or the bump note says which recorded fingerprints moved.
4. **Notice to Watcher** before deploy: the new `processor_version`, and the moved digests. A change to the contract needs coordination with Watcher first, not just notice. The contract is the `content.process` / `content.derived` models and `canonical_text` semantics. A change to essence resolution is notice, not coordination (spec §5, #57).
5. Merge, then `scripts/deploy.sh`.

## Extraction dependencies

`processor_version` sees co-core and `LOCAL_GENERATION`, nothing else in the lock. The extraction stack (the transitive closure of co-core's `extract` extra: pypdf, lxml, bs4, html5lib, openpyxl, …) turns bytes into text too, and co-core floors it loosely, so a re-lock can move a fingerprint under an unchanged version. Watcher reads that as a content change and notifies. So moving the stack outside a co-core bump is a generation bump (spec §5, amended 2026-10-24, #71):

1. `uv lock --upgrade-package <pkg>`, never `uv lock --upgrade`; the diff moves what you named. A security floor goes in `[tool.uv] constraint-dependencies`, with its check in `tests/test_lock_floors.py`.
2. `tests/test_pin.py` fails, naming what moved. Update `EXTRACTION_STACK`, and bump `LOCAL_GENERATION` in `src/processor/processors/extract.py` and `GENERATION` in the test. At a co-core bump the version moves anyway: update the table, leave the generation.
3. Measure what moved: the parity tests (goldens, real corpus) must pass unchanged (a golden that moves is a co-core-pin question, not this one), and run co-core's extractor for the moved package over a wider corpus on both versions (#71 used pypdf's and py-pdf's sample PDFs: 4 of 106 moved). Put the result in the PR.
4. **Notice to Watcher** before deploy: the new `processor_version` and the evidence. Merge, then `scripts/deploy.sh`.

Since #71: `0.38.0+2`, pypdf 6.21.2, opentelemetry-api 1.45.1 (not stack; a floor only). Since #112: `0.38.0+3`, pypdf 6.21.4, pyasn1 0.6.5 (not stack; a floor only).

## Parity goldens

**Parity goldens** come from co-core's pure extract API directly (`scripts/gen_parity_goldens.py`), never from Processor's code. The script imports nothing from `processor`, and a test checks that. The v1 set was Watcher's, and the generator reproduced it byte for byte on 0.19.7 (2026-10-10, #47; `v1_provenance`). A test holds the committed goldens equal to the generator's output on the pinned co-core. A spec co-core refuses (`UnsupportedExtractionError`) is a golden too, `{"refused": …}`; any other raise is a generator error (#29). Regenerate only at a co-core bump, and list every moved digest in the bump note. A failing parity test means the output diverged; do not regenerate the goldens to make it pass. The real corpus, `tests/fixtures/parity/real/` (#16), is Watcher's export verbatim and the only copy of its blobs (gone from `gs://co-gcs-blobs` after the TTL): never edit or regenerate it; a bump that moves its output says so in the bump note.

## TLS trust

The service's TLS trust is certifi's bundle in the release venv, not the system store: GCS (`google-auth` / `google-cloud-storage` over `requests`) and the drift check's GitHub calls verify against `certifi.where()`, and nothing sets `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`. A CA distrust (exe.dev's system-store remediation doesn't reach the venv) is a certifi bump: raise `constraint-dependencies` in `pyproject.toml` and the floor in `tests/test_tls_trust.py`, `uv lock --upgrade-package certifi` (the diff moves certifi alone, never `uv lock --upgrade`), merge, then `scripts/deploy.sh`. The restart is the fix: a running process keeps the bundle it loaded. Check from the release venv: `/srv/processor/live/.venv/bin/python -I -c "import certifi; print(certifi.__version__)"`. certifi isn't co-core, so `processor_version` doesn't move and Watcher needs no notice. Since #66: ≥ 2026.10.19, without `emSign ECC Root CA - G3` (the eMudhra compromise, https://exe.dev/security/emudhra-compromise).
