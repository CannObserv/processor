# Deployment

Processor runs as one systemd unit, `processor`, on exe.dev VM `co-processor`, as its own user `processor`, from a release that [`scripts/deploy.sh`](#deploy-a-change-scriptsdeploysh) builds from a commit on `origin/main` (#2). What runs is `/srv/processor/live/REVISION`. An hourly timer, `processor-drift.timer`, reports through Status when that lags `origin/main` ([The drift check](MONITORING.md#the-drift-check), #35). `processor run` itself checks in to Status every 5 min while it is consuming ([Liveness](MONITORING.md#liveness), #39). Design: [the spec](specs/2026-09-29-processor-service-design.md) §2 (identities, grants), §4 (failure handling), §6 (cutover).

The rest of the runbook, by topic (#126): [PROVISIONING.md](PROVISIONING.md) (prerequisites, `/etc/processor/` and `processor_cli`, containment, the install), [DEPENDENCIES.md](DEPENDENCIES.md) (CI pins and Dependabot, shell lint, the lock audit, co-core and extraction bumps, TLS trust), [MONITORING.md](MONITORING.md) (the drift check, liveness, disk GC, memory), [NETWORK.md](NETWORK.md) (Tailscale DNS, tailnet flaps), [PATCHING.md](PATCHING.md) (the posture, the weekly run, the patch-due check, Node.js).

## At a glance

| Thing | Value |
|---|---|
| Service | systemd unit `processor` — [deploy/processor.service](../deploy/processor.service), runbook [docs/DEPLOYMENT.md](DEPLOYMENT.md); live since 2026-10-02. Runs as user `processor` (no sudo, not in `docker`, no home) from `/srv/processor/live` → `releases/<build>`, a read-only release `scripts/deploy.sh` builds from a commit on `origin/main` (#2; the cohort standard, broker#22 / status#9). **Never from a checkout** |

## Releases, not a checkout

**The unit runs a release, never a checkout** (#2). Processor follows the cohort's release standard, broker#22, as CannObserv/status#9 shipped it. That spec is `docs/specs/2026-09-30-deploy-releases-design.md` in CannObserv/status, decisions R1–R13.

```
/srv/processor/                      exedev's, 755
  releases/<build>/   git archive of one commit on origin/main, its own .venv (co-core
                      from the co-pypi index, each file held to the lock's sha256);
                      REVISION written last; a-w, go+rX
  live -> releases/<build>           processor.service, processor-drift.service
```

- **`<build>` is the commit's 12-character short SHA.** `REVISION` holds it. A directory without `REVISION` is an interrupted build, and the next deploy rebuilds it. The `starting` record reports the build, from `REVISION` (`dev` outside a release).
- **Nothing done in a checkout reaches the unit.** Branches, uncommitted edits, `uv sync` and the skills hook's commits all stay in the checkout. `~/processor` stays the operator's clone, and `scripts/deploy.sh` builds from it.
- **The venv:**
  - built with `uv sync --locked --no-dev --no-editable --compile-bytecode --python /usr/bin/python3.12`, inside the release, with `UV_LINK_MODE=copy`, through the checkout's `scripts/co-pypi.sh` (#121); uv's Linux default hardlinks from `~/.cache/uv`, which would make every release file the same inode as the cache's and each dev venv's. The release's `chmod` would then reach them, and an edit to any of them would reach production (CR 11);
  - never synced at start: the unit runs the venv's own `processor` entry point;
  - non-editable, so `sys.path`, and with it the child's allowlist, holds only the stdlib and `site-packages`;
  - on the system interpreter, because `processor` can't read anything under `/home` (`/home/exedev` is 0750, and the unit sets `ProtectHome=yes`).
- **Ownership:** the releases are `exedev`'s and read-only. `processor` reads them but isn't the owner, so it can never make them writable again. That is the cohort's goal 5 without root-owned releases (status#14).

**Where processor differs from Status:**
- there is no `dev` target; the smoke run on the scratch bus is the rehearsal;
- there's no database, so no migration step;
- it verifies through the journal and the smoke run, since it has no HTTP endpoint;
- the venv entry point stands in for `uv run --frozen --no-sync`.

Status's CI gate (status#11) is adopted as § The CI gate (#34), and its drift check (status#12) as MONITORING.md § The drift check (#35), which reports to Status instead of healthchecks.io.

## Deploy a change: `scripts/deploy.sh`

Run as `exedev`, from `~/processor`, after the PR merges. It needs `git`, `uv`, `jq`, `curl` and `flock` (util-linux) on the `PATH`, HTTPS to `api.github.com`, and for a build the co-pypi reader's key ([PROVISIONING.md § GCP provisioning](PROVISIONING.md#gcp-provisioning)) with HTTPS to `oauth2.googleapis.com` (the mint) and `us-west1-python.pkg.dev` (the index), plus `sudo` for `systemctl`, `systemd-run` and the unit file; `co-processor` has all of them. The script fetches `origin` itself:

```bash
cd ~/processor && git pull --ff-only     # for the skills hook; the deploy fetches on its own
scripts/deploy.sh                        # origin/main, once its CI passed
scripts/deploy.sh <build>                # any commit on origin/main with green CI: a rollback
scripts/deploy.sh --skip-ci [<build>]    # without asking CI: an emergency, logged
journalctl -t processor-deploy -n 20     # "CI passed for …", "live -> <build> (was …)", then "live is on <build>: pid <N> …"
```

**The journal holds the verdict** (`-t processor-deploy`, #93). A verified deploy ends with `live is on <build>: pid <N> logged starting (child_containment required), then consuming; the smoke run passed`: both proofs of step 4, `<N>` the new `MainPID`. A `live -> <build>` with no `live is on <build>` after it was never verified: a rollback (`live rolled back to …` / `… NOT answering`), an interrupt (below), or a SIGKILL, which leaves nothing. Before #93's deploy only the switch was journaled.

A merge that changes nothing under `src/`, `deploy/`, `scripts/deploy.sh`, `pyproject.toml`, `uv.lock` or `.skills/patching-hosts` (the patch-due check reads the release's knob, #113) needs no deploy. That list is `RUNTIME_DIRS` and `RUNTIME_FILES` in `src/processor/drift.py`, and the drift check alerts on it (MONITORING.md § The drift check).

**In order:**
0. **Ask CI** whether the commit passed (§ The CI gate). A refusal builds nothing.
1. **Build** `releases/<build>`, or reuse a complete one whose venv still imports `processor`, co-core and lxml. A release that `live` runs is never rebuilt in place. The service user must exist, or nothing switches.
   - **The index** (#121): the release's `uv sync --locked` runs through the checkout's `scripts/co-pypi.sh`, which mints the co-pypi reader's token for that one `uv`. `--locked` holds every file uv installs to the release's `uv.lock` sha256 and refuses a miss. That is the content check #87's wheelhouse check was, before #121 retired it with its digest table.
   - A key that mints nothing, or an index that refuses, fails the sync: `uv sync failed for <build>; nothing switched`, exit 1.
   - A lock from before #121 (co-core from `./.wheelhouse`) is refused and journaled (`refused <build>: … (before #121) …; nothing switched`), with the way back: § Rollback.
2. **Switch** `live` (an atomic rename). Then install every `deploy/*.service` and `deploy/*.timer` from the release that differs from its installed copy (#35, as status#18), with one `daemon-reload`. A changed timer is `try-restart`ed so it re-arms. Each replaced copy is kept aside for step 5.
3. **Restart.** `reset-failed` comes first, in case a crash loop hit the start limit. The restart lets the in-flight command finish (`KillMode=mixed`: SIGTERM reaches the consumer, not its extraction child), for up to `TimeoutStopSec=240`.
4. **Verify,** within `PROCESSOR_DEPLOY_VERIFY_SECONDS` (300):
   - the new `MainPID` logs `starting` with this build and `child_containment: required`, then `consuming`;
   - then the **smoke run** passes on this build under `required`. It runs `scripts/smoke_scratch_bus.py` through `systemd-run` with the unit's user, env file, credentials (and their `SetCredential=` fallbacks, #39 CR 1) and sandboxing:
     - **Bus:** the scratch Redis (db 14), never the broker.
     - **Command:** one real command through the real consumer loop and the contained child.
     - **Input:** from the committed real corpus.
     - **Output:** the production bucket, write-if-absent, so a repeat run writes nothing new.
     - **Pass:** it prints `"result": "pass"` when the fact matches Watcher's recorded fingerprint, the entry is acked, and the object reads back intact.
5. **On failure:** switch back (the units too: replaced copies go back, added ones are removed), restart, and prove the old build the same way.
   - Exit 1 when the old build answers.
   - Exit 4 when nothing answers: the old build failed too, or there was nothing to switch back to. On a first deploy, the unit it replaced goes back and the service restarts on it, and § Rollback applies.

**On success,** a timer this deploy installed for the first time is enabled (`systemctl enable --now`), so a deploy that was switched back never ran it. A timer installed before is never enabled again, so an operator's `disable` sticks. A timer that won't enable or re-arm is a note, never a switch back: it watches the deploy, it never decides one. Still by hand: `sudo systemctl reenable <unit>` after its `[Install]` section changes, and retiring a unit (`disable --now`, remove the file, `daemon-reload`), since a unit gone from `deploy/` stays installed.

Then the host configs under `deploy/` (`HOST_CONFIGS`: tailscaled's drop-in, the apt files and the apt timers' drop-ins, needrestart's, the disk GC's) are compared with their installed copies. A difference is a note, never an install, and is journaled, as is an installed unit the release lacks. The deploy keeps the 5 most recently deployed releases plus `live`.

**Interrupted** (INT, TERM or HUP: Ctrl-C, or an agent's tool call timing out), the deploy says where it stopped once the step in progress ends:
- before the switch: `interrupted before the switch; nothing switched`, exit 1, nothing journaled;
- after it, the switch back included: `interrupted on <build> <stage>: live -> <link>, unverified`, journaled, exit 4. Deploy again, or deploy the build it should run;
- after the verify: `interrupted after the verify; live is on <build>, verified`, journaled, exit 0. While it enabled a timer it installed new, the line ends `sudo systemctl enable --now <timer>`: run it, since no later deploy counts that timer as new.

A signal to the script alone, not its process group, waits for the step in hand: up to `TimeoutStopSec=240` in a restart.

| Variable | Default | What |
|---|---|---|
| `PROCESSOR_DEPLOY_ROOT` | `/srv/processor` | releases and the `live` link |
| `PROCESSOR_DEPLOY_ETC` | `/etc` | the units go in `systemd/system/`; host configs are compared there |
| `PROCESSOR_DEPLOY_KEEP` | `5` | releases kept besides `live` |
| `PROCESSOR_DEPLOY_VERIFY_SECONDS` | `300` | how long the new process has to start, past `TimeoutStopSec=240` |
| `PROCESSOR_DEPLOY_PYTHON` | `/usr/bin/python3.12` | the interpreter each venv is built on |
| `PROCESSOR_DEPLOY_CI_WAIT_SECONDS` | `600` | how long to wait for a pending CI run |
| `PROCESSOR_DEPLOY_CI_POLL_SECONDS` | `30` | how often to ask while waiting |

A stop mid-reclaim finishes the command in hand and leaves the rest pending. A command killed mid-flight stays pending, and the reclaim re-runs it after `reclaim_min_idle_ms`. That includes a stop during a GCS outage: the library's retries can push one command to about 435 s, past `TimeoutStopSec`, so systemd SIGKILLs it. That is safe (nothing was acked) and deliberate (a deploy never hangs for minutes).

### The CI gate

**A deploy needs the commit's CI to have passed** (#34, ported from status#11). Before anything is built, `deploy.sh` asks GitHub's Actions API about the commit. It refuses unless `lint`, `test` and any other job in the run all succeeded. The refusal names each job that didn't, with its conclusion, and links the run. A refused deploy changes nothing.

- **Which run.** The newest `push` run of `ci.yml` on `main` for exactly that commit. Every FF merge also has a `pull_request` run on the same SHA, and that one doesn't count, nor does a `workflow_dispatch` run. A re-run counts, since GitHub reports a run's latest attempt.
- **The run and every job in it must be `success`.** A `skipped` job leaves the run's own conclusion `success`, so the gate reads the jobs too.
  - `CI_JOBS` in `deploy.sh` (`lint test`) is a floor, and `tests/test_deploy.py` holds `ci.yml` to it. Any other job counts without being named there.
  - A job renamed in `ci.yml` must be renamed in `CI_JOBS`. A build from before the rename then lacks the new name, so rolling back to it needs `--skip-ci`.
- **Cancelled is not a verdict,** and has nothing to fix. The refusal says `run concluded cancelled`: re-run it from its page (a re-run counts), or deploy a newer commit. It happens two ways:
  - **Superseded.** `ci.yml`'s concurrency group never cancels a run in progress on `main`, but GitHub keeps only one *pending* run per group. Push to `main` twice while a run is in progress, and the first of the two pending runs is cancelled. A dispatch on `main` does the same.
  - **Never scheduled.** A run that times out before any runner picks it up ends `cancelled` with no jobs. That happened on #23's PR, from GitHub capacity.
- **Pending: it waits.** Up to 10 minutes, asking every 30 s, holding the deploy lock. CI takes about 3 minutes. A run still going after that is refused with its link: deploy again when it finishes.
- **No run: deploy the tip of the push.** An FF merge pushes every commit of the PR, and GitHub runs CI only on the newest commit of each push. #33 put 21 commits on `main`, and only `fe19a2b` has a run.
  - `scripts/deploy.sh <an intermediate commit>` is refused at once: deploy the tip of its push, or pass `--skip-ci`.
  - The tip may have been pushed seconds ago, so it waits, as for a pending run. A tip that never gets a run (`[skip ci]`) is refused when the wait ends; pass `--skip-ci` rather than wait.
- **No token.** The repo is public, so the API answers without one, at 60 requests an hour per address. A deploy costs 2 requests, or up to 22 if it waits the full 10 minutes. Whether `co-processor`'s egress address is shared with other VMs is unknown.
  - If GitHub refuses or can't be reached (a 403 rate limit, a 5xx, a timeout, an answer that is empty or not a JSON object), the deploy says so, with GitHub's message when there is one, and builds nothing. Wait, or pass `--skip-ci`. It never passes.
  - If the repo goes private, the API answers 404, and the gate needs a read-only token.
- **Rollbacks are gated too.** One rule: a kept release proves it was built, not that its CI passed. Every deploy so far was the tip of its push, so its run exists, and a rollback to it normally passes. When GitHub is slow or rate-limited mid-incident, `scripts/deploy.sh --skip-ci <old build>`.
- **The lock audit is not in the gate.** `audit.yml` is its own workflow, so its run never counts, and a finding never blocks a deploy (DEPENDENCIES.md § The lock audit).
- **`--skip-ci`** deploys without asking GitHub at all: for a fix that can't wait for CI, or when GitHub can't answer. It is logged before anything is built, as `CI not checked for <build> (--skip-ci)`. A gate that passes is logged too, as `CI passed for <build>: <run>`.

## Rollback

- **A failed deploy** switches back by itself (exit 1). `scripts/deploy.sh <old build>` rolls back on purpose; the old release still exists among the 5 kept, so nothing is rebuilt. It is gated on the old build's CI like any deploy; if GitHub can't answer, add `--skip-ci` (§ The CI gate).
- **Past #121**, to a build no longer kept: this `deploy.sh` refuses its lock, which resolves co-core from `./.wheelhouse`. A kept release is reused and needs nothing. For one that isn't kept, run that commit's own `scripts/deploy.sh` from a worktree at it: `git worktree add ../processor-<build> <build>`, then `ln -s ~/processor/.wheelhouse ../processor-<build>/.wheelhouse`, then `../processor-<build>/scripts/deploy.sh <build>`. Its `deploy.sh` checks the wheelhouse against its own table. `~/processor/.wheelhouse/` stays on the host for this; `scripts/build_wheelhouse.sh` is in that commit. Documented, not exercised.
- **Past #87**, to a build no longer kept: the rebuild is refused, since the old `pyproject.toml` pins no wheelhouse digest. Falling back to unchecked would undo the check. Roll back to a kept release instead.
- **Past #35** (to `428af0e2a016` or older): the old release has no `processor-drift.*` units and no `processor drift`. A deploy never removes a unit, and it names each one the release lacks, so the timer keeps firing. Each run then exits 2 with a usage line and checks in nothing, and `co-processor-drift` goes `missing`. So when rolling back past #35, run `sudo systemctl disable --now processor-drift.timer`, and ask Status's operator to pause the monitor (`"enabled": false`). Rolling forward again doesn't re-enable a timer that is still installed: `sudo systemctl enable --now processor-drift.timer`.
- **Past #39** (to a build before the liveness check-in): the old unit loads no Status key and the old build never checks in, so `co-processor-live` goes `missing` 20 min later. Ask Status's operator to pause it before rolling back.
- **The install** (Stage B), back to `exedev` running `~/processor`:

  ```bash
  sudo cp -a /var/backups/processor/2/processor.service /etc/systemd/system/processor.service
  sudo rm -rf /etc/processor && sudo cp -a /var/backups/processor/2/processor /etc/processor
  sudo systemctl daemon-reload && sudo systemctl reset-failed processor && sudo systemctl restart processor
  ```

  The backup has the old ownership (`exedev`) and the `GOOGLE_APPLICATION_CREDENTIALS` line. The user and `/srv/processor` can stay.

## Operate

- **Drift:** `journalctl -u processor-drift` (MONITORING.md § The drift check).
- **Liveness:** `journalctl -u processor -o cat | jq -cR 'fromjson? | select(.logger == "processor.liveness")'` (MONITORING.md § Liveness).
- **Logs:** `journalctl -u processor`. A record's journald `PRIORITY` follows its `level` (CRITICAL 2, ERROR 3, WARNING 4, INFO 6, DEBUG 7; #76), so `journalctl -u processor -p warning` lists WARNING and above, with systemd's own warning lines for the unit; add `-o cat | jq -cR 'fromjson?'` for the JSON alone. Every `processor` entry point (so every unit that runs it: `processor`, `processor-drift`, `processor-patching`) writes an sd-daemon `<N>` prefix only when its stderr is the journal (`JOURNAL_STREAM` names stderr's own device and inode), and journald strips it, so `MESSAGE` stays pure JSON. A terminal, `processor_cli` and any `systemd-run --pipe` never see one. Records from before #76's deploy (`live -> 431b7b569300` at 2026-10-25T15:50:36Z in `journalctl -t processor-deploy`) are all `PRIORITY=6`: filter those by `.level`. Only `logging` records carry a priority: a bare traceback or a `print` to stderr is still `PRIORITY=6`. A record past journald's `LineMax` (48K) is split, and only its first part carries the priority (neither part is JSON). `starting` names the `build`, `child_containment`, `landlock_abi` and `liveness`. Deploys: `journalctl -t processor-deploy`. Each outcome has `command_id`, `info_source_id`, `action` (`ack` / `strike` / `leave_pending` / `dead_letter`), `reason`, `detail`, and timings in `*_ms`. Each also has `input_digest` once it is valid, as bare hex like the command's. A complete fact adds `output_digest` (`sha256:<hex>`, as on the fact; the object is `blobs/<hex>.bin`; absent when `empty`), `output_size_bytes`, `empty` and `processor_version`, and so does a `leave_pending` whose publish failed after the store. A shadow command's output: `journalctl -u processor -o cat | jq -cR 'fromjson? | select(.command_id == "<id>") | {action, reason, input_digest, output_digest}'` (`-R … fromjson?` skips systemd's own lines, which `-u` includes and plain `jq` aborts on).
- **Dead letters:** `processor_cli dlq list | show <id> | drop <id>` (PROVISIONING.md § `/etc/processor/`). There is no replay. An entry failed to decode, was not a command, or is a command that raised outside the handler on every attempt (`reason` starts `gave up on attempt`): that one is a bug to fix. Before dead-lettering such a command, Processor published a terminal `dead_lettered` whose `detail` starts `dead-lettered:` (best effort), so Watcher has closed it (#17; `extraction_error` before co-core 0.37.0, #21). The journal's `dead-lettering` record says which: `failure_fact` is `published`, `skipped` (a fact had already gone out) or `refused`. On a command given up at the cap, `handle_skipped` is `true` when the record retries a give-up that did not finish (the failure fact refused transiently, or the dead-letter refused), with no re-run of the command and the original traceback (#28). The same entry logging that every ~11 min while it stays pending means `content.process.dlq` refuses it: `journalctl -u processor -o cat | jq -cR 'fromjson? | select(.handle_skipped) | {timestamp, command_id, message_id, reason}'`.
- **Lag / missing group:** the broker probe watches `processor.process` (broker#75).
- **Strikes** are counted in memory. A restart resets them, so a poison command gets at most 3 more attempts.

## Moved sections

These sections left this runbook (#126); each heading keeps its old anchor.

### Prerequisites (operator)

[PROVISIONING.md](PROVISIONING.md#prerequisites-operator)

### `/etc/processor/`

[PROVISIONING.md](PROVISIONING.md#etcprocessor)

### CI pins and Dependabot

[DEPENDENCIES.md](DEPENDENCIES.md#ci-pins-and-dependabot)

### Shell lint

[DEPENDENCIES.md](DEPENDENCIES.md#shell-lint)

### The lock audit

[DEPENDENCIES.md](DEPENDENCIES.md#the-lock-audit)

### The drift check

[MONITORING.md](MONITORING.md#the-drift-check)

### Liveness

[MONITORING.md](MONITORING.md#liveness)

### Containment

[PROVISIONING.md](PROVISIONING.md#containment)

### Install (once; #2's Stage B)

[PROVISIONING.md](PROVISIONING.md#install-once-2s-stage-b)

### Before #2: the first install (2026-10-02)

[PROVISIONING.md](PROVISIONING.md#before-2-the-first-install-2026-10-02)

### Create the group: as soon as the credential verifies

[PROVISIONING.md](PROVISIONING.md#create-the-group-as-soon-as-the-credential-verifies)

### Bumping co-core

[DEPENDENCIES.md](DEPENDENCIES.md#bumping-co-core)

### Extraction dependencies

[DEPENDENCIES.md](DEPENDENCIES.md#extraction-dependencies)

### TLS trust

[DEPENDENCIES.md](DEPENDENCIES.md#tls-trust)

### Tailscale DNS

[NETWORK.md](NETWORK.md#tailscale-dns)

### Tailnet flaps

[NETWORK.md](NETWORK.md#tailnet-flaps)

### Disk GC

[MONITORING.md](MONITORING.md#disk-gc)

### Memory: no earlyoom

[MONITORING.md](MONITORING.md#memory-no-earlyoom)

### Patching

[PATCHING.md](PATCHING.md#patching)

### Node.js (agent tooling only)

[PATCHING.md](PATCHING.md#nodejs-agent-tooling-only)
