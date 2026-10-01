# Deployment

Processor runs as one systemd unit, `processor`, on exe.dev VM `co-processor`. Code on `main` is the deployed code. Design: [the spec](specs/2026-09-29-processor-service-design.md) §2 (identities, grants), §4 (failure handling), §6 (cutover).

## Prerequisites (operator)

| What | Where | Tracked |
|---|---|---|
| Broker ACL user `processor` | `co-broker` | broker#75; live 2026-10-01 (`observo` deleted) |
| Broker credential | `/etc/processor/.env` as `CO_PROCESSOR_BUS_URL` | broker#75 item 5; minted 2026-10-01, `PING broker` as `processor` → `PONG` 22:09:36Z |
| Tailnet `tag:processor` → `tag:broker` on 6379 | tailnet policy | done 2026-09-29 |
| `processor.process` on `content.process` (the hard ordering) | broker | done 2026-10-01 22:09:41Z by `processor ensure-group`: stream empty, group at `0-0`, lag 0 |
| Tailscale `--accept-dns=true`, and tailscaled's OOM drop-in | this VM | done 2026-10-01, [Tailscale DNS](#tailscale-dns) (#8) |
| A direct tailnet path to the broker (still relayed via DERP `sea` on 2026-09-30, although this node advertises endpoints and its netcheck is clean) | tailnet / broker side | broker#75 finding |
| Bucket `gs://co-gcs-processor`, UBLA, public access prevention, no lifecycle | GCP | spec §2, [GCP provisioning](#gcp-provisioning) |
| SA `co-gcs-processor-writer`: `objectCreator` + `objectViewer` on `co-gcs-processor` (**no delete**), `objectViewer` on `co-gcs-blobs` | GCP | spec §2, [GCP provisioning](#gcp-provisioning) |
| SA key at `/etc/processor/co-gcs-processor-writer.json` (600) | this VM | [GCP provisioning](#gcp-provisioning) |
| Watcher's reader: `objectViewer` on `co-gcs-processor`, bucket level. Proposed for `co-gcs-blob-reader`, the identity Watcher already reads `gs://` blobs with | GCP | watcher#325 (unconfirmed) |

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
2. **On the broker,** once the digest is posted, the broker's owner runs `ACL SETUSER processor on "#<digest>" …`, `ACL DELUSER observo` and `ACL SAVE`, and records the digest line.
3. **From `co-processor`,** `PING` as `processor`. This is the only verification possible. The password travels in `REDISCLI_AUTH`, never argv:

   ```bash
   REDISCLI_AUTH="$(sed -n 's|^CO_PROCESSOR_BUS_URL=redis://processor:\([^@]*\)@.*|\1|p' /etc/processor/.env)" \
     redis-cli -h broker --user processor PING   # PONG
   ```
4. Then create the group right away (below).

The URL names the broker, never its address: see [Tailscale DNS](#tailscale-dns).

### GCP provisioning

This is a draft for the operator to run where `gcloud` is authenticated to project `co-gcs`; nothing in this repo runs it. It mirrors the cohort's other buckets (Replicator's `docs/INFRASTRUCTURE.md`): `US-WEST1`, `STANDARD`, uniform bucket-level access, and public access prevented. It sets no lifecycle rule, since the output is never deleted (spec D4), and keeps GCS's default 7-day soft delete, as on `co-gcs-replicator`. The writer holds no delete permission, so soft delete only guards against an admin's mistake.

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
#    is derived from blobs that identity can read. Run once watcher#325 agrees.
gcloud storage buckets add-iam-policy-binding gs://co-gcs-processor \
  --member="serviceAccount:co-gcs-blob-reader@${PROJECT}.iam.gserviceaccount.com" \
  --role=roles/storage.objectViewer

# 5. The key, straight onto co-processor over SSH stdin, never left on disk here.
(
  set -e; dir="$(mktemp -d)"; trap 'rm -rf "$dir"' EXIT   # mktemp -d is 700
  gcloud iam service-accounts keys create "$dir/key.json" --iam-account="$SA"
  ssh co-processor.exe.xyz \
    'umask 077; cat > /etc/processor/co-gcs-processor-writer.json' < "$dir/key.json"
)
```

Then, **on `co-processor`**:

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

## `/etc/processor/.env`

The directory is 700 and the file 600, both owned by `exedev`. The unit loads the file. Never source it into a login shell.

```bash
CO_PROCESSOR_BUS_URL=redis://processor:<password>@broker:6379/0
GOOGLE_APPLICATION_CREDENTIALS=/etc/processor/co-gcs-processor-writer.json
# Optional overrides; defaults in src/processor/settings.py:
# CO_PROCESSOR_EXTRACTION_TIMEOUT_S=120
# CO_PROCESSOR_RLIMIT_AS_BYTES=3221225472
# CO_PROCESSOR_RECLAIM_MIN_IDLE_MS=600000    # must exceed timeout + 60 s
# CO_PROCESSOR_RECLAIM_INTERVAL_S=60
# CO_PROCESSOR_MAX_ATTEMPTS=3
# CO_PROCESSOR_CONSUMER_NAME=co-processor
```

## Build

```bash
cd ~/processor
git pull --ff-only
# Wheelhouse (AGENTS.md): sync_wheelhouse.py with a co-pypi-reader key, or:
set -a; . ./.env; set +a; scripts/build_wheelhouse.sh
uv sync --frozen --no-dev
```

## Create the group — as soon as the credential verifies

`processor.process` must exist before Watcher's first command (spec §6, the hard ordering). A group created later from `$` skips earlier entries. On 2026-09-30 broker#75 found `content.process` absent, so creating the group now skips nothing. This is idempotent:

```bash
set -a; . /etc/processor/.env; set +a        # an operator shell, closed afterwards
.venv/bin/processor ensure-group
```

## Install and start

```bash
sudo cp deploy/processor.service /etc/systemd/system/processor.service
sudo systemctl daemon-reload
sudo systemctl enable --now processor
journalctl -u processor -f      # "starting", then "consuming"; one JSON record per command
```

On boot, the service preflights both buckets and exits non-zero if either is unreachable, and systemd restarts it. Broker outages do not stop the service: the loop backs off from 1 s up to 30 s and retries.

## Deploy a change

```bash
git pull --ff-only && uv sync --frozen --no-dev && sudo systemctl restart processor
```

A restart lets the in-flight command finish (`KillMode=mixed`: SIGTERM reaches the consumer, not its extraction child; `TimeoutStopSec=240`, which must grow with `CO_PROCESSOR_EXTRACTION_TIMEOUT_S`). A stop mid-reclaim finishes the command in hand and leaves the rest pending. A command killed mid-flight stays pending, and the reclaim re-runs it after `reclaim_min_idle_ms`. That includes a stop during a GCS outage: the library's retries can push one command to about 435 s, past `TimeoutStopSec`, so systemd SIGKILLs it. This is safe (nothing was acked) and deliberate (a deploy never hangs for minutes).

## Operate

- **Logs:** `journalctl -u processor`. Each outcome has `command_id`, `info_source_id`, `action` (`ack` / `strike` / `leave_pending` / `dead_letter`), `reason`, `detail`, and timings in `*_ms`.
- **Dead letters:** `.venv/bin/processor dlq list | show <id> | drop <id>` (needs the env file loaded). There is no replay. An entry failed to decode, was not a command, or is a command that raised outside the handler on every attempt (`reason` starts `gave up on attempt`): that one is a bug to fix, and Watcher's reaper re-issues the command under a fresh id.
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
