# Deployment

Processor runs as one systemd unit, `processor`, on exe.dev VM `co-processor`. Code on `main` is the deployed code. Design: [the spec](specs/2026-09-29-processor-service-design.md) §2 (identities, grants), §4 (failure handling), §6 (cutover).

## Prerequisites (operator)

| What | Where | Tracked |
|---|---|---|
| Broker ACL user `processor` | `co-broker` | broker#75 |
| Broker credential | `/etc/processor/.env` as `CO_PROCESSOR_BUS_URL` | broker#75 item 5 |
| Tailnet `tag:processor` → `tag:broker` on 6379 | tailnet policy | done 2026-09-29 |
| Bucket `gs://co-gcs-processor`, UBLA, public access prevention, no lifecycle | GCP | spec §2 |
| SA `co-gcs-processor-writer`: `objectCreator` + `objectViewer` on `co-gcs-processor` (**no delete**), `objectViewer` on `co-gcs-blobs` | GCP | spec §2 |
| SA key at `/etc/processor/co-gcs-processor-writer.json` (600) | this VM | — |
| Watcher's SA: `objectViewer` on `co-gcs-processor`, bucket level | GCP | watcher#325 |

### Broker credential handoff (broker#75's order)

The plaintext comes from `/etc/redis/broker-acl-passwords` on the broker node. The order matters:

1. **Verify from here, before anything is written.** The password travels in `REDISCLI_AUTH`, never argv or a URL:

   ```bash
   read -rs REDISCLI_AUTH && export REDISCLI_AUTH
   redis-cli -h 100.97.91.19 --user processor PING   # PONG
   ```
2. Write `/etc/processor/.env` (below).
3. Only then does the broker replace the plaintext line with its digest.

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
- **Dead letters:** `.venv/bin/processor dlq list | show <id> | drop <id>` (needs the env file loaded). There is no replay: every entry either failed to decode or was not a command.
- **Lag / missing group:** the broker probe watches `processor.process` (broker#75).
- **Strikes** are counted in memory. A restart resets them, so a poison command gets at most 3 more attempts.

## Bumping co-core

co-core is pinned `==` in lockstep with Watcher (spec §5). A bump changes `processor_version` on every fact, so it is planned with Watcher:

1. Rebuild the wheelhouse at the new tag, then update the pin in `pyproject.toml` and `EXPECTED` in `tests/test_pin.py`, and `uv lock`.
2. Regenerate the goldens from Watcher on the new version (`scripts/gen_parity_goldens.py`). They must not change, or the bump note says why output moved.
3. Deploy together with Watcher's bump, or neither moves.
