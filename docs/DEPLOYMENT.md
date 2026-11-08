# Deployment

Processor runs as one systemd unit, `processor`, on exe.dev VM `co-processor`, as its own user `processor`, from a release that [`scripts/deploy.sh`](#deploy-a-change-scriptsdeploysh) builds from a commit on `origin/main` (#2). What runs is `/srv/processor/live/REVISION`. An hourly timer, `processor-drift.timer`, reports through Status when that lags `origin/main` ([The drift check](MONITORING.md#the-drift-check), #35). `processor run` itself checks in to Status every 5 min while it is consuming ([Liveness](MONITORING.md#liveness), #39). Design: [the spec](specs/2026-09-29-processor-service-design.md) §2 (identities, grants), §4 (failure handling), §6 (cutover).

The rest of the runbook, by topic (#126): [PROVISIONING.md](PROVISIONING.md) (prerequisites, `/etc/processor/` and `processor_cli`, containment, the install), [DEPENDENCIES.md](DEPENDENCIES.md) (CI pins and Dependabot, shell lint, the lock audit, co-core and extraction bumps, TLS trust), [MONITORING.md](MONITORING.md) (the drift check, liveness, disk GC, memory), [NETWORK.md](NETWORK.md) (Tailscale DNS, tailnet flaps).

## At a glance

| Thing | Value |
|---|---|
| Service | systemd unit `processor` — [deploy/processor.service](../deploy/processor.service), runbook [docs/DEPLOYMENT.md](DEPLOYMENT.md); live since 2026-10-02. Runs as user `processor` (no sudo, not in `docker`, no home) from `/srv/processor/live` → `releases/<build>`, a read-only release `scripts/deploy.sh` builds from a commit on `origin/main` (#2; the cohort standard, broker#22 / status#9). **Never from a checkout** |
| Patching | `automatic` (knob `.skills/patching-hosts`, #113): `-security` daily by unattended-upgrades (13:00 UTC, no restart, no reboot); the rest in one approved run a week, `Tue 05:00–10:00` UTC (Monday night Pacific), as the broker's window closes; `processor-patching.timer` asks for it through Status. Never a bare `apt-get upgrade`. [§ below](#patching) (#77) |
| Node.js | 24 LTS from NodeSource apt, agent tooling only (mayfly, SocratiCode): `deploy/nodesource.sh install\|check`, [§ below](#nodejs-agent-tooling-only) |

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

## Patching

Posture **`automatic`** since #113 (operator, 2026-11-03; `scheduled` from #77), under gregoryfoster/skills `patching-hosts` ([skills/patching-hosts](../skills/patching-hosts/SKILL.md)), read from the knob [.skills/patching-hosts](../.skills/patching-hosts). Two lanes (the skill's policy.md):

- **Security, daily and unattended.** `apt-daily-upgrade.timer` runs `unattended-upgrade` on `-security` (and the release pocket and ESM security, its stock origins) at **13:00 + up to 60 min UTC**, with needrestart listing restarts and performing none, and `Automatic-Reboot "false"`. Nothing restarts `processor` and nothing reboots: a maintainer script's own restart still happens (the scratch `redis-server`'s). A `-security` libssl or libc leaves the service on the old library until the next deploy, attended run or reboot: the attended run's step 6 picks it up.
- **Maintenance, weekly and attended** (`cadence weekly`). The owner approves a run **every week**, in **`Tue 05:00–10:00` UTC**: Monday night Pacific, 21:00–02:00 PST or 22:00–03:00 PDT (operator, 2026-10-26). It takes the security lane's set plus `-updates` plus the followed origins, `--lane maintenance`, and any reboot or restart due. The window opens as the broker's closes (`Mon 23:00–05:00`, CannObserv/broker#67). Both VMs hold the bus link, so the hosts never patch at once and a flap stays attributable to one host. Each run is approved in this repo's session, like the first.

**The timers are moved, not only unmasked.** `apply.sh` refuses any step whose span meets `apt-daily-upgrade.timer`'s range, its calendar time plus its random delay, and refuses while either apt service runs. The stock `06:00` + 60 min sits inside the window, so the drop-ins below move it to 13:00, and `apt-daily.timer` (stock: any hour) to 12:00 + 30 min, before it. `tests/test_apt_automatic.py` holds both outside every knob window: a window change that meets them fails CI. **Never a bare `apt-get upgrade`:** it takes every origin. The skill's [run.md](../skills/patching-hosts/references/run.md) is the procedure; this section is what is particular to this host.

| Knob line | Why |
|---|---|
| `posture automatic`, `cadence weekly` | The security lane is the apt timers'; the maintenance lane is the weekly attended run in the `window` |
| `service processor.service` | The reboot chain stops it. `--post-boot` checks its first start and `NRestarts` |
| `inflight pgrep … processor\._child` | An extraction in progress. The chain checks it again before it stops anything |
| `health` ×2 | The unit is active, and nothing at `-p warning` in 6 min ([§ Operate → Logs](#operate); valid since #76) |
| `origin deb.nodesource.com follow` | Node, agent tooling ([§ Patching Node](#patching-node)) |
| `origin pkgs.tailscale.com follow`, `hold tailscale tailscale` | Its postinst restarts tailscaled, the only path to the broker, so it is a held step approved on its own |
| `owner /srv/processor/live self` | The release has no git origin; its `uv.lock` is this repo's ([§ The lock audit](#the-lock-audit)) |
| no `datastore` | The scratch `redis-server` holds test data only. It stays the skill's default `redis` held step, because its postinst restarts it |
| no `quiet` | `processor-drift.timer` ticks drift (`OnUnitActiveSec=1h`), and liveness runs inside the service |

**Host files** (compared after each deploy, installed by hand):

| Tracked | Installed | Why |
|---|---|---|
| [deploy/needrestart.conf.d/processor.conf](../deploy/needrestart.conf.d/processor.conf) | `/etc/needrestart/conf.d/` | `$nrconf{restart} = 'l'`: apt's hook lists restarts and performs none. With it unset, Ubuntu mode restarts `processor` by itself after a libssl update (its `python3.12` maps the library). `tailscaled` is static Go and maps none; only its own postinst restarts it, in the held `tailscale` step. `NEEDRESTART_MODE=l` is now a backup, not the guard |
| [deploy/apt/tailscale.pref](../deploy/apt/tailscale.pref) | `/etc/apt/preferences.d/` | Scopes the followed origin to `tailscale` and its keyring (policy.md) |
| [deploy/apt/apt.conf.d/processor-periodic](../deploy/apt/apt.conf.d/processor-periodic) | `/etc/apt/apt.conf.d/` | `APT::Periodic::Enable "1"` and `Automatic-Reboot "false"` (#113). The name sorts after the image's `docker-disable-periodic-update`, which sets 0: apt reads the directory in name order, so a `99…` name would lose |
| [deploy/apt-daily-upgrade.timer.d/processor-window.conf](../deploy/apt-daily-upgrade.timer.d/processor-window.conf) | `/etc/systemd/system/apt-daily-upgrade.timer.d/` | 13:00 + 60 min UTC, clear of the window (above) |
| [deploy/apt-daily.timer.d/processor-window.conf](../deploy/apt-daily.timer.d/processor-window.conf) | `/etc/systemd/system/apt-daily.timer.d/` | 12:00 + 30 min UTC: the lists, before the upgrade |

Install once, **before any apt run**, from the deploy checkout's root, after `git pull --ff-only` (until then it lags `origin/main` and has neither file):

```bash
sudo install -m 644 deploy/needrestart.conf.d/processor.conf /etc/needrestart/conf.d/processor.conf
sudo needrestart -m u -b -r l    # "Disabling Ubuntu mode, explicit restart mode configured"; never without -r l
sudo install -m 644 deploy/apt/tailscale.pref /etc/apt/preferences.d/tailscale.pref
sudo tailscale set --auto-update=false   # the tailnet's switch is off (broker#67); this is the node's own
# posture automatic (#113): the files first, then the timers
sudo install -m 644 deploy/apt/apt.conf.d/processor-periodic /etc/apt/apt.conf.d/processor-periodic
sudo install -D -m 644 deploy/apt-daily.timer.d/processor-window.conf /etc/systemd/system/apt-daily.timer.d/processor-window.conf
sudo install -D -m 644 deploy/apt-daily-upgrade.timer.d/processor-window.conf /etc/systemd/system/apt-daily-upgrade.timer.d/processor-window.conf
sudo systemctl unmask apt-daily.timer apt-daily-upgrade.timer && sudo systemctl daemon-reload
sudo systemctl enable --now apt-daily.timer apt-daily-upgrade.timer   # unattended-upgrades.service (the shutdown hook) stays masked
uv run pytest tests/test_needrestart.py tests/test_tailscaled.py tests/test_apt_automatic.py   # on the node, the installed and live tests run and pass
```

The first unattended run reads in `journalctl -u apt-daily-upgrade` and `/var/log/unattended-upgrades/unattended-upgrades.log` (root). No apt timer has a stamp under `/var/lib/systemd/timers/` (measured 2026-11-05), so `Persistent=true` has no missed run to catch up: enabling fires nothing, and the first run is the next 13:00. **To go back to `scheduled`:** `sudo systemctl disable --now` and `mask` both timers, remove `processor-periodic`, and set the knob's `posture`.

**The probe escalates by itself.** `probe.sh` runs its root readings through `sudo -n` whenever that works, without asking. So every run, including `--dry-run-into` and `--post-boot`, is a sudo step and needs the owner's approval. For a non-root read, put a `sudo` that exits 1 first on `PATH`; the root readings then land in its `not_read`.

**This host's run** (each sudo step approved in this repo's session; `<run>` = `/var/backups/patching-hosts-<UTC>`). Run the scripts under `skills/patching-hosts/scripts/` from the deploy checkout's root, pulled: each defaults its knob to `.skills/patching-hosts` at `git rev-parse --show-toplevel`, or else the working directory, and with no knob found it falls back to `automatic`, report-only.

1. `sudo apt-get update`. The maintenance dry run reads the host's lists, which nothing else refreshes (gregoryfoster/skills#467).
2. `probe.sh --lane maintenance --dry-run-into <dir>`, in the background: 0 findings expected, beside a `lists:stale` risk that reads the security archive's own publish date, not when the lists were refreshed (2026-10-26). Read the `redis-server` candidate; it must still be a `7.0.15` build, not Valkey, until #54.
3. The needrestart proof, as above.
4. `apply.sh --approve --step bulk --lane maintenance --dry-run <dir>`, in the background, within 24 h of the dry run. It restarts nothing. With no data store there is no recovery point: `recovery-point.sh` refuses ("nothing to dump"), and the bulk records the before-versions, the rollback path, in the `<run>` it creates and prints.
5. Held steps, each approved, each `apply.sh --approve --step <name> --run <run>`:
   - `redis`: the scratch server restarts;
   - `docker`: `containerd`. Docker and containerd are disabled and inactive here, so nothing is running to restart;
   - `tailscale`, if pending: a 2.3 s tailnet blip, sockets survive ([§ Tailnet flaps](#tailnet-flaps), "A tailscaled restart here").
6. The reboot and restart decision, from the host, never from this run's set: under `automatic` a `-security` library may have landed any day since the last run. `sudo needrestart -b -r l` and `sudo grep -c '(deleted)' /proc/1/maps`. The kernel never decides one on exe.dev. Expect a reboot when `systemd` or `libsystemd0` is in the set: after those families, needrestart listed dbus and logind on co-broker and co-status, and PID 1 mapped deleted files.
7. The reboot: `reboot-chain.sh --run <run>` prints the chain; `--approve` launches it. It stops `processor` (up to `TimeoutStopSec=240`), then reboots in-guest. **Never `exe.dev restart`**: that is a hard reset (#83).
8. After it: `probe.sh --post-boot --run <run>`, § Tailscale DNS → *Check after a reboot*, `NRestarts=0`, and drift and liveness `ok` in Status. The disk sensor's first tick after boot logs `checked in ok (ok): HTTP 202` within 10 min ([§ Disk GC](#disk-gc)). Read `journalctl -u processor -p warning --since <window start>` raw (systemd's own lines, such as a failed stop) and through `-o cat | jq -cR 'fromjson? | …'`. A crash is `PRIORITY=6`, so check `NRestarts` too.
9. The after-probe, last, after the reboot or the no-reboot restart below: `probe.sh --refresh-into <dir>`. It is what's left pending, by class, for the record, and the only step that audits the live `uv.lock` (`pending.language`). The dry run and `--post-boot` never ask OSV ([§ The lock audit](#the-lock-audit)).

**With no reboot,** restart the service anyway when needrestart lists it, or after a libssl update by either lane: `/usr/bin/python3.12`'s `_ssl` links the system `libssl.so.3`, so the old library stays mapped until a restart. `sudo grep -c 'libssl.so.3 (deleted)' /proc/$(systemctl show -p MainPID --value processor)/maps` must read 0 afterwards.

**Liveness's grace** is 900 s. The heartbeat ticks as soon as the service starts, so the silence is at most the time since the last tick (300 s), plus the stop (240 s), plus the boot. That stays inside the grace, so Status needs a warning only if a step stalls.

**Deploys don't wait for a window** (operator, 2026-10-26, #77). The skill's policy.md says a deploy that restarts a declared `service` takes one (gregoryfoster/skills#366). Here `scripts/deploy.sh` runs after each merge, as before: it restarts `processor` alone, verifies the new build and switches back on failure ([§ Deploy a change](#deploy-a-change-scriptsdeploysh)). A merge that lands during a window is deployed after the post-boot checks, so a flap stays attributable.

**The record** goes on the issue (`records CannObserv/processor`), per run.md §7, and ends with the next window's date. **Its first line is `## Patch run record: co-processor`**, then anything (a date, the lane), posted by an `OWNER` or a `MEMBER` whose membership is public (§ The patch-due check): that line is what [§ The patch-due check](#the-patch-due-check) reads as this week's run, a nothing-pending run's included. A run that can't take its window still posts one, saying why, or the monitor stays `missed` until the next. What each run leaves pending, by class, comes from step 9 and includes:
- **Ubuntu Pro / ESM:** Pro isn't attached, and 25 esm-apps updates were visible on 2026-10-26.
- **Language:** `/srv/processor/live/uv.lock`, its OSV advisory count, owner `self`. Run.md §7 asks for the owning repo's update mechanism; here that is `audit.yml`, report-only ([§ The lock audit](#the-lock-audit)). Dependabot's version updates cover Actions only, never `uv.lock`, and its security updates, resolving through the co-pypi index since #121, are landed by hand ([§ CI pins and Dependabot](#ci-pins-and-dependabot)). A count above 0 goes to that section's "Where a finding goes" and is linked from the record. A tree listed as not audited, or the class in `not_read`, is recorded as no read, never as 0.

### The patch-due check

`processor patching`, hourly on the hour from `processor-patching.timer` (#113), asks for the attended run, since nothing else does (the 2026-11-03 window passed unnoticed). It checks in to Status's `co-processor-patching` (tenant `co-processor`, the drift check's key and `LoadCredential=`):

| Kind | Status | When |
|---|---|---|
| `ok` | ok | a record has been posted since the window last opened |
| `due` | alert | the window is open and no record yet: **the request for the run**, within a minute of 05:00Z |
| `missed` | alert | the window closed with no record, until it next opens. A new fault, so it reports at once |
| `knob` | alert | the window can't be read from the release's knob |
| `test` | alert | `processor patching --test-alert`, by hand |
| — | none | GitHub didn't answer: silence, which the monitor's grace turns into `missing`, never a false `ok` |

- **The window** is the release's own `.skills/patching-hosts`, its weekday `window` lines. `read-knob.sh` isn't in a release (a `git archive` leaves the submodule empty), so `src/processor/patching.py` parses them, and `tests/test_patching.py` holds the parse equal to the skill's reader. A dated line is a one-off and opens no due period. A `[host` section is unread (`knob`). The knob is a `RUNTIME_FILES` path: a change to it is a deploy, or the drift check alerts.
- **The record** is an issue comment anywhere in this repo whose first line is `## Patch run record: co-processor`, from an `OWNER` or `MEMBER` (the repo is public: anyone else's comment never counts), created at or after the opening. **The read is anonymous, so a member counts only while their CannObserv membership is public:** a private member reads as `CONTRIBUTOR` (measured 2026-11-06 for user id 14647, before the operator made theirs public). A recorder whose membership goes private turns every week `missed`: a false alert, never a false `ok`. Check with `curl -s -o /dev/null -w '%{http_code}' https://api.github.com/orgs/CannObserv/public_members/<login>` (204: public). One unauthenticated request an hour, newest first; past 300 comments since the opening it can't say, and is silent.
- **What it doesn't report:** a restart or reboot due. The service user can't read `processor`'s maps (it's undumpable) or run needrestart, so the weekly run's step 6 picks that up.

Read a run with `journalctl -u processor-patching -n 3 -o cat`: one `patching check` record, its `kind`, `window`, `record` and `checkin` (Status's 202). A deliberate alert (`kind: test`, fault `test`, asking GitHub nothing) proves the channels, as the drift check's does ([§ The drift check](#the-drift-check)):

```bash
sudo systemd-run --quiet --pipe --wait --collect -p User=processor -p Group=processor \
  -p LoadCredential=status-checkin-key:/etc/processor/status-checkin.key \
  -p "$(systemctl show -p Environment processor-patching.service)" -p WorkingDirectory=/srv/processor/live \
  /srv/processor/live/.venv/bin/processor patching --test-alert
```

Status's side (status#95): `enabled: false` stops only its `missing` sweep, never an alert, so a check-in's `alert` pages whatever the flag. An `ok` after an alert sends Status's own `cleared` notice, so the proof is two messages: *co-processor patching: test (…)*, then *cleared* at the next hourly `ok`.

**Silence it** for a week you mean to skip: `sudo systemctl stop processor-patching.timer`, and the monitor goes `missing`, which is honest; `start` resumes it. Better: post a record saying why, which reads `ok`.

## Node.js (agent tooling only)

Node 24 LTS from NodeSource's apt repo, for `using-mayfly-chat` and SocratiCode (`npx`). The `processor` unit never runs it. The cohort survey and the reasons for NodeSource and for 24 are in #6.

```bash
sudo bash deploy/nodesource.sh install   # key, source, pin, nodejs; ends with check
bash deploy/nodesource.sh check          # exit 0 in sync (a pending update is a note, not drift); 3, naming each drift
```

| Installed | From | Why |
|---|---|---|
| `/etc/apt/keyrings/nodesource.gpg` | `https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key`, refused unless its fingerprint is `6F71 F525 2828 41EE DAF8 51B4 2F59 B5F9 9B1B E0B4` | `Signed-By:` binds the key to this one source |
| `/etc/apt/sources.list.d/nodesource.sources` | [deploy/apt/nodesource.sources](../deploy/apt/nodesource.sources) | `node_24.x`: the major line is pinned |
| `/etc/apt/preferences.d/nodesource.pref` | [deploy/apt/nodesource.pref](../deploy/apt/nodesource.pref) | `nodejs` from NodeSource at 600: above universe's 18 (500), never a downgrade. Everything else the host serves (`nsolid`) at 100, so the lane's `site=` takes only Node |

`node` is the package's `/usr/bin/node`, so it is on every PATH: hooks, systemd user units, VS Code sessions. Installed 2026-09-30: 24.21.0, with npm 11.19.0 bundled (no Ubuntu `npm`); nothing restarted.

**SocratiCode** (#5) runs as `exedev`, pinned to `socraticode@1.16.0` with a capped pre-install under `~/.socraticode/pin`, against co-index's shared store; it never touches the release, and the service can't read `exedev`'s home (`ProtectHome=yes`). They share 7.7 GiB with no swap, and `processor.service` carries no `MemoryLow=`: inert on this host, and the unit's `OOMScoreAdjust=-500` is the protection that counts. Pin, link stubs, memory reasoning: [docs/SOCRATICODE.md](SOCRATICODE.md#repo-specific-notes).

### Patching Node

unattended-upgrades takes Ubuntu's security origins alone ([§ Patching](#patching)), so nothing updates Node on its own. NodeSource ships security fixes in its own repo, never `noble-security`, so its policy is **follow**: `nodejs` rides the weekly maintenance lane. Never a bare `apt-get upgrade`.

**Select it by host, not by origin.** NodeSource's Release file says `Origin: . nodistro`, aptly's default, which every aptly-published `nodistro` repo shares. So the knob names the site, `origin deb.nodesource.com follow` (a value holding a dot is read as a site: the skill's knob.md), and the lane's `APT_CONFIG` takes:

```
Unattended-Upgrade::Origins-Pattern {
  "origin=Ubuntu,archive=${distro_codename}-updates";
  "site=deb.nodesource.com";
};
```

Proven 2026-09-30, with `nodejs` stepped back to 24.20.0: `unattended-upgrade --dry-run` selected it under `site=deb.nodesource.com` and not under the stock security-lane config. `origin=. nodistro` also selects it, but can't tell NodeSource from any other aptly repo. The skill's policy.md cites `nodesource.pref` as its example of scoping a followed origin (gregoryfoster/skills#344).

**An out-of-cycle fix** (a Node security release): `sudo bash deploy/nodesource.sh install`. It refreshes the package lists first, which nothing else here does (`apt-daily.timer` is masked and `APT::Periodic::Enable` is 0), so a bare `apt-get install nodejs` answers "already the newest version" while the release sits unfetched. It then takes the newest `nodejs` candidate, at adj 0 with needrestart listing only, and ends with `check`. `check` alone reads the lists as of the last update.

### Moving the major line

In one commit, edit `node_<N>.x` in `deploy/apt/nodesource.sources`, `EXPECTED_MAJOR` in `tests/test_nodesource.py`, this section's "Node <N> LTS" and table row, and AGENTS.md's Node.js row; the test fails until all of them agree. Then `sudo bash deploy/nodesource.sh install`. Read `init-socraticode`'s preflight first: Node 26 crashes SocratiCode older than 1.13.

### Removing it

```bash
sudo apt-get purge nodejs
sudo rm /etc/apt/keyrings/nodesource.gpg /etc/apt/sources.list.d/nodesource.sources /etc/apt/preferences.d/nodesource.pref
sudo apt-get update
```

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
