# Deployment

Processor runs as one systemd unit, `processor`, on exe.dev VM `co-processor`, as its own user `processor`, from a release that [`scripts/deploy.sh`](#deploy-a-change-scriptsdeploysh) builds from a commit on `origin/main` (#2). What runs is `/srv/processor/live/REVISION`. An hourly timer, `processor-drift.timer`, reports through Status when that lags `origin/main` ([The drift check](#the-drift-check), #35). Design: [the spec](specs/2026-09-29-processor-service-design.md) §2 (identities, grants), §4 (failure handling), §6 (cutover).

## Prerequisites (operator)

| What | Where | Tracked |
|---|---|---|
| Broker ACL user `processor` | `co-broker` | broker#75; live 2026-10-01 (`observo` deleted) |
| Broker credential | `/etc/processor/.env` as `CO_PROCESSOR_BUS_URL` | broker#75 item 5; minted 2026-10-01, `PING broker` as `processor` → `PONG` 22:09:36Z |
| Tailnet `tag:processor` → `tag:broker` on 6379 | tailnet policy | done 2026-09-29 |
| `processor.process` on `content.process` (the hard ordering) | broker | done 2026-10-01 22:09:41Z by `processor ensure-group`: stream empty, group at `0-0`, lag 0 |
| Tailscale `--accept-dns=true`, and tailscaled's OOM drop-in | this VM | done 2026-10-01, [Tailscale DNS](#tailscale-dns) (#8) |
| A direct tailnet path to the broker. On 2026-10-02 it formed under traffic (1 ms, a hairpin through the NAT both VMs share) and fell back to DERP `sea` (16–18 ms) after 150 s idle; on 2026-09-30 it never formed. Not a go-live blocker: only slower over DERP | tailnet / broker side | #15, done for this node: holds under the running service (direct, 1 ms, through 5 min with no pings, 2026-10-02 23:35Z), and through a reboot of `co-processor` (2026-10-03 16:53Z: direct on the first ping, still direct after 5 idle minutes). A broker reboot is the broker's to schedule and is untested |
| Bucket `gs://co-gcs-processor`, UBLA, public access prevention, no lifecycle | GCP | done 2026-10-02, [GCP provisioning](#gcp-provisioning) (spec §2) |
| SA `co-gcs-processor-writer`: `objectCreator` + `objectViewer` on `co-gcs-processor` (**no delete**), `objectViewer` on `co-gcs-blobs` | GCP | done 2026-10-02, [GCP provisioning](#gcp-provisioning) (spec §2) |
| SA key at `/etc/processor/co-gcs-processor-writer.json` (root 600 since #2; the unit's `LoadCredential=`) | this VM | done 2026-10-02; the writer's preflight reached both buckets |
| User `processor` (system, no home, no sudo, not in `docker`); `/srv/processor` (`exedev`, 755); `/etc/processor` root-only | this VM | #2, [Install](#install-once-2s-stage-b) |
| `docker.socket` disabled | this VM | done 2026-10-05 (operator, #2 gate 1); `disabled` and `inactive` on 2026-10-06 |
| Landlock ABI ≥ 6 | kernel | 6 on 2026-10-06 (6.12.93); [Containment](#containment) |
| Tailnet `tag:processor` → `tag:status:9000` | tailnet policy | #35 gate 2 (operator). On 2026-10-06 23:23Z `status` did not resolve from `co-processor` |
| Status tenant `co-processor`, its production key, and the monitor `co-processor-drift` | `co-status` | CannObserv/status#24 (#35 gate 1) |
| Status key at `/etc/processor/status-checkin.key` (root 600; `processor-drift.service`'s `LoadCredential=`) | this VM | #35 gate 3, [The drift check](#the-drift-check) |
| Watcher's reader: `objectViewer` on `co-gcs-processor`, bucket level, for `co-gcs-blob-reader`, the identity Watcher already reads `gs://` blobs with | GCP | done 2026-10-02 (in the bucket's IAM policy); watcher#325 has not yet confirmed that identity |

### Broker credential handoff (hash-only, broker#75 as of 2026-09-30)

The broker node never holds `processor`'s plaintext; since broker#72 its hourly backup refuses a plaintext line. This follows the shape of archiver#251:

1. **On `co-processor`,** mint the password into `/etc/processor/.env` and the password manager. It stays out of argv and shell history, because `printf` is a shell builtin:

   ```bash
   pw="$(set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40)"; [ "${#pw}" -eq 40 ]
   ( umask 077; printf 'CO_PROCESSOR_BUS_URL=redis://processor:%s@broker:6379/0\n' "$pw" >> /etc/processor/.env )
   printf %s "$pw" | sha256sum | cut -d' ' -f1   # post THIS digest on broker#75; it is not the credential
   # copy "$pw" into the password manager, then:
   unset pw
   ```

   **Since #2 the file is root's.** For a rotation, write the line with `printf … "$pw" | sudo tee -a /etc/processor/.env >/dev/null`. `printf` is a builtin, so the password stays out of every argv.
2. **On the broker,** once the digest is posted, the broker's owner runs `ACL SETUSER processor on "#<digest>" …`, `ACL DELUSER observo` and `ACL SAVE`, and records the digest line.
3. **From `co-processor`,** `PING` as `processor`. This is the only verification possible. The password travels in `REDISCLI_AUTH`, never argv:

   ```bash
   REDISCLI_AUTH="$(sed -n 's|^CO_PROCESSOR_BUS_URL=redis://processor:\([^@]*\)@.*|\1|p' /etc/processor/.env)" \
     redis-cli -h broker --user processor PING   # PONG
   ```
4. Then create the group right away (below).

The URL names the broker, never its address: see [Tailscale DNS](#tailscale-dns).

### GCP provisioning

The operator ran this on 2026-10-02 where `gcloud` is authenticated to project `co-gcs`, and the preflight below passed on `co-processor`. It stays here as the record and for re-provisioning; nothing in this repo runs it. It mirrors the cohort's other buckets (Replicator's `docs/INFRASTRUCTURE.md`): `US-WEST1`, `STANDARD`, uniform bucket-level access, and public access prevented. It sets no lifecycle rule, since the output is never deleted (spec D4), and keeps GCS's default 7-day soft delete, as on `co-gcs-replicator`. The writer holds no delete permission, so soft delete only guards against an admin's mistake.

```bash
PROJECT=co-gcs
SA="co-gcs-processor-writer@${PROJECT}.iam.gserviceaccount.com"

# 1. The bucket: derived text, append-only (spec §2, D4).
gcloud storage buckets create gs://co-gcs-processor --project="$PROJECT" \
  --location=US-WEST1 --default-storage-class=STANDARD \
  --uniform-bucket-level-access --public-access-prevention

# 2. The writer identity.
gcloud iam service-accounts create co-gcs-processor-writer --project="$PROJECT" \
  --display-name="processor: derived-text writer (CannObserv/processor)"

# 3. Grants (spec §2). No delete anywhere.
gcloud storage buckets add-iam-policy-binding gs://co-gcs-processor \
  --member="serviceAccount:${SA}" --role=roles/storage.objectCreator
gcloud storage buckets add-iam-policy-binding gs://co-gcs-processor \
  --member="serviceAccount:${SA}" --role=roles/storage.objectViewer
gcloud storage buckets add-iam-policy-binding gs://co-gcs-blobs \
  --member="serviceAccount:${SA}" --role=roles/storage.objectViewer

# 4. Watcher's reader. Proposed: co-gcs-blob-reader, which Watcher already reads
#    gs:// blobs with (GCS_BLOB_CREDENTIALS). It gains nothing new, since the text
#    is derived from blobs that identity can read. Run 2026-10-02, ahead of
#    watcher#325's answer; if Watcher names another identity, grant that one too.
gcloud storage buckets add-iam-policy-binding gs://co-gcs-processor \
  --member="serviceAccount:co-gcs-blob-reader@${PROJECT}.iam.gserviceaccount.com" \
  --role=roles/storage.objectViewer

# 5. The key, straight onto co-processor over SSH stdin, never left on disk here.
(
  set -e; dir="$(mktemp -d)"; trap 'rm -rf "$dir"' EXIT   # mktemp -d is 700
  gcloud iam service-accounts keys create "$dir/key.json" --iam-account="$SA"
  ssh co-processor.exe.xyz \
    'umask 077; sudo tee /etc/processor/co-gcs-processor-writer.json >/dev/null' < "$dir/key.json"   # since #2: root's
)
```

Then, **on `co-processor`**, as it was done on 2026-10-02. **Since #2:**
- the key is root 600, written with `sudo`;
- the unit sets `GOOGLE_APPLICATION_CREDENTIALS` itself, so there is no line in `.env`;
- the deploy's smoke run is the preflight that counts. It writes, as the service, through the credential.

```bash
printf 'GOOGLE_APPLICATION_CREDENTIALS=/etc/processor/co-gcs-processor-writer.json\n' >> /etc/processor/.env
# Preflight both buckets as the writer: a one-object listing each, nothing written.
( cd ~/processor && set -a && . /etc/processor/.env && set +a
  .venv/bin/python -c 'from processor.settings import Settings
from processor.stores import build_stores
s = build_stores(Settings()); s.input.preflight(); s.output.preflight(); print("both buckets reachable")' )
```

To verify the bucket's settings and grants (read-only):

```bash
gcloud storage buckets describe gs://co-gcs-processor \
  --format="default(location, uniform_bucket_level_access, public_access_prevention, soft_delete_policy, lifecycle_config)"
gcloud storage buckets get-iam-policy gs://co-gcs-processor
```

## `/etc/processor/`

`root:root`, mode 700; each file in it is root 600 (#2). systemd reads the env file before it switches to the service user, and hands the key over as a credential, so `processor` can read neither file. Never source the env file into a login shell.

```bash
# /etc/processor/.env
CO_PROCESSOR_BUS_URL=redis://processor:<password>@broker:6379/0
# Optional overrides; defaults in src/processor/settings.py:
# CO_PROCESSOR_CHILD_CONTAINMENT=required    # off: Rollback A only (§ Containment)
# CO_PROCESSOR_EXTRACTION_TIMEOUT_S=120
# CO_PROCESSOR_RLIMIT_AS_BYTES=3221225472
# CO_PROCESSOR_RECLAIM_MIN_IDLE_MS=600000    # must exceed timeout + 60 s
# CO_PROCESSOR_RECLAIM_INTERVAL_S=60
# CO_PROCESSOR_MAX_ATTEMPTS=3
# CO_PROCESSOR_CONSUMER_NAME=co-processor
```

- **The GCS key** is `/etc/processor/co-gcs-processor-writer.json`. The unit loads it with `LoadCredential=gcs-writer-key:…` and sets `GOOGLE_APPLICATION_CREDENTIALS=%d/gcs-writer-key`, which is `/run/credentials/processor.service/gcs-writer-key`.
- **Keep `GOOGLE_APPLICATION_CREDENTIALS` out of `.env`.** `EnvironmentFile=` overrides `Environment=`, so a leftover line would point the service back at the root-only key path, and the preflight would fail.
- **The service user can read the credentials directory, and so can the child.** Landlock refuses it to the child (§ Containment).
- **The Status key** is `/etc/processor/status-checkin.key`, for `processor-drift.service` alone (`LoadCredential=status-checkin-key:…`). That unit reads no `.env`: the drift check never holds the broker credential (§ The drift check).

A command that needs the service's settings runs as the service, through systemd. Don't load the file into a shell:

```bash
processor_cli() {   # e.g. processor_cli dlq list; processor_cli ensure-group
  sudo systemd-run --quiet --pipe --wait --collect -p User=processor -p Group=processor \
    -p EnvironmentFile=/etc/processor/.env -p WorkingDirectory=/srv/processor/live \
    /srv/processor/live/.venv/bin/processor "$@"
}
```

## Releases, not a checkout

**The unit runs a release, never a checkout** (#2). Processor follows the cohort's release standard, broker#22, as CannObserv/status#9 shipped it. That spec is `docs/specs/2026-09-30-deploy-releases-design.md` in CannObserv/status, decisions R1–R13.

```
/srv/processor/                      exedev's, 755
  releases/<build>/   git archive of one commit on origin/main, its .wheelhouse, its own
                      .venv; REVISION written last; a-w, go+rX
  live -> releases/<build>           processor.service, processor-drift.service
```

- **`<build>` is the commit's 12-character short SHA.** `REVISION` holds it. A directory without `REVISION` is an interrupted build, and the next deploy rebuilds it. The `starting` record reports the build, from `REVISION` (`dev` outside a release).
- **Nothing done in a checkout reaches the unit.** Branches, uncommitted edits, `uv sync` and the skills hook's commits all stay in the checkout. `~/processor` stays the operator's clone, and `scripts/deploy.sh` builds from it.
- **The venv:**
  - built with `uv sync --locked --no-dev --no-editable --compile-bytecode --python /usr/bin/python3.12`, inside the release, with `UV_LINK_MODE=copy`. uv's Linux default hardlinks from `~/.cache/uv`, which would make every release file the same inode as the cache's and each dev venv's. The release's `chmod` would then reach them, and an edit to any of them would reach production (CR 11);
  - never synced at start: the unit runs the venv's own `processor` entry point;
  - non-editable, so `sys.path`, and with it the child's allowlist, holds only the stdlib and `site-packages`;
  - on the system interpreter, because `processor` can't read anything under `/home` (`/home/exedev` is 0750, and the unit sets `ProtectHome=yes`).
- **Ownership:** the releases are `exedev`'s and read-only. `processor` reads them but isn't the owner, so it can never make them writable again. That is the cohort's goal 5 without root-owned releases (status#14).

**Where processor differs from Status:**
- there is no `dev` target; the smoke run on the scratch bus is the rehearsal;
- there's no database, so no migration step;
- it verifies through the journal and the smoke run, since it has no HTTP endpoint;
- the venv entry point stands in for `uv run --frozen --no-sync`.

Status's CI gate (status#11) is adopted as § The CI gate (#34), and its drift check (status#12) as § The drift check (#35), which reports to Status instead of healthchecks.io.

## Deploy a change: `scripts/deploy.sh`

Run as `exedev`, from `~/processor`, after the PR merges. It needs `git`, `uv`, `jq`, `curl` and `flock` (util-linux) on the `PATH`, and HTTPS to `api.github.com`, plus `sudo` for `systemctl`, `systemd-run` and the unit file; `co-processor` has all of them. The script fetches `origin` itself:

```bash
cd ~/processor && git pull --ff-only     # for the skills hook; the deploy fetches on its own
scripts/deploy.sh                        # origin/main, once its CI passed
scripts/deploy.sh <build>                # any commit on origin/main with green CI: a rollback
scripts/deploy.sh --skip-ci [<build>]    # without asking CI: an emergency, logged
journalctl -t processor-deploy -n 20     # "CI passed for <build>: <run>", "live -> <build> (was releases/<old>)"
```

A merge that changes nothing under `src/`, `deploy/`, `scripts/deploy.sh`, `pyproject.toml` or `uv.lock` needs no deploy. That list is `RUNTIME_DIRS` and `RUNTIME_FILES` in `src/processor/drift.py`, and the drift check alerts on it (§ The drift check).

**In order:**
0. **Ask CI** whether the commit passed (§ The CI gate). A refusal builds nothing.
1. **Build** `releases/<build>`, or reuse a complete one whose venv still imports `processor`, co-core and lxml. A release that `live` runs is never rebuilt in place. The service user must exist, or nothing switches.
2. **Switch** `live` (an atomic rename). Then install every `deploy/*.service` and `deploy/*.timer` from the release that differs from its installed copy (#35, as status#18), with one `daemon-reload`. A changed timer is `try-restart`ed so it re-arms. Each replaced copy is kept aside for step 5.
3. **Restart.** `reset-failed` comes first, in case a crash loop hit the start limit. The restart lets the in-flight command finish (`KillMode=mixed`: SIGTERM reaches the consumer, not its extraction child), for up to `TimeoutStopSec=240`.
4. **Verify,** within `PROCESSOR_DEPLOY_VERIFY_SECONDS` (300):
   - the new `MainPID` logs `starting` with this build and `child_containment: required`, then `consuming`;
   - then the **smoke run** passes on this build under `required`. It runs `scripts/smoke_scratch_bus.py` through `systemd-run` with the unit's user, env file, credential and sandboxing:
     - **Bus:** the scratch Redis (db 14), never the broker.
     - **Command:** one real command through the real consumer loop and the contained child.
     - **Input:** from the committed real corpus.
     - **Output:** the production bucket, write-if-absent, so a repeat run writes nothing new.
     - **Pass:** it prints `"result": "pass"` when the fact matches Watcher's recorded fingerprint, the entry is acked, and the object reads back intact.
5. **On failure:** switch back (the units too: replaced copies go back, added ones are removed), restart, and prove the old build the same way.
   - Exit 1 when the old build answers.
   - Exit 4 when nothing answers: the old build failed too, or there was nothing to switch back to. On a first deploy, the unit it replaced goes back and the service restarts on it, and § Rollback applies.

**On success,** a timer this deploy installed for the first time is enabled (`systemctl enable --now`), so a deploy that was switched back never ran it. A timer installed before is never enabled again, so an operator's `disable` sticks. A timer that won't enable or re-arm is a note, never a switch back: it watches the deploy, it never decides one. Still by hand: `sudo systemctl reenable <unit>` after its `[Install]` section changes, and retiring a unit (`disable --now`, remove the file, `daemon-reload`), since a unit gone from `deploy/` stays installed.

Then the host configs under `deploy/` (tailscaled's drop-in, NodeSource's apt files) are compared with their installed copies. A difference is a note, never an install. The deploy keeps the 5 most recently deployed releases plus `live`.

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
- **`--skip-ci`** deploys without asking GitHub at all: for a fix that can't wait for CI, or when GitHub can't answer. It is logged before anything is built, as `CI not checked for <build> (--skip-ci)`. A gate that passes is logged too, as `CI passed for <build>: <run>`.

## The drift check

**Live lagging `origin/main` in code that runs alerts through Status** (#35, ported from status#12). `processor-drift.timer` starts `processor-drift.service` 5 min after boot, then hourly. It runs `/srv/processor/live/.venv/bin/processor drift` as `processor`: it asks GitHub how far live's `REVISION` is behind `main`, then checks in once to Status's monitor `co-processor-drift` (tenant `co-processor`, CannObserv/status#24) at `http://status:9000`. The rules are in `src/processor/drift.py`.

| Live | Check-in (`kind`) | What to do |
|---|---|---|
| `main`; behind only in what never runs; behind in code for 8 h or less since the push that brought it | `ok` (`ok`) | nothing |
| behind in code for more than 8 h | `alert` (`lag`), every hour until deployed. It names `main`'s CI result | `scripts/deploy.sh`. If `main`'s CI isn't green, § The CI gate refuses: fix `main` first |
| not on `main`: diverged, or GitHub doesn't know live's commit (a rewritten `main`) | `alert` (`off_main`) | deploy a commit on `main`; `journalctl -t processor-deploy` says how live got there |
| unstamped: the release has no `REVISION` | `alert` (`unstamped`) | `live` names something that isn't a finished release: deploy |
| GitHub can't answer: a rate limit, a 5xx, a timeout, a wrong-shaped answer | none; the run exits 1 | nothing. The monitor's grace absorbs one missed run |

- **What runs:** `src/`, `deploy/`, `scripts/deploy.sh`, `pyproject.toml`, `uv.lock`. A rename counts by either name. Docs, tests, CI, skills and the other scripts never count, however old.
- **The check is itself runtime:** it lives in `src/` and its units in `deploy/`. A change to it needs a deploy, and lags like any other.
- **The clock** starts at the push, from its CI run's `created_at`, never the commit date. Within the grace, the body names the oldest push since live, which may be docs only.
- **GitHub's budget:** unauthenticated, 60 requests an hour per address, shared with § The CI gate. A run costs 1 request in sync or behind in docs only, 2 behind in code, and at most 10 past the grace.
- **Apart from `processor run`:** a oneshot with `TimeoutStartSec=90`, no `Restart=`, and no retry. A GitHub or Status outage costs it its exit code and nothing else. It reads no `.env`, so it never holds the broker credential.

**`missing` on `co-processor-drift` means the check isn't reporting.** The cause is one of: the timer is stopped, `co-processor` is down, the tailnet path to `status` is gone, the key is wrong or missing, Status refuses, or GitHub stayed silent for two runs in a row. It says nothing about `processor run`. To find out which:

```bash
systemctl list-timers processor-drift.timer
systemctl status processor-drift.service
journalctl -u processor-drift -o cat | jq -cR 'fromjson? | select(.message == "drift check") | {timestamp, level, kind, checkin, body}'
```

Each run logs one `drift check` record: `build`, `main`, `kind`, `body`, and `checkin`. `checkin` is one of:
- `202`;
- `null`: GitHub was silent;
- `failed: <Status's answer>`;
- `not sent: no <what>`: the monitor id or the key is missing.

The level is INFO for `ok`, WARNING for an alert or a silent GitHub, and ERROR when the check-in failed. Exit 0 means Status took the check-in.

**Run it now:** `sudo systemctl start processor-drift.service`, then read the journal as above.

**Silence a lag you mean to keep.** It alerts hourly until deployed. `sudo systemctl stop processor-drift.timer` stops the alerts, and the monitor then goes `missing`, which is honest. `start` resumes them. A deploy never re-enables a timer it installed before, so `disable --now` keeps it stopped across reboots too.

**A deliberate alert** proves the channels end to end. It sends `kind: test`, and asks GitHub nothing:

```bash
sudo systemd-run --quiet --pipe --wait --collect -p User=processor -p Group=processor \
  -p LoadCredential=status-checkin-key:/etc/processor/status-checkin.key \
  -p "$(systemctl show -p Environment processor-drift.service)" -p WorkingDirectory=/srv/processor/live \
  /srv/processor/live/.venv/bin/processor drift --test-alert
```

**Installing the key** (once; #35 gate 3). Status's operator mints it into `/etc/status/pending/co-processor.key` on `co-status` (status#24), and it is read across terminal to terminal. It never goes on a command line, into an issue or into a chat:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/processor/status-checkin.key
sudo tee /etc/processor/status-checkin.key >/dev/null     # paste the key, Enter, then Ctrl-D
sudo systemctl start processor-drift.service              # once the timer is installed: check in now
```

Installed before the deploy that brings the timer, the timer's first run (at the enable) checks in.

## Containment

The extraction child is assumed compromised by the document it parses (spec §3, #2). Three layers protect against that:
- **The child contains itself:** Landlock (read-only on a derived allowlist, no TCP, abstract sockets and signals scoped) and seccomp (no socket of any family).
- **The parent is undumpable.**
- **The service runs as `processor`,** reading releases it doesn't own.

- **Prerequisites:**
  - Landlock ABI ≥ 6 (Linux 6.12; `co-processor`: 6);
  - x86_64 or aarch64;
  - `docker.socket` disabled. It is enabled by default on exeuntu, and `/run/docker.sock` meant root to the `docker` group; the child can't reach any socket now, but `processor` isn't in that group either.
- **Fail closed.** Under `CO_PROCESSOR_CHILD_CONTAINMENT=required` (the default), `processor run` exits 1 with `child containment unavailable` where the kernel can't contain the child, and systemd restarts it, so the unit flaps visibly.
- **The boot canary.** Before it takes any command, `processor run` makes one real extraction (a tiny HTML page) through the child, under the configured containment. It exits 1 with `child canary failed` (`kind`, `detail`) if that fails. This covers what the ABI check can't see, such as a library outside the allowlist after a host update, which would otherwise crash every command three times and publish a terminal `extraction_error` for each. A child that fails to contain itself exits 70 before it reads its request, which counts as a strike.
- **Debugging the parent.** Being undumpable means no core dump, and `py-spy dump` or `gdb -p` need root (`sudo py-spy dump --pid <pid>`). The child is still dumpable, and its environment holds only `PATH` and `LANG`.
- **What the child can still reach:**
  - the kernel's other syscalls;
  - its own code, the stdlib and the shared libraries;
  - CPU up to the timeout, and memory up to `RLIMIT_AS`;
  - its own command's output, which only Watcher's comparison checks.

**Check on the host** (after an install, a kernel change, or a unit edit):

```bash
pid=$(systemctl show -p MainPID --value processor)
ps -o user= -p "$pid"                                            # processor
grep NoNewPrivs "/proc/$pid/status"                              # 1
sudo -u processor cat "/proc/$pid/environ" >/dev/null            # Permission denied (undumpable)
sudo -u processor cat /etc/processor/.env >/dev/null             # Permission denied
systemctl is-enabled docker.socket                               # disabled
# The key as the unit receives it: readable by the service's uid through an ACL,
# from anywhere on the host, so by the child's but for Landlock (CR 9). The control
# proves the uid can read it, so the second line's refusal is Landlock's.
key=/run/credentials/processor.service/gcs-writer-key
(cd / && sudo -u processor head -c1 "$key" >/dev/null) && echo "control: readable"
(cd / && sudo -u processor /srv/processor/live/.venv/bin/python -I -c \
  "from processor._contain import contain; contain('required'); open('$key')") # PermissionError
journalctl -u processor -o cat | jq -cR 'fromjson? | select(.message == "starting") | {build, child_containment, landlock_abi}' | tail -n 1
```

`tests/test_child.py` proves each denial on the host it runs on, against the real `/etc/processor/.env`, the key (on disk, and as the unit's credential) and the repo's `.env` when they're readable there. After the install `exedev` can read none of the first three, so on `co-processor` those tests skip, and the two `key` lines above are the evidence. Each denial is paired with an uncontained control. `tests/test_contain.py` proves each layer alone. On `co-processor` the suite never skips them.

**Rollback A, containment alone:** add `CO_PROCESSOR_CHILD_CONTAINMENT=off` to `/etc/processor/.env` and `sudo systemctl restart processor`. A deploy then fails its verify, which requires `required`, so take the line out before the next deploy.

## Install (once; #2's Stage B)

On 2026-10-02 the service was installed as `exedev`, running `~/processor` (below). #2 moves it to the `processor` user and to releases. As root where marked:

```bash
sudo install -d -m 700 /var/backups/processor/2
sudo cp -a /etc/systemd/system/processor.service /etc/processor /var/backups/processor/2/
sudo useradd --system --user-group --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin processor
sudo install -d -o exedev -g exedev -m 755 /srv/processor
sudo sed -i '/^GOOGLE_APPLICATION_CREDENTIALS=/d' /etc/processor/.env
sudo chown -R root:root /etc/processor && sudo chmod 700 /etc/processor
sudo chmod 600 /etc/processor/.env /etc/processor/co-gcs-processor-writer.json   # by name: a glob is expanded as exedev, and * skips .env
cd ~/processor && git pull --ff-only && scripts/deploy.sh   # builds, installs the unit, restarts, verifies
```

Then run § Containment's checks, and wait for the next shadow command: `ack: complete` with its digests, and Watcher still reporting a match.

## Rollback

- **A failed deploy** switches back by itself (exit 1). `scripts/deploy.sh <old build>` rolls back on purpose; the old release still exists among the 5 kept, so nothing is rebuilt. It is gated on the old build's CI like any deploy; if GitHub can't answer, add `--skip-ci` (§ The CI gate).
- **The install** (Stage B), back to `exedev` running `~/processor`:

  ```bash
  sudo cp -a /var/backups/processor/2/processor.service /etc/systemd/system/processor.service
  sudo rm -rf /etc/processor && sudo cp -a /var/backups/processor/2/processor /etc/processor
  sudo systemctl daemon-reload && sudo systemctl reset-failed processor && sudo systemctl restart processor
  ```

  The backup has the old ownership (`exedev`) and the `GOOGLE_APPLICATION_CREDENTIALS` line. The user and `/srv/processor` can stay.

## Before #2: the first install (2026-10-02)

The service ran as `exedev` from this checkout (`uv sync --frozen --no-dev`; `WorkingDirectory=/home/exedev/processor`) until #2's install.

**Installed on `co-processor` 2026-10-02 23:27:27Z** (`main` at `6e518d8`): `starting`, then `consuming` within a second; 65 MB charged to the unit's cgroup (`MemoryCurrent`, page cache included; the cap is `MemoryMax=6G`). A smoke test ran one real blob through `handler.handle` with the production stores and child, and its publish captured locally (never the broker). The blob was `2e38aa5e…`, from Watcher's real corpus.
- Read 89 ms, extract 550 ms, store 102 ms.
- The stored text's digest equals Watcher's recorded fingerprint, and it read back intact.
- A second run was write-if-absent.
- It left one permanent object, `gs://co-gcs-processor/blobs/b8f6d0f1ec63d0b4c0e20fec0052871704f2353b59663cb715ca3eb783600bdf.bin` (17,327 bytes): exactly what Watcher's command for that revision produces.
- 23:45:29Z: `scripts/smoke_scratch_bus.py` passed on the same input. That is the full loop on the scratch bus (read, child, store, publish, ack) against the production bucket, and its write was a no-op on the existing object.

On boot, the service preflights both buckets and exits non-zero if either is unreachable, and systemd restarts it. Broker outages do not stop the service: the loop backs off from 1 s up to 30 s and retries.

## Create the group: as soon as the credential verifies

`processor.process` must exist before Watcher's first command (spec §6, the hard ordering). A group created later from `$` skips earlier entries. On 2026-09-30 broker#75 found `content.process` absent, so creating the group then skipped nothing. Done 2026-10-01; idempotent: `processor_cli ensure-group` (§ `/etc/processor/`).

## Operate

- **Drift:** `journalctl -u processor-drift` (§ The drift check).
- **Logs:** `journalctl -u processor`. `starting` names the `build`, `child_containment` and `landlock_abi`. Deploys: `journalctl -t processor-deploy`. Each outcome has `command_id`, `info_source_id`, `action` (`ack` / `strike` / `leave_pending` / `dead_letter`), `reason`, `detail`, and timings in `*_ms`. Each also has `input_digest` once it is valid, as bare hex like the command's. A complete fact adds `output_digest` (`sha256:<hex>`, as on the fact; the object is `blobs/<hex>.bin`; absent when `empty`), `output_size_bytes`, `empty` and `processor_version`, and so does a `leave_pending` whose publish failed after the store. A shadow command's output: `journalctl -u processor -o cat | jq -cR 'fromjson? | select(.command_id == "<id>") | {action, reason, input_digest, output_digest}'` (`-R … fromjson?` skips systemd's own lines, which `-u` includes and plain `jq` aborts on).
- **Dead letters:** `processor_cli dlq list | show <id> | drop <id>` (§ `/etc/processor/`). There is no replay. An entry failed to decode, was not a command, or is a command that raised outside the handler on every attempt (`reason` starts `gave up on attempt`): that one is a bug to fix. Before dead-lettering such a command, Processor published a terminal `extraction_error` whose `detail` starts `dead-lettered:` (best effort), so Watcher has closed it (#17). The journal's `dead-lettering` record says which: `failure_fact` is `published`, `skipped` (a fact had already gone out) or `refused`. On a command given up at the cap, `handle_skipped` is `true` when the record retries a give-up that did not finish (the failure fact refused transiently, or the dead-letter refused), with no re-run of the command and the original traceback (#28). The same entry logging that every ~11 min while it stays pending means `content.process.dlq` refuses it: `journalctl -u processor -o cat | jq -cR 'fromjson? | select(.handle_skipped) | {timestamp, command_id, message_id, reason}'`.
- **Lag / missing group:** the broker probe watches `processor.process` (broker#75).
- **Strikes** are counted in memory. A restart resets them, so a poison command gets at most 3 more attempts.

## Bumping co-core

co-core is pinned `==` in lockstep with Watcher (spec §5). A bump changes `processor_version` on every fact, so it is planned with Watcher:

1. Rebuild the wheelhouse at the new tag, then update the pin in `pyproject.toml` and `EXPECTED` in `tests/test_pin.py`, and `uv lock`.
2. Regenerate the goldens from Watcher on the new version (`scripts/gen_parity_goldens.py`). They must not change, or the bump note says why output moved.
3. Deploy together with Watcher's bump, or neither moves.

## Tailscale DNS

Since #8 (2026-10-01) this VM runs Tailscale with `--accept-dns=true`, as every cohort node does (observo#631, notifier#43 D8). It had run with DNS off since provisioning: the `tailscale up` of 2026-09-29, from an Observo session (observo#629), passed `--accept-dns=false`, and nothing records a reason.

How it works here:

- exeuntu has no `systemd-resolved`, so tailscaled runs in direct mode and writes `/etc/resolv.conf` itself, pointing it at MagicDNS (`100.100.100.100`).
- exe.dev wrote `nameserver 169.254.169.254` (its resolver) into `/etc/resolv.conf` at the VM's first boot: the file has the same 2026-09-29 22:50:42 mtime as `/etc/hosts`. At boot, before systemd starts, exe.dev's init rewrote it when it differed: after the 2026-10-01 hard reset it replaced the tailscaled file it found. It left it alone when it matched, after that day's graceful reboot, as it has `/etc/hosts` at both boots. tailscaled keeps that file as `/etc/resolv.pre-tailscale-backup.conf` and forwards public names to it, since the tailnet sets no global resolvers. The unit's `ExecStopPost=/usr/sbin/tailscaled --cleanup` puts it back on a clean stop, and tailscaled takes it over again when it starts.
- `broker` resolves through MagicDNS, so the bus URL names it and never its address. Broker's `docs/RECOVERY.md` rebuilds the node under the same name with a new address. Only the peers the tailnet policy shows this node resolve; on 2026-10-01 that was `broker` alone.

**`CorpDNS` must be true.** It can be false while `tailscale status` looks healthy: the peers are listed, and every tailnet name still fails (archiver#193). `tests/test_tailscaled.py` checks it on this node.

```bash
tailscale debug prefs | grep CorpDNS     # true
getent hosts broker storage.googleapis.com
```

### tailscaled's OOM rank

The accepted cost: tailscaled sits in the path of every lookup on the host, as well as carrying the bus. [deploy/tailscaled.service.d/90-processor-oom.conf](../deploy/tailscaled.service.d/90-processor-oom.conf) ranks it at `-950`, below `processor.service` (`-500`). `OOMScoreAdjust=` applies at exec, so restart tailscaled after installing it. The restart blips DNS and the tailnet: on 2026-10-01 `broker` resolved again 2.4 s after it, and `oom_score` went from 670 to 37.

```bash
sudo install -D -m 644 deploy/tailscaled.service.d/90-processor-oom.conf /etc/systemd/system/tailscaled.service.d/90-processor-oom.conf
sudo systemctl daemon-reload && sudo systemctl restart tailscaled
cat /proc/$(systemctl show -p MainPID --value tailscaled)/oom_score_adj   # -950
```

### At boot

**Proven across a graceful reboot (`sudo systemctl reboot`) and a hard reset (`exe.dev restart`), both on 2026-10-01.** A probe ran at each boot:

- One unit snapshots `/etc/resolv.conf` and the backup before tailscaled starts (from the hard reset on).
- A second unit times each lookup from tailscaled's start. It would have turned Tailscale DNS off if public names had still failed after 90 s.

The probe, its two units and its log are in `/var/backups/processor/8/` (root only). To arm it again, copy both units to `/etc/systemd/system/` and `systemctl enable` both; the second unit disables both after one run.

The graceful reboot:

- tailscaled started 1.1 s into boot, at `-950`, with `CorpDNS: true`.
- Public names resolved on the probe's first try (+0.5 s), through exe.dev's resolver in the file the stop hook had restored, which was in place from boot.
- tailscaled took the file over at +1.46 s, exact from the file's mtime. `broker` resolved by +1.65 s (replicator#88 measured about 2 s).
- The boot's journal has no name-resolution error.

The hard reset skips the stop hook, so tailscaled's own file and its backup were still in place when the VM went down:

- exe.dev's init replaced the file with `nameserver 169.254.169.254` 1.5 s into boot (mtime at 1.534 s), as it started its guest daemon (`exe-init guestd`, 1.53 s). guestd runs in PID 1's own `init.scope` rather than as a service, the mark of a process exe-init forked before it exec'd systemd. journald came up at 1.84 s. So every lookup a systemd unit makes before tailscaled takes over goes to exe.dev's resolver, as after a clean stop.
- tailscaled started at 2.1 s, at `-950`, with `CorpDNS: true`. It deleted the backup the reset left behind, then took the file over at +1.60 s, exact from the file's mtime, backing up exe.dev's fresh copy.
- Public names resolved on the probe's first try (+0.8 s). `broker` and `index` resolved by +1.67 s.
- The boot's journal has no name-resolution error.

The second unit polled every 0.25 s, starting 0.32 s (reboot) and 0.44 s (reset) after tailscaled, so the lookup times are upper bounds.

**With the unit installed, a graceful reboot on 2026-10-03 (#15)** (the first boot since the 2026-10-02 install):

- The stop hook restored exe.dev's resolver at 16:53:14Z. tailscaled and `processor` started together at 16:53:19.4–19.7Z, and tailscaled took the file over at 19.4 + 1.35 s.
- So the GCS preflight resolved through exe.dev's resolver. `starting` came at 21.74 and `consuming` at 21.76, about 1 s after `broker` became resolvable.
- `NRestarts=0`. No backoff warning, no preflight failure, and no name-resolution error in the boot's journal.
- The broker path was direct on the first ping and still direct after 5 idle minutes.
- `tests/test_main.py` pins the restart path: preflight failure exits 1, `Restart=on-failure`, inside the default start limit.

On both paths, tailnet names such as `broker` don't resolve until tailscaled takes the file over, 1.5–1.6 s after it starts; public names do. The bus loop backs off and retries a name that does not resolve yet, as it does for any broker fault (`tests/test_consumer.py`). The GCS preflight's listing retries a connection error for up to 120 s (the storage client's `DEFAULT_RETRY`). A start that still fails exits 1, and systemd restarts it 5 s later.

Check after a reboot:

```bash
tailscale debug prefs | grep CorpDNS                                       # true
getent hosts broker storage.googleapis.com github.com
cat /proc/$(systemctl show -p MainPID --value tailscaled)/oom_score_adj   # -950
journalctl -u processor -b | grep -ciE 'name or service not known|temporary failure in name resolution'
```

### Rollback

```bash
sudo tailscale set --accept-dns=false   # tailscaled moves its backup back over /etc/resolv.conf
grep nameserver /etc/resolv.conf        # 169.254.169.254
```

The backup is gone afterwards (measured 2026-10-01), so there is nothing left to copy. Restore it by hand only while tailscaled is down and its own file is still in place: `sudo cp /etc/resolv.pre-tailscale-backup.conf /etc/resolv.conf`. Once tailscaled is back up, run the `set` above, or it takes the file over again.

After this, `broker` stops resolving. Until DNS is back on, put the broker's address from `tailscale status` into `CO_PROCESSOR_BUS_URL` and restart the service.

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

### Patching

The cohort's posture is `scheduled` (gregoryfoster/skills `patching-hosts`): one owner-approved monthly window, apt timers masked, so nothing updates Node on its own. Processor declares it nowhere yet: there is no `.skills/patching-hosts` knob (see below for why), so the skill would read this host as its no-knob default, `automatic`, report-only. NodeSource ships security fixes in its own repo, never `noble-security`, so its policy is **follow**: `nodejs` rides the monthly maintenance lane. Never a bare `apt-get upgrade`.

**Select it by host, not by origin.** NodeSource's Release file says `Origin: . nodistro`, aptly's default, which every aptly-published `nodistro` repo shares. The lane's `APT_CONFIG`:

```
Unattended-Upgrade::Origins-Pattern {
  "origin=Ubuntu,archive=${distro_codename}-updates";
  "site=deb.nodesource.com";
};
```

Proven 2026-09-30, with `nodejs` stepped back to 24.20.0: `unattended-upgrade --dry-run` selected it under `site=deb.nodesource.com` and not under the stock security-lane config. `origin=. nodistro` also selects it, but can't tell NodeSource from any other aptly repo. The `.skills/patching-hosts` knob's `origin <origin> follow` line can't hold a value with a space, so this repo commits no knob line for NodeSource yet.

**An out-of-cycle fix** (a Node security release): `sudo bash deploy/nodesource.sh install`. It refreshes the package lists first, which nothing else here does (`apt-daily.timer` is masked and `APT::Periodic::Enable` is 0), so a bare `apt-get install nodejs` answers "already the newest version" while the release sits unfetched. It then takes the newest `nodejs` candidate, at adj 0 with needrestart listing only, and ends with `check`. `check` alone reads the lists as of the last update.

### Moving the major line

In one commit, edit `node_<N>.x` in `deploy/apt/nodesource.sources`, `EXPECTED_MAJOR` in `tests/test_nodesource.py`, this section's "Node <N> LTS" and table row, and AGENTS.md's Node.js row; the test fails until all of them agree. Then `sudo bash deploy/nodesource.sh install`. Read `init-socraticode`'s preflight first: Node 26 crashes SocratiCode older than 1.13.

### Removing it

```bash
sudo apt-get purge nodejs
sudo rm /etc/apt/keyrings/nodesource.gpg /etc/apt/sources.list.d/nodesource.sources /etc/apt/preferences.d/nodesource.pref
sudo apt-get update
```
