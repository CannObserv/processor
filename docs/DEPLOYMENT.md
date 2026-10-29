# Deployment

Processor runs as one systemd unit, `processor`, on exe.dev VM `co-processor`, as its own user `processor`, from a release that [`scripts/deploy.sh`](#deploy-a-change-scriptsdeploysh) builds from a commit on `origin/main` (#2). What runs is `/srv/processor/live/REVISION`. An hourly timer, `processor-drift.timer`, reports through Status when that lags `origin/main` ([The drift check](#the-drift-check), #35). `processor run` itself checks in to Status every 5 min while it is consuming ([Liveness](#liveness), #39). Design: [the spec](specs/2026-09-29-processor-service-design.md) §2 (identities, grants), §4 (failure handling), §6 (cutover).

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
| Tailnet `tag:processor` → `tag:status:9000` | tailnet policy | #35 gate 2, done 2026-10-07: `http://status:9000/health` answered `production` from `co-processor` at 14:03Z |
| Status tenant `co-processor`, its production key, and the monitor `co-processor-drift` (`01M4BHMGGTXWQ16J3G5WWDQRYQ`) | `co-status` | CannObserv/status#24 (#35 gate 1), done 2026-10-07 16:02Z |
| Status key at `/etc/processor/status-checkin.key` (root 600; `processor-drift.service`'s `LoadCredential=`) | this VM | #35 gate 3, [The drift check](#the-drift-check) |
| Watcher's reader: `objectViewer` on `co-gcs-processor`, bucket level, for `co-gcs-blob-reader`, the identity Watcher already reads `gs://` blobs with | GCP | done 2026-10-02 (in the bucket's IAM policy); Watcher confirmed that identity on watcher#325 the same day ([its answers](https://github.com/CannObserv/watcher/issues/325#issuecomment-5958475061)) |

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
# CO_PROCESSOR_LIVE_INTERVAL_S=300           # change co-processor-live's interval with it (§ Liveness)
# CO_PROCESSOR_LIVE_CHECKIN_TIMEOUT_S=10     # below the interval
```

- **The GCS key** is `/etc/processor/co-gcs-processor-writer.json`. The unit loads it with `LoadCredential=gcs-writer-key:…` and sets `GOOGLE_APPLICATION_CREDENTIALS=%d/gcs-writer-key`, which is `/run/credentials/processor.service/gcs-writer-key`.
- **Keep `GOOGLE_APPLICATION_CREDENTIALS` out of `.env`.** `EnvironmentFile=` overrides `Environment=`, so a leftover line would point the service back at the root-only key path, and the preflight would fail.
- **The service user can read the credentials directory, and so can the child.** Landlock refuses it to the child (§ Containment).
- **The Status key** is `/etc/processor/status-checkin.key`. Both units load it as `LoadCredential=status-checkin-key:…`:
  - `processor-drift.service`, which reads no `.env`, so the drift check never holds the broker credential (§ The drift check);
  - `processor.service`, since #39, for the liveness check-in (§ Liveness). Landlock refuses it to the child, as it refuses the GCS key.

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
  releases/<build>/   git archive of one commit on origin/main, the wheelhouse files its
                      uv.lock names (each held to its pinned sha256), its own .venv;
                      REVISION written last; a-w, go+rX
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
journalctl -t processor-deploy -n 20     # "CI passed for …", "wheelhouse for <build>: … match their pinned sha256", "live -> <build> (was …)", then "live is on <build>: pid <N> …"
```

**The journal holds the verdict** (`-t processor-deploy`, #93). A verified deploy ends with `live is on <build>: pid <N> logged starting (child_containment required), then consuming; the smoke run passed`: both proofs of step 4, `<N>` the new `MainPID`. A `live -> <build>` with no `live is on <build>` after it was never verified: a rollback (`live rolled back to …` / `… NOT answering`), an interrupt (below), or a SIGKILL, which leaves nothing. Before #93's deploy only the switch was journaled.

A merge that changes nothing under `src/`, `deploy/`, `scripts/deploy.sh`, `pyproject.toml` or `uv.lock` needs no deploy. That list is `RUNTIME_DIRS` and `RUNTIME_FILES` in `src/processor/drift.py`, and the drift check alerts on it (§ The drift check).

**In order:**
0. **Ask CI** whether the commit passed (§ The CI gate). A refusal builds nothing.
1. **Build** `releases/<build>`, or reuse a complete one whose venv still imports `processor`, co-core and lxml. A release that `live` runs is never rebuilt in place. The service user must exist, or nothing switches.
   - **The wheelhouse check** (#87, as broker#125):
     - only the files the release's `uv.lock` names are copied from `~/processor/.wheelhouse`;
     - each copy is held to the sha256 in the release's own `pyproject.toml` `[tool.processor.wheelhouse-sha256]`, before `uv sync`. uv records no digest for a find-links file, so `uv sync --locked` would install any file of that name.
   - A pass is journaled: `wheelhouse for <build>: <files> match their pinned sha256`.
   - A refusal is journaled too (`refused <build>: …; nothing switched`, exit 1). It fires on a file that misses its pin, one with no pin, one the wheelhouse lacks, or a lock that names no wheelhouse file at all.
   - A mismatch says to remove the file, populate the wheelhouse again (AGENTS.md; a local build: § Bumping co-core) and deploy again. This deploy doesn't run the sync: it checks what the checkout holds.
   - A reused release isn't checked again. Builds from before #87's deploy were never checked.
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

Then the host configs under `deploy/` (`HOST_CONFIGS`: tailscaled's drop-in, the apt files, needrestart's, the disk GC's) are compared with their installed copies. A difference is a note, never an install, and is journaled, as is an installed unit the release lacks. The deploy keeps the 5 most recently deployed releases plus `live`.

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
- **The lock audit is not in the gate.** `audit.yml` is its own workflow, so its run never counts, and a finding never blocks a deploy (§ The lock audit).
- **`--skip-ci`** deploys without asking GitHub at all: for a fix that can't wait for CI, or when GitHub can't answer. It is logged before anything is built, as `CI not checked for <build> (--skip-ci)`. A gate that passes is logged too, as `CI passed for <build>: <run>`.

## CI pins and Dependabot

Ported from broker#88 and broker#90 (#80).

**Pins.** Every `uses:` in `ci.yml` and `audit.yml` takes the form `owner/action@<40-hex sha> # vX.Y.Z`. A tag can be moved, and setup-uv publishes no floating major since v8. As of #80:

| Action | Pin |
|---|---|
| `actions/checkout` | v7.0.1 |
| `astral-sh/setup-uv` | v10.3.2 |
| `google-github-actions/auth` | v3.0.0 |

These are broker's majors (operator, 2026-10-29). `tests/test_dependabot.py` holds the form. `tests/test_ci.py` holds each major to a floor, so a Dependabot major passes and a downgrade fails. `tests/test_lock_audit.py` holds `audit.yml`'s pins equal to `ci.yml`'s.

That a SHA *is* its tag needs the network, so it is checked by hand whenever a pin moves:

```bash
gh api repos/<owner>/<action>/git/ref/tags/<tag> --jq '.object.type + " " + .object.sha'
```

- `commit`: that SHA is the pin.
- `tag`: an annotated tag, whose SHA names the tag object rather than a commit. The pin is the commit it points at: `gh api repos/<owner>/<action>/git/tags/<sha> --jq .object.sha`. setup-uv's old `v7` was one.

**The cache.** `ci.yml` sets `prune-cache: true` on both setup-uv steps, because setup-uv v9 flipped the default to false. The repo is public, and a fork's PR can restore a cache saved on `main`. Unpruned, that cache holds co-core unpacked from the private wheelhouse. The prune drops downloaded wheels and keeps wheels built from source, so it covers co-core only while `uv.lock` locks co-core to a wheel. `tests/test_ci.py` checks both.

**Dependabot** (`.github/dependabot.yml`) proposes action bumps only: `github-actions`, weekly, at most 10 open PRs, with `chore:` subjects.
- **Never `uv` or `pip`.** Processor owns the exact co-core pin and holds the extraction stack ([§ Extraction dependencies](#extraction-dependencies)). The wheelhouse is private, so Dependabot's updater couldn't sync it anyway. `uv.lock` has its own signal ([§ The lock audit](#the-lock-audit)).
- **Settings → Code security** (operator reading, 2026-10-29, #80):
  - Dependency graph: **on**. It is the `Graph Update: uv in /.` run on each push, and opens nothing.
  - Dependabot alerts: **on**. These are the operator's.
  - Dependabot security updates: **off**, and they stay off. They open PRs that move `uv.lock` whatever `dependabot.yml` says.

  No test can see these settings, and our token gets 403 on both APIs. Re-read them in the UI whenever the repo's security settings change.

**A Dependabot run reads its own credential context.** GitHub runs it as if from a fork: a read-only `GITHUB_TOKEN`, and Dependabot secrets only.
- `ci.yml` needs no secret. It needs `vars.GCP_WIF_PROVIDER` to resolve, plus the OIDC token that `id-token: write` asks for.
- Both held on CannObserv/watcher's Dependabot PRs (same provider, same SA; watcher#356, watcher#364, per broker's `docs/CI.md`). This repo's first Dependabot PR is the confirmation here.
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
- A major bump that changes the pin's floor moves `ACTIONS` in `tests/test_ci.py` too, in a commit on the same branch.
- **No deploy:** an action bump touches `.github/` alone, which is outside `RUNTIME_*` ([§ The drift check](#the-drift-check)).

## The lock audit

Disclosures arrive without commits: #71's 12 advisories surfaced only because someone ran `uv audit` by hand. Two things read the lock against OSV now:

- **`audit.yml`, the signal.** `.github/workflows/audit.yml` runs `scripts/audit_lock.sh` on every push to `main`, every PR, weekly (`audit.yml`'s cron), and on dispatch (#73, ported from broker#86). A push or PR run is seen by whoever made the change. A scheduled run that finds something is still green, so it notifies nobody (#75).
- **The weekly window, the human read** (#75, operator 2026-10-26). Each run's after-probe (§ Patching, step 9) runs `uv audit --frozen` on `/srv/processor/live/uv.lock`, the **live** lock, as the lock file's owner, `exedev`, on a copy, never in the tree. It asks OSV live, so a disclosure `audit.yml`'s Monday 14:37Z run saw is read again in the window, which opens 14 h later. The count goes in the run's record, and the operator reads it. `tests/test_patching_knob.py` keeps the units' `WorkingDirectory=`, the knob's `owner` line and `scripts/deploy.sh`'s live link equal, so a refactor can't move the probe off the lock unnoticed.

The script and its workflow:

- **Report-only.** The script always exits 0. A finding (uv exit 1) is a `uv.lock advisories` warning, with the audit and where it goes in the job summary. Any other failure is a `uv audit did not run` warning, never a clean result. A red gate would turn `main` red on someone else's disclosure date.
- **Outside the deploy gate.** The gate reads `ci.yml`'s push run alone (§ The CI gate); `tests/test_lock_audit.py` holds both workflows to that.
- **No secret, no WIF.** `uv audit --frozen` reads the lock without the wheelhouse. OSV's text is echoed between `::stop-commands::` and a random token, so it can't issue workflow commands.
- **By hand:** `bash scripts/audit_lock.sh` from any directory audits this checkout's lock.
- **Where a finding goes:** every finding is `uv lock --upgrade-package <name>` plus a `[tool.uv] constraint-dependencies` floor with its check in `tests/test_lock_floors.py`, so a later re-lock can't undo it. A package in the extraction stack also bumps `LOCAL_GENERATION`, with the notice: § Extraction dependencies (#71 did both for pypdf). Re-locks touch `uv.lock`, so deploy after the merge.

What the window doesn't cover:
- A disclosure mid-week waits for the next window, at most 7 days. Between windows, `audit.yml` alone sees it.
- A skipped window means no read that week, and no reading says so. A run without step 9 leaves the class in its other probes' `not_read`.
- A unit moved off the lock would read clean: the probe finds trees only through `WorkingDirectory=`, and one with no lockfile still reads complete. That is the test's case.
- The run never re-locks. A finding there goes to "Where a finding goes", above, outside the window.

**Reopen path:** if a window is skipped twice running, or an advisory that matters reaches production between windows, take #75's option 2: the scheduled run opens or updates an issue, with `issues: write` on that job only.

## The drift check

**Live lagging `origin/main` in code that runs alerts through Status** (#35, ported from status#12). `processor-drift.timer` starts `processor-drift.service` 5 min after boot, then hourly. It runs `/srv/processor/live/.venv/bin/processor drift` as `processor`: it asks GitHub how far live's `REVISION` is behind `main`, then checks in once to Status's monitor `co-processor-drift` (tenant `co-processor`, CannObserv/status#24) at `http://status:9000`. The rules are in `src/processor/drift.py`.

| Live | Check-in (`kind`) | What to do |
|---|---|---|
| `main`; behind only in what never runs; behind in code for 8 h or less since the push that brought it | `ok` (`ok`) | nothing |
| behind in code for more than 8 h | `alert` (`lag`), every hour until deployed. It names `main`'s CI result | `scripts/deploy.sh`. If `main`'s CI isn't green, § The CI gate refuses: fix `main` first |
| not on `main`: diverged, or GitHub doesn't know live's commit (a rewritten `main`) | `alert` (`off_main`) | deploy a commit on `main`; `journalctl -t processor-deploy` says how live got there |
| unstamped: the release has no `REVISION` | `alert` (`unstamped`) | `live` names something that isn't a finished release: deploy |
| GitHub can't answer: a rate limit, a 5xx, a timeout, a wrong-shaped answer | none; the run exits 1 | nothing. The monitor's grace absorbs one missed run |

- **Each `alert` names its kind as `metadata.fault`** (#58; status#28, the contract of `co-archiver-drift` and `co-broker-drift`); an `ok` sends no `metadata`. Status groups a run of alerts into one fault by it, so a change of kind (`lag` → `off_main`, say) reports at once, not at the next daily reminder.
- **What runs:** `src/`, `deploy/`, `scripts/deploy.sh`, `pyproject.toml`, `uv.lock`. A rename counts by either name. Docs, tests, CI, skills and the other scripts never count, however old.
- **The check is itself runtime:** it lives in `src/` and its units in `deploy/`. A change to it needs a deploy, and lags like any other.
- **The clock** starts at the push, from its CI run's `created_at`, never the commit date. Within the grace, the body names the oldest push since live, which may be docs only.
- **GitHub's budget:** unauthenticated, 60 requests an hour per address, shared with § The CI gate. A run costs 1 request in sync or behind in docs only, 2 behind in code, and at most 10 past the grace.
- **Apart from `processor run`:** a oneshot with `TimeoutStartSec=90`, no `Restart=`, and no retry. No GitHub call starts past 60 s, but each is bounded per read, not in total, so the 90 s is the hard stop. A run killed there sends nothing, which reads as a GitHub-silent run. A GitHub or Status outage costs it its exit code and nothing else. It reads no `.env`, so it never holds the broker credential.

**`missing` on `co-processor-drift` means the check isn't reporting.** The cause is one of: the timer is stopped, live was rolled back past #35 (§ Rollback), `co-processor` is down, the tailnet path to `status` is gone, the key is wrong or missing, Status refuses, or GitHub stayed silent for two runs in a row. It says nothing about `processor run` (that is `co-processor-live`, § Liveness). To find out which:

```bash
systemctl list-timers processor-drift.timer
systemctl status processor-drift.service
journalctl -u processor-drift -o cat | jq -cR 'fromjson? | select(.message == "drift check") | {timestamp, level, kind, checkin, body}'
```

Each run logs one `drift check` record: `build`, `main`, `kind`, `body`, and `checkin`. `checkin` is one of:
- `202`;
- `null`: GitHub was silent;
- `failed: <Status's answer>`;
- `not sent: missing <what>`: the monitor id or the key.

The level is INFO for `ok`, WARNING for an alert or a silent GitHub, and ERROR when the check-in failed. Exit 0 means Status took the check-in.

**Run it now:** `sudo systemctl start processor-drift.service`, then read the journal as above.

**Silence a lag you mean to keep.** It alerts hourly until deployed. `sudo systemctl stop processor-drift.timer` stops the alerts, and the monitor then goes `missing`, which is honest. `start` resumes them. A deploy never re-enables a timer it installed before, so `disable --now` keeps it stopped across reboots too.

**A deliberate alert** proves the channels end to end. It sends `kind: test`, and asks GitHub nothing. Its fault is `test`, so sent while a real fault is open it reports at once, as a fault of its own; the next hourly run's alert, back to the real kind, reports at once too:

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

## Liveness

**`processor run` checks in to Status while it is consuming** (#39). A task beside the consume loop checks in `ok` to Status's monitor `co-processor-live` (tenant `co-processor`, CannObserv/status#24) every 5 min. It does so only while the loop has made progress within 605 s: a finished read (an empty one counts), a handled message, or a backoff. It never sends `alert`. When it stops checking in, Status's `missing` is the page. The code is `src/processor/liveness.py`.

| Monitor | Value |
|---|---|
| interval | 300 s (`CO_PROCESSOR_LIVE_INTERVAL_S`; change the monitor's with it) |
| grace | 900 s: Status pages after 20 min of silence |
| renotify | daily |
| variables | `build`, `last_progress_age_s`, `backing_off` |

- **The monitor id** is in `deploy/processor.service` (`CO_PROCESSOR_LIVE_MONITOR_ID`). Without it, liveness is off: dev and CI.
- **The key** is the drift check's, the same file (§ `/etc/processor/`). If it's missing, the service still starts. `starting` then says `liveness: off: missing the status-checkin-key credential`, an ERROR record `liveness check-in off` follows, and the monitor goes `missing`.
- **Never in consumption's way.** Each check-in runs in a daemon thread, bounded at 10 s, with one attempt per tick. A tick whose check-in is still running is skipped. A Status outage costs log lines, never a command's time.
- **605 s** is `reclaim_min_idle_ms + read_block_ms`: the reclaim's own bound for a dead command, plus one read. A reclaim walk over a backlog counts as progress per message, so it is never a wedge.
- **The grace covers a deploy.** The worst case is about 850 s:
  - up to 300 s since the last check-in;
  - a 240 s stop (`TimeoutStopSec`, while the in-flight command finishes);
  - a 300 s verify of a new build that never starts;
  - then the switch back, whose old build checks in about 5 s after it starts.

  A normal deploy (about 5 s) never pages.

**`missing` on `co-processor-live` means `processor run` isn't consuming**, or can't say so. It is one of:
- the service is stopped, or crash-looping (a failed preflight, a failed canary, refused containment);
- the consume loop is wedged: no read, message or backoff has finished in 605 s, though the process is up;
- the key is missing or wrong, so the check-ins fail;
- `co-processor` is down, the tailnet path to `status` is gone, or Status refuses.

It never means the queue is empty. A broker outage isn't it either: that backs off, which counts as consuming, and the variables say `backing_off: true`. To find out which:

```bash
systemctl status processor
journalctl -u processor -o cat | jq -cR 'fromjson? | select(.message == "starting") | {timestamp, build, liveness}' | tail -n 1
journalctl -u processor -o cat --since -1h | jq -cR 'fromjson? | select(.logger == "processor.liveness") | {timestamp, level, message, error, last_progress_age_s}'
```

| Record | Level | Means |
|---|---|---|
| `liveness check-in` | INFO | the first `ok` after start, a failure or a silence; a steady `ok` logs nothing |
| `liveness check-in failed` | WARNING | Status refused or didn't answer (`error`) |
| `consume loop stale; not checking in` | WARNING | the wedge: `systemctl restart processor` (a stop lets the in-flight command finish), and keep the journal for an issue |
| `previous check-in still in flight; skipping this tick` | WARNING | Status hangs past the timeout |

**A deliberate stop longer than 20 min pages.** Ask Status's operator to pause the monitor first (`"enabled": false`), and to resume it after.

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
for key in /run/credentials/processor.service/{gcs-writer-key,status-checkin-key}; do
  (cd / && sudo -u processor head -c1 "$key" >/dev/null) && echo "control: readable"
  (cd / && sudo -u processor /srv/processor/live/.venv/bin/python -I -c \
    "from processor._contain import contain; contain('required'); open('$key')") # PermissionError
done
journalctl -u processor -o cat | jq -cR 'fromjson? | select(.message == "starting") | {build, child_containment, landlock_abi}' | tail -n 1
```

`tests/test_child.py` proves each denial on the host it runs on, against the real `/etc/processor/.env`, both keys (on disk, and as the unit's credentials) and the repo's `.env` when they're readable there. After the install `exedev` can read none but the last, so on `co-processor` those tests skip, and the `key` loop above is the evidence. Each denial is paired with an uncontained control. `tests/test_contain.py` proves each layer alone. On `co-processor` the suite never skips them.

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
- **Liveness:** `journalctl -u processor -o cat | jq -cR 'fromjson? | select(.logger == "processor.liveness")'` (§ Liveness).
- **Logs:** `journalctl -u processor`. A record's journald `PRIORITY` follows its `level` (CRITICAL 2, ERROR 3, WARNING 4, INFO 6, DEBUG 7; #76), so `journalctl -u processor -p warning` lists WARNING and above, with systemd's own warning lines for the unit; add `-o cat | jq -cR 'fromjson?'` for the JSON alone. Every `processor` entry point (so both units, `processor` and `processor-drift`) writes an sd-daemon `<N>` prefix only when its stderr is the journal (`JOURNAL_STREAM` names stderr's own device and inode), and journald strips it, so `MESSAGE` stays pure JSON. A terminal, `processor_cli` and any `systemd-run --pipe` never see one. Records from before #76's deploy (`live -> 431b7b569300` at 2026-10-25T15:50:36Z in `journalctl -t processor-deploy`) are all `PRIORITY=6`: filter those by `.level`. Only `logging` records carry a priority: a bare traceback or a `print` to stderr is still `PRIORITY=6`. A record past journald's `LineMax` (48K) is split, and only its first part carries the priority (neither part is JSON). `starting` names the `build`, `child_containment`, `landlock_abi` and `liveness`. Deploys: `journalctl -t processor-deploy`. Each outcome has `command_id`, `info_source_id`, `action` (`ack` / `strike` / `leave_pending` / `dead_letter`), `reason`, `detail`, and timings in `*_ms`. Each also has `input_digest` once it is valid, as bare hex like the command's. A complete fact adds `output_digest` (`sha256:<hex>`, as on the fact; the object is `blobs/<hex>.bin`; absent when `empty`), `output_size_bytes`, `empty` and `processor_version`, and so does a `leave_pending` whose publish failed after the store. A shadow command's output: `journalctl -u processor -o cat | jq -cR 'fromjson? | select(.command_id == "<id>") | {action, reason, input_digest, output_digest}'` (`-R … fromjson?` skips systemd's own lines, which `-u` includes and plain `jq` aborts on).
- **Dead letters:** `processor_cli dlq list | show <id> | drop <id>` (§ `/etc/processor/`). There is no replay. An entry failed to decode, was not a command, or is a command that raised outside the handler on every attempt (`reason` starts `gave up on attempt`): that one is a bug to fix. Before dead-lettering such a command, Processor published a terminal `dead_lettered` whose `detail` starts `dead-lettered:` (best effort), so Watcher has closed it (#17; `extraction_error` before co-core 0.37.0, #21). The journal's `dead-lettering` record says which: `failure_fact` is `published`, `skipped` (a fact had already gone out) or `refused`. On a command given up at the cap, `handle_skipped` is `true` when the record retries a give-up that did not finish (the failure fact refused transiently, or the dead-letter refused), with no re-run of the command and the original traceback (#28). The same entry logging that every ~11 min while it stays pending means `content.process.dlq` refuses it: `journalctl -u processor -o cat | jq -cR 'fromjson? | select(.handle_skipped) | {timestamp, command_id, message_id, reason}'`.
- **Lag / missing group:** the broker probe watches `processor.process` (broker#75).
- **Strikes** are counted in memory. A restart resets them, so a poison command gets at most 3 more attempts.

## Bumping co-core

co-core is pinned `==`, and Processor owns the pin (spec D5, §5; amended 2026-10-10, #47). Until watcher#350 it was in lockstep with Watcher. A bump changes `processor_version` on every fact, and Watcher re-baselines on that:

1. Rebuild the wheelhouse at the new tag, then update the pin in `pyproject.toml`, `EXPECTED` in `tests/test_pin.py` and the default tag in `scripts/build_wheelhouse.sh` (the pin test holds all three), and `uv lock`.
   - **The wheelhouse digests move in the same diff** (#87). `[tool.processor.wheelhouse-sha256]` in `pyproject.toml` gets one row per file the new lock names: `sha256sum .wheelhouse/<file>`. Drop the old rows. `tests/test_pin.py` holds the names to `EXPECTED`, and `tests/test_sync_wheelhouse.py` holds them to the lock. Until then, every sync and deploy refuses.
   - **Two sources, compared, never one picked.** With no co-pypi-reader key here, the rows come from `scripts/build_wheelhouse.sh`. CI's from-empty sync is the bucket's side: it refuses a download that misses a row, so a green PR is the comparison.
   - **A local build matches the bucket's only on the same build backend.** cannobserv's `requires = ["hatchling"]` is unpinned. hatchling normalizes timestamps, but writes its version into `.dist-info/WHEEL` (`Generator:`). 0.38.0 built with 1.33.0 differs from the 1.32.4 build (measured 2026-10-28). `build_wheelhouse.sh` constrains hatchling to `HATCHLING` (the pin test holds it). Re-check it at each bump: `unzip -p <wheel> '*.dist-info/WHEEL'` names the generator.
   - **CI red on a digest means the two sources disagree.** Stop and find out why (the `Generator:` line first), before pinning either. Each digest stops a later swap, not one made at pin time: that waits on cannobserv#591's `SHA256SUMS`.
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

Since #71: `0.38.0+2`, pypdf 6.21.2, opentelemetry-api 1.45.1 (not stack; a floor only).

## TLS trust

The service's TLS trust is certifi's bundle in the release venv, not the system store: GCS (`google-auth` / `google-cloud-storage` over `requests`) and the drift check's GitHub calls verify against `certifi.where()`, and nothing sets `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`. A CA distrust (exe.dev's system-store remediation doesn't reach the venv) is a certifi bump: raise `constraint-dependencies` in `pyproject.toml` and the floor in `tests/test_tls_trust.py`, `uv lock --upgrade-package certifi` (the diff moves certifi alone, never `uv lock --upgrade`), merge, then `scripts/deploy.sh`. The restart is the fix: a running process keeps the bundle it loaded. Check from the release venv: `/srv/processor/live/.venv/bin/python -I -c "import certifi; print(certifi.__version__)"`. certifi isn't co-core, so `processor_version` doesn't move and Watcher needs no notice. Since #66: ≥ 2026.10.19, without `emSign ECC Root CA - G3` (the eMudhra compromise, https://exe.dev/security/emudhra-compromise).

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

## Tailnet flaps

**The rule** (operator, 2026-10-23, #67): "A tailnet flap gets an issue only if it delays a command, pages, or outlasts liveness's 900 s grace. Any other flap is logged on the epic with its window and signature."

A flap is the tailnet path to `broker` going away for minutes while tailscaled keeps running. The consumer backs off and retries, with no restart (§ Liveness). It has ridden out both flaps so far:

| Window | What it was | Processor |
|---|---|---|
| 2026-10-10 02:26–04:12Z | Path churn between direct (peers at the shared NAT `16.145.19.221`) and DERP. No `LinkChange` and no clock jump | 19 WARNINGs, 14 `open-conn-track` timeouts, no command affected |
| 2026-10-22 04:13:38–04:15:30Z | A host-side event on five of the six VMs behind egress `32.185.164.108` (broker, processor, replicator, status, notifier): memory-balloon pressure, then each VM paused, staggered over about 40 min | 6 WARNINGs, 1 failed check-in, no command delayed, nothing paged |

**The 2026-10-22 signature,** in order:

- The journal: no entry at all 04:12:53–04:13:26Z (32.7 s, the length of the jump). An idle VM can be that quiet, so this suggests the pause rather than proving it.
- kernel: `virtio_balloon virtio5: Out of puff! Can't get 1 pages` ×26, 04:13:26–38Z. The host was inflating the balloon, and the guest had no pages to give back. These were the only balloon messages on this boot (since 2026-10-03).
- `processor`: `consumer loop error; backing off`, `error` `TimeoutError: Timeout reading from broker:6379`, then `Timeout connecting to server` until the path is back (backoff 1, 2, 4 … s).
- tailscaled:
  - `magicsock: endpoints changed`, with a second STUN address beside the usual one;
  - `LinkChange: major, rebinding … rebind-reason=[time-jumped(33s),ips-changed]`. The wall clock jumped 33 s, which is what a paused VM looks like from inside;
  - `closing connection to derp-10 (rebind-ping-fail)`, then a reconnect;
  - `open-conn-track` timeouts.
- `processor`: possibly one `liveness check-in failed` (the 10 s bound). The next check-in lands inside the 900 s grace, so nothing pages.
- Recovery on its own: `NRestarts=0`, and `ack: complete` resumes. A command issued during the gap stays in the stream and is acked once the path is back.

**The cohort saw the same event** (a Mayfly questionnaire, 2026-10-23; synthesis on #67, the broker's timeline on CannObserv/broker#118):
- co-broker and co-replicator, behind the same egress, each logged the first balloon pressure of their boot (04:13:07Z and 04:13:39Z), then a 31–33 s time jump;
- co-status (moved onto that egress during the event) had balloon pressure at 04:14:04Z and a pause at about 04:17:07–24Z;
- notifier, behind it too, had the same 19 min later: balloon pressure at 04:32:48Z, then a pause logged at 04:51:10Z;
- on two small VMs without swap (status, notifier), the balloon pushed memory to earlyoom's floor, and earlyoom killed a user process (not a service);
- co-index, behind the same egress, logged neither. It has room to spare, and `Out of puff` appears only when the guest *can't* give pages back, so a quiet balloon there isn't ruled out. It did pause on its own on 2026-10-18 around 04:06Z; co-processor didn't;
- co-registrar (archiver), co-provisioner and co-watcher, each on its own egress, logged neither; they only lost `broker` for about 2 min.

The events were staggered per VM, not one simultaneous pause. Every bus client lost `broker` at once, from about 04:13:18 to 04:15:33Z. That window matches the broker's balloon reclaim, not its own pause, which its clock places about 04:16:14–43Z (CannObserv/broker#118).

exe.dev's docs don't describe maintenance, host migration, ballooning or pauses. The cause is a host-side memory reclaim they don't explain. Asking exe.dev is the operator's step (CannObserv/broker#118).

**Telling it apart:**

- **A broker-side problem** (a Redis restart, the broker's patch window, broker#91, or a host pause of the broker alone, which is how archiver, provisioner and watcher saw 2026-10-22): this VM's tailscaled logs no `LinkChange` or `time-jumped`. The broker's patch run (2026-10-13 04:17–04:22Z) left backoff WARNINGs at its Redis restart and at its reboot, and `open-conn-track` timeouts during the reboot.
- **A tailscaled restart here** (#65, 2026-10-22 23:40:29Z): tailscaled gets a new `MainPID` and logs `linkChange: in state NoState`. The tailnet is down for 2.3 s, and the broker socket survives with no WARNING.
- **A flap:** this VM's tailscaled logs `endpoints changed` and `LinkChange` (and, on 2026-10-22, `time-jumped`), or peers go stale or relayed in `tailscale status`.

```bash
journalctl -u processor -p warning -o cat --since <start> | jq -cR 'fromjson? | {timestamp, level, logger, message, error}'
journalctl -u tailscaled -o short-iso --since <start> | grep -E 'LinkChange|endpoints changed|rebind-reason|closing connection to derp|open-conn-track'
journalctl -k -o short-iso --since <start> | grep -E 'Out of puff|balloon'
systemctl show processor -p NRestarts
tailscale status
```

`-p warning` does the level filter (#76) and `fromjson?` drops systemd's own warning lines; for a window before #76's deploy, every record is `PRIORITY=6`, so drop `-p warning` and filter with `select(.level != "INFO")` instead (§ Operate → Logs).

Log a flap on the epic as: the window (first WARNING to the first `ack: complete` after it), the counts above, and which kind it was.

## Patching

Posture **`scheduled`** (#77), under gregoryfoster/skills `patching-hosts` ([skills/patching-hosts](../skills/patching-hosts/SKILL.md)), read from the knob [.skills/patching-hosts](../.skills/patching-hosts). The owner approves a run **every week**, in **`Tue 05:00–10:00` UTC**: Monday night Pacific, 21:00–02:00 PST or 22:00–03:00 PDT (operator, 2026-10-26). That is more often than the skill's policy.md, which has `scheduled` hosts run once a month; there's no knob directive for the cadence, so this is a declared deviation. The window opens as the broker's closes (`Mon 23:00–05:00`, CannObserv/broker#67). Both VMs hold the bus link, so the hosts never patch at once and a flap stays attributable to one host. Each run is approved in this repo's session, like the first.

The apt timers stay masked and `APT::Periodic::Enable` stays 0 (both from the image). Both lanes run in the window: security plus `-updates` plus the followed origins, `--lane maintenance`. **Never a bare `apt-get upgrade`:** it takes every origin. The skill's [run.md](../skills/patching-hosts/references/run.md) is the procedure; this section is what is particular to this host.

| Knob line | Why |
|---|---|
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

Install once, **before any apt run**, from the deploy checkout's root, after `git pull --ff-only` (until then it lags `origin/main` and has neither file):

```bash
sudo install -m 644 deploy/needrestart.conf.d/processor.conf /etc/needrestart/conf.d/processor.conf
sudo needrestart -m u -b -r l    # "Disabling Ubuntu mode, explicit restart mode configured"; never without -r l
sudo install -m 644 deploy/apt/tailscale.pref /etc/apt/preferences.d/tailscale.pref
sudo tailscale set --auto-update=false   # the tailnet's switch is off (broker#67); this is the node's own
uv run pytest tests/test_needrestart.py tests/test_tailscaled.py   # on the node, the installed and live tests run and pass
```

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
6. The reboot decision: `sudo needrestart -b -r l` and `sudo grep -c '(deleted)' /proc/1/maps`. The kernel never decides one on exe.dev. Expect a reboot when `systemd` or `libsystemd0` is in the set: after those families, needrestart listed dbus and logind on co-broker and co-status, and PID 1 mapped deleted files.
7. The reboot: `reboot-chain.sh --run <run>` prints the chain; `--approve` launches it. It stops `processor` (up to `TimeoutStopSec=240`), then reboots in-guest. **Never `exe.dev restart`**: that is a hard reset (#83).
8. After it: `probe.sh --post-boot --run <run>`, § Tailscale DNS → *Check after a reboot*, `NRestarts=0`, and drift and liveness `ok` in Status. The disk sensor's first tick after boot logs `checked in ok (ok): HTTP 202` within 10 min ([§ Disk GC](#disk-gc)). Read `journalctl -u processor -p warning --since <window start>` raw (systemd's own lines, such as a failed stop) and through `-o cat | jq -cR 'fromjson? | …'`. A crash is `PRIORITY=6`, so check `NRestarts` too.
9. The after-probe, last, after the reboot or the no-reboot restart below: `probe.sh --refresh-into <dir>`. It is what's left pending, by class, for the record, and the only step that audits the live `uv.lock` (`pending.language`). The dry run and `--post-boot` never ask OSV ([§ The lock audit](#the-lock-audit)).

**With no reboot,** restart the service anyway after a libssl update: `/usr/bin/python3.12`'s `_ssl` links the system `libssl.so.3`, so the old library stays mapped until a restart. `sudo grep -c 'libssl.so.3 (deleted)' /proc/$(systemctl show -p MainPID --value processor)/maps` must read 0 afterwards.

**Liveness's grace** is 900 s. The heartbeat ticks as soon as the service starts, so the silence is at most the time since the last tick (300 s), plus the stop (240 s), plus the boot. That stays inside the grace, so Status needs a warning only if a step stalls.

**Deploys don't wait for a window** (operator, 2026-10-26, #77). The skill's policy.md says a deploy that restarts a declared `service` takes one (gregoryfoster/skills#366). Here `scripts/deploy.sh` runs after each merge, as before: it restarts `processor` alone, verifies the new build and switches back on failure ([§ Deploy a change](#deploy-a-change-scriptsdeploysh)). A merge that lands during a window is deployed after the post-boot checks, so a flap stays attributable.

**The record** goes on the issue (`records CannObserv/processor`), per run.md §7, and ends with the next window's date. What each run leaves pending, by class, comes from step 9 and includes:
- **Ubuntu Pro / ESM:** Pro isn't attached, and 25 esm-apps updates were visible on 2026-10-26.
- **Language:** `/srv/processor/live/uv.lock`, its OSV advisory count, owner `self`. Run.md §7 asks for the owning repo's update mechanism; here that is `audit.yml`, report-only ([§ The lock audit](#the-lock-audit)). Dependabot covers Actions only, never `uv.lock`, and its security updates are off ([§ CI pins and Dependabot](#ci-pins-and-dependabot)). A count above 0 goes to that section's "Where a finding goes" and is linked from the record. A tree listed as not audited, or the class in `not_read`, is recorded as no read, never as 0.

## Disk GC

Provisioner's disk GC and headroom sensor (provisioner#61), **VERSION 2**, installed and turned on through gregoryfoster/skills `maintaining-host-disk` ([skills/maintaining-host-disk](../skills/maintaining-host-disk/SKILL.md), #82). The skill's [turning-on.md](../skills/maintaining-host-disk/references/turning-on.md) is the procedure; this section is what's particular to this host. The GC's tiers and rules are Provisioner's. A change to them is raised there, through the operator, and never patched into the installed copy: the installer refuses a copy that doesn't match its MANIFEST.

| What | Where |
|---|---|
| The GC, its units, the journald cap | `/usr/local/lib/cannobserv/disk-gc/` (`VERSION`, `MANIFEST`); `/usr/local/lib/systemd/system/cannobserv-disk-{gc,headroom}.{service,timer}`; `/usr/local/lib/systemd/journald.conf.d/zz-cannobserv-journal.conf` (`SystemMaxUse=512M`; the journal was 87M on 2026-10-27) |
| Config | [deploy/disk-gc/disk-gc.conf](../deploy/disk-gc/disk-gc.conf) → `/etc/cannobserv/disk-gc/disk-gc.conf` (root, 0644). Installed by hand; `scripts/deploy.sh` compares the two |
| Key | `/etc/cannobserv/disk-gc/checkin.key` (root, 0600): a copy of the tenant's `/etc/processor/status-checkin.key`. **Rotating the tenant key means updating both** |
| State | `/var/lib/cannobserv-disk-gc/`: the GC's `state.json`, and the sensor's hold in `sensor.json` |
| Timers | `cannobserv-disk-gc.timer`: daily at 05:15 UTC, plus up to 30 min, with no catch-up at boot. `cannobserv-disk-headroom.timer`: every 10 min |
| Monitor | `co-processor-disk-headroom` (`01M5ZQBHEX06T5D50Q96QYKCR6`, CannObserv/status#69), tenant `co-processor`: 600 / 1800 / renotify 3600. Its faults are `low-headroom`, `gc-failed`, `gc-stale` (72 h), `unreadable` and `test` |

**This host's choices** (each one is a comment in the config):
- **Homes:** `homes` is `/home/exedev` (`processor` has no home).
- **Tiers:** the template's, plus `worktrees` for this repo's `.worktrees/`. `docker` stays off, because the daemon is inactive. `npx_spec_files` keeps the SocratiCode pin in the deploy checkout's `.claude/settings.json`.
- **uv cache:** pruned only (`uv_clean_above_mb = 0`). Releases are built with `UV_LINK_MODE=copy`.
- **Lines:** alert at 85% Use% (as `df` reports it), clear at 80%. On 2026-10-27, `/` was 76% used with 1.8G under the line, before the first prune (estimated at about 3.5G of idle VS Code builds and caches; the report-only run measures it, #82).
- **pytest:** `pyproject.toml` keeps one basetemp, and only a failed run's (`tmp_path_retention_*`). A run's basetemp is about 62M.

**Install or change the config** from the deploy checkout's root, after `git pull --ff-only`. Each install is a sudo step, approved:

```bash
sudo install -D -m 0644 -o root -g root deploy/disk-gc/disk-gc.conf /etc/cannobserv/disk-gc/disk-gc.conf
uv run pytest tests/test_disk_gc.py   # on the node: the installed copy matches, and the installed GC accepts it
```

**With § Patching:**
- **patching-hosts reads `[sensor] mount` and `alert_at_percent` as a floor** (gregoryfoster/skills#390). It refuses a step whose download and install would take `/` to 85%, and stops on a config the GC would refuse.
- **The GC's daily run (05:15–05:45) falls inside the Tuesday window.** It never touches apt's trees, and it yields: `Nice=10`, idle I/O.
- **After a reboot,** if the GC runs before VS Code reconnects, it finds no build running and keeps none. The next session re-downloads its build (about 640M). That's accepted (#82).
- **A run the reboot cuts off** finishes the next day. A missed day pages only as `gc-stale`, after 72 h.

**Read it** without sudo: `exedev` is in `adm`, so `journalctl -u cannobserv-disk-headroom -p err --since -1d` reads the alerts. verifying.md's `sudo` is for users outside `adm`, for whom the same command prints `-- No entries --`. Its rule holds here too: only `checked in ok (ok): HTTP 202` proves a check-in.

**Updates and removal** are the skill's ([SKILL.md](../skills/maintaining-host-disk/SKILL.md)). A new release arrives as a pin bump in the daily skills-vendor refresh.

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

The host's posture is `scheduled` ([§ Patching](#patching)), so nothing updates Node on its own. NodeSource ships security fixes in its own repo, never `noble-security`, so its policy is **follow**: `nodejs` rides the weekly maintenance lane. Never a bare `apt-get upgrade`.

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
