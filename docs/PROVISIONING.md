# Provisioning

What the operator set up once, on `co-processor` and around it: grants and credentials, `/etc/processor/`, the child's containment, the install, the consumer group. Deploying and operating: [DEPLOYMENT.md](DEPLOYMENT.md).

## Prerequisites (operator)

| What | Where | Tracked |
|---|---|---|
| Broker ACL user `processor` | `co-broker` | broker#75; live 2026-10-01 (`observo` deleted) |
| Broker credential | `/etc/processor/.env` as `CO_PROCESSOR_BUS_URL` | broker#75 item 5; minted 2026-10-01, `PING broker` as `processor` → `PONG` 22:09:36Z |
| Tailnet `tag:processor` → `tag:broker` on 6379 | tailnet policy | done 2026-09-29 |
| `processor.process` on `content.process` (the hard ordering) | broker | done 2026-10-01 22:09:41Z by `processor ensure-group`: stream empty, group at `0-0`, lag 0 |
| Tailscale `--accept-dns=true`, and tailscaled's OOM drop-in | this VM | done 2026-10-01, [Tailscale DNS](NETWORK.md#tailscale-dns) (#8) |
| A direct tailnet path to the broker. On 2026-10-02 it formed under traffic (1 ms, a hairpin through the NAT both VMs share) and fell back to DERP `sea` (16–18 ms) after 150 s idle; on 2026-09-30 it never formed. Not a go-live blocker: only slower over DERP | tailnet / broker side | #15, done for this node: holds under the running service (direct, 1 ms, through 5 min with no pings, 2026-10-02 23:35Z), and through a reboot of `co-processor` (2026-10-03 16:53Z: direct on the first ping, still direct after 5 idle minutes). A broker reboot is the broker's to schedule and is untested |
| Bucket `gs://co-gcs-processor`, UBLA, public access prevention, no lifecycle | GCP | done 2026-10-02, [GCP provisioning](#gcp-provisioning) (spec §2) |
| SA `co-gcs-processor-writer`: `objectCreator` + `objectViewer` on `co-gcs-processor` (**no delete**), `objectViewer` on `co-gcs-blobs` | GCP | done 2026-10-02, [GCP provisioning](#gcp-provisioning) (spec §2) |
| SA key at `/etc/processor/co-gcs-processor-writer.json` (root 600 since #2; the unit's `LoadCredential=`) | this VM | done 2026-10-02; the writer's preflight reached both buckets |
| User `processor` (system, no home, no sudo, not in `docker`); `/srv/processor` (`exedev`, 755); `/etc/processor` root-only | this VM | #2, [Install](#install-once-2s-stage-b) |
| `docker.socket` disabled | this VM | done 2026-10-05 (operator, #2 gate 1); `disabled` and `inactive` on 2026-10-06 |
| Landlock ABI ≥ 6 | kernel | 6 on 2026-10-06 (6.12.93); [Containment](#containment) |
| Tailnet `tag:processor` → `tag:status:9000` | tailnet policy | #35 gate 2, done 2026-10-07: `http://status:9000/health` answered `production` from `co-processor` at 14:03Z |
| Status tenant `co-processor`, its production key, and the monitor `co-processor-drift` (`01M4BHMGGTXWQ16J3G5WWDQRYQ`) | `co-status` | CannObserv/status#24 (#35 gate 1), done 2026-10-07 16:02Z |
| Status key at `/etc/processor/status-checkin.key` (root 600; `processor-drift.service`'s `LoadCredential=`) | this VM | #35 gate 3, [The drift check](MONITORING.md#the-drift-check) |
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

The URL names the broker, never its address: see [Tailscale DNS](NETWORK.md#tailscale-dns).

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

**The co-pypi reader's key (#121).** Deploy-time only: `scripts/co-pypi.sh` mints the index's token from it for one `uv` command, as `exedev`. It's a key for `co-pypi-reader@co-gcs`, which already holds `roles/artifactregistry.reader` on `co-pypi` (CannObserv/cannobserv#554), never a grant to the writer: the runtime identity parses untrusted documents. It sits outside `/etc/processor/`, which is root 700 and the service's. Placed 2026-11-06 (key `57797cdb…`):

```bash
# on co-processor
sudo install -d -o root -g exedev -m 750 /etc/processor-deploy
# where gcloud is authenticated; the key is never left on disk there
(
  set -e; dir="$(mktemp -d)"; trap 'rm -rf "$dir"' EXIT
  gcloud iam service-accounts keys create "$dir/key.json" \
    --iam-account=co-pypi-reader@co-gcs.iam.gserviceaccount.com
  ssh co-processor.exe.xyz \
    'sudo install -o root -g exedev -m 640 /dev/stdin /etc/processor-deploy/co-pypi-reader.json' < "$dir/key.json"
)
```

Check it by metadata alone (`ls -l /etc/processor-deploy`), then by use: `bash scripts/co-pypi.sh uv lock --locked --refresh-package co-core`. The refresh makes uv ask the index, so a token that can't read it fails: with no token that command gets a 401. A plain `uv lock --locked` resolves from the lock alone and proves only the mint. `PROCESSOR_CO_PYPI_KEY` points the wrapper at another key, for another machine. `tests/test_child.py` proves the contained child can't open it, without reading it.

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
# CO_PROCESSOR_LIVE_INTERVAL_S=300           # change co-processor-live's interval with it (MONITORING.md § Liveness)
# CO_PROCESSOR_LIVE_CHECKIN_TIMEOUT_S=10     # below the interval
```

- **The GCS key** is `/etc/processor/co-gcs-processor-writer.json`. The unit loads it with `LoadCredential=gcs-writer-key:…` and sets `GOOGLE_APPLICATION_CREDENTIALS=%d/gcs-writer-key`, which is `/run/credentials/processor.service/gcs-writer-key`.
- **Keep `GOOGLE_APPLICATION_CREDENTIALS` out of `.env`.** `EnvironmentFile=` overrides `Environment=`, so a leftover line would point the service back at the root-only key path, and the preflight would fail.
- **The service user can read the credentials directory, and so can the child.** Landlock refuses it to the child (§ Containment).
- **The Status key** is `/etc/processor/status-checkin.key`. Each unit that checks in loads it as `LoadCredential=status-checkin-key:…`:
  - `processor-drift.service`, which reads no `.env`, so the drift check never holds the broker credential (MONITORING.md § The drift check);
  - `processor.service`, since #39, for the liveness check-in (MONITORING.md § Liveness). Landlock refuses it to the child, as it refuses the GCS key;
  - `processor-patching.service`, since #113, which reads no `.env` either (DEPLOYMENT.md § The patch-due check).

A command that needs the service's settings runs as the service, through systemd. Don't load the file into a shell:

```bash
processor_cli() {   # e.g. processor_cli dlq list; processor_cli ensure-group
  sudo systemd-run --quiet --pipe --wait --collect -p User=processor -p Group=processor \
    -p EnvironmentFile=/etc/processor/.env -p WorkingDirectory=/srv/processor/live \
    /srv/processor/live/.venv/bin/processor "$@"
}
```

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
