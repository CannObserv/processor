# Deployment

Processor runs as one systemd unit, `processor`, on exe.dev VM `co-processor`. Code on `main` is the deployed code. Design: [the spec](specs/2026-09-29-processor-service-design.md) §2 (identities, grants), §4 (failure handling), §6 (cutover).

## Prerequisites (operator)

| What | Where | Tracked |
|---|---|---|
| Broker ACL user `processor` | `co-broker` | broker#75 |
| Broker credential | `/etc/processor/.env` as `CO_PROCESSOR_BUS_URL` | broker#75 item 5 |
| Tailnet `tag:processor` → `tag:broker` on 6379 | tailnet policy | done 2026-09-29 |
| A direct tailnet path to the broker (still relayed via DERP `sea` on 2026-09-30, although this node advertises endpoints and its netcheck is clean) | tailnet / broker side | broker#75 finding |
| Bucket `gs://co-gcs-processor`, UBLA, public access prevention, no lifecycle | GCP | spec §2 |
| SA `co-gcs-processor-writer`: `objectCreator` + `objectViewer` on `co-gcs-processor` (**no delete**), `objectViewer` on `co-gcs-blobs` | GCP | spec §2 |
| SA key at `/etc/processor/co-gcs-processor-writer.json` (600) | this VM | — |
| Watcher's SA: `objectViewer` on `co-gcs-processor`, bucket level | GCP | watcher#325 |

### Broker credential handoff (hash-only, broker#75 as of 2026-09-30)

The broker node never holds `processor`'s plaintext; since broker#72 its hourly backup refuses a plaintext line. This follows the shape of archiver#251:

1. **On `co-processor`,** mint the password into `/etc/processor/.env` and the password manager. It stays out of argv and shell history, because `printf` is a shell builtin:

   ```bash
   pw="$(set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40)"; [ "${#pw}" -eq 40 ]
   ( umask 077; printf 'CO_PROCESSOR_BUS_URL=redis://processor:%s@100.97.91.19:6379/0\n' "$pw" >> /etc/processor/.env )
   printf %s "$pw" | sha256sum | cut -d' ' -f1   # post THIS digest on broker#75; it is not the credential
   # copy "$pw" into the password manager, then:
   unset pw
   ```
2. **On the broker,** once the digest is posted, the broker's owner runs `ACL SETUSER processor on "#<digest>" …`, `ACL DELUSER observo` and `ACL SAVE`, and records the digest line.
3. **From `co-processor`,** `PING` as `processor`. This is the only verification possible. The password travels in `REDISCLI_AUTH`, never argv:

   ```bash
   REDISCLI_AUTH="$(sed -n 's|^CO_PROCESSOR_BUS_URL=redis://processor:\([^@]*\)@.*|\1|p' /etc/processor/.env)" \
     redis-cli -h 100.97.91.19 --user processor PING   # PONG
   ```
4. Then create the group right away (below).

Use `100.97.91.19`, not `broker`. This VM runs Tailscale with `--accept-dns=false`, so MagicDNS does not resolve.

## `/etc/processor/.env`

The directory is 700 and the file 600, both owned by `exedev`. The unit loads the file. Never source it into a login shell.

```bash
CO_PROCESSOR_BUS_URL=redis://processor:<password>@100.97.91.19:6379/0
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

## Node.js (agent tooling only)

Node 24 LTS from NodeSource's apt repo, for `using-mayfly-chat` and SocratiCode (`npx`). The `processor` unit never runs it. The cohort survey and the reasons for NodeSource and for 24 are in #6.

```bash
sudo bash deploy/nodesource.sh install   # key, source, pin, nodejs; ends with check
bash deploy/nodesource.sh check          # exit 0 in sync; 3, naming each drift
```

| Installed | From | Why |
|---|---|---|
| `/etc/apt/keyrings/nodesource.gpg` | `https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key`, refused unless its fingerprint is `6F71 F525 2828 41EE DAF8 51B4 2F59 B5F9 9B1B E0B4` | `Signed-By:` binds the key to this one source |
| `/etc/apt/sources.list.d/nodesource.sources` | [deploy/apt/nodesource.sources](../deploy/apt/nodesource.sources) | `node_24.x`: the major line is pinned |
| `/etc/apt/preferences.d/nodesource.pref` | [deploy/apt/nodesource.pref](../deploy/apt/nodesource.pref) | `nodejs` from NodeSource at 600: above universe's 18 (500), never a downgrade |

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

An out-of-cycle fix (a Node security release): `sudo NEEDRESTART_MODE=l choom -n 0 -- apt-get install nodejs` takes this one package, at adj 0 as run.md applies.

### Moving the major line

Edit `node_<N>.x` in `deploy/apt/nodesource.sources` and `EXPECTED_MAJOR` in `tests/test_nodesource.py` in one commit, then `sudo bash deploy/nodesource.sh install`. Read `init-socraticode`'s preflight first: Node 26 crashes SocratiCode older than 1.13.

### Removing it

```bash
sudo apt-get purge nodejs
sudo rm /etc/apt/keyrings/nodesource.gpg /etc/apt/sources.list.d/nodesource.sources /etc/apt/preferences.d/nodesource.pref
sudo apt-get update
```
