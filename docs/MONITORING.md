# Monitoring

What checks in to Status, and the host's resources: the drift check, liveness, the disk GC and its headroom sensor, memory. Day-to-day commands: [DEPLOYMENT.md](DEPLOYMENT.md#operate).

## At a glance

| Thing | Value |
|---|---|
| Drift check | `processor-drift.timer`, hourly: does live lag `origin/main` in code that runs (`processor.drift.RUNTIME_*`)? Checks in to Status's `co-processor-drift` (`http://status:9000`, status#24); `alert` past 8 h. [§ below](#the-drift-check) (#35) |
| Liveness | `processor run` checks in `ok` to Status's `co-processor-live` every 5 min, only while the consume loop progressed within 605 s; silence past the 900 s grace pages. Daemon thread, 10 s bound: Status never touches consumption. [§ below](#liveness) (#39) |
| Disk GC | Provisioner's GC and headroom sensor (`maintaining-host-disk`): a daily prune of tooling under `/home/exedev`, and a check-in to Status's `co-processor-disk-headroom` every 10 min (85% / 80%). Config [deploy/disk-gc/disk-gc.conf](../deploy/disk-gc/disk-gc.conf); patch runs read its line as a floor. [§ below](#disk-gc) (#82) |

## The drift check

**Live lagging `origin/main` in code that runs alerts through Status** (#35, ported from status#12). `processor-drift.timer` starts `processor-drift.service` 5 min after boot, then hourly. It runs `/srv/processor/live/.venv/bin/processor drift` as `processor`: it asks GitHub how far live's `REVISION` is behind `main`, then checks in once to Status's monitor `co-processor-drift` (tenant `co-processor`, CannObserv/status#24) at `http://status:9000`. The rules are in `src/processor/drift.py`.

| Live | Check-in (`kind`) | What to do |
|---|---|---|
| `main`; behind only in what never runs; behind in code for 8 h or less since the push that brought it | `ok` (`ok`) | nothing |
| behind in code for more than 8 h | `alert` (`lag`), every hour until deployed. It names `main`'s CI result | `scripts/deploy.sh`. If `main`'s CI isn't green, DEPLOYMENT.md § The CI gate refuses: fix `main` first |
| not on `main`: diverged, or GitHub doesn't know live's commit (a rewritten `main`) | `alert` (`off_main`) | deploy a commit on `main`; `journalctl -t processor-deploy` says how live got there |
| unstamped: the release has no `REVISION` | `alert` (`unstamped`) | `live` names something that isn't a finished release: deploy |
| GitHub can't answer: a rate limit, a 5xx, a timeout, a wrong-shaped answer | none; the run exits 1 | nothing. The monitor's grace absorbs one missed run |

- **Each `alert` names its kind as `metadata.fault`** (#58; status#28, the contract of `co-archiver-drift` and `co-broker-drift`); an `ok` sends no `metadata`. Status groups a run of alerts into one fault by it, so a change of kind (`lag` → `off_main`, say) reports at once, not at the next daily reminder.
- **What runs:** `src/`, `deploy/`, `scripts/deploy.sh`, `pyproject.toml`, `uv.lock`. A rename counts by either name. Docs, tests, CI, skills and the other scripts never count, however old.
- **The check is itself runtime:** it lives in `src/` and its units in `deploy/`. A change to it needs a deploy, and lags like any other.
- **The clock** starts at the push, from its CI run's `created_at`, never the commit date. Within the grace, the body names the oldest push since live, which may be docs only.
- **GitHub's budget:** unauthenticated, 60 requests an hour per address, shared with DEPLOYMENT.md § The CI gate. A run costs 1 request in sync or behind in docs only, 2 behind in code, and at most 10 past the grace.
- **Apart from `processor run`:** a oneshot with `TimeoutStartSec=90`, no `Restart=`, and no retry. No GitHub call starts past 60 s, but each is bounded per read, not in total, so the 90 s is the hard stop. A run killed there sends nothing, which reads as a GitHub-silent run. A GitHub or Status outage costs it its exit code and nothing else. It reads no `.env`, so it never holds the broker credential.

**`missing` on `co-processor-drift` means the check isn't reporting.** The cause is one of: the timer is stopped, live was rolled back past #35 (DEPLOYMENT.md § Rollback), `co-processor` is down, the tailnet path to `status` is gone, the key is wrong or missing, Status refuses, or GitHub stayed silent for two runs in a row. It says nothing about `processor run` (that is `co-processor-live`, § Liveness). To find out which:

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
- **The key** is the drift check's, the same file (PROVISIONING.md § `/etc/processor/`). If it's missing, the service still starts. `starting` then says `liveness: off: missing the status-checkin-key credential`, an ERROR record `liveness check-in off` follows, and the monitor goes `missing`.
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

## Disk GC

Provisioner's disk GC and headroom sensor (provisioner#61), **VERSION 3** (#99), installed and turned on through gregoryfoster/skills `maintaining-host-disk` ([skills/maintaining-host-disk](../skills/maintaining-host-disk/SKILL.md), #82). VERSION 3 changed only the `vscode-servers` tier: it keeps every `Stable-*` build newer than the newest live one, and the newest when none is live, by `server/product.json`'s version ([editor-builds.md](../skills/maintaining-host-disk/references/editor-builds.md)). Its first run, 2026-10-31 05:44Z, exited 0: it removed 135.3M in 2 items (a 135.1M `vsix-cache` entry and the npm cache), kept 6 (among them `Stable-2c33b50b`, 1.144.0, newer than the live 1.140.0), and left `/` at 57%. The skill's [turning-on.md](../skills/maintaining-host-disk/references/turning-on.md) is the procedure; this section is what's particular to this host. The GC's tiers and rules are Provisioner's. A change to them is raised there, through the operator, and never patched into the installed copy: the installer refuses a copy that doesn't match its MANIFEST.

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

**With DEPLOYMENT.md § Patching:**
- **patching-hosts reads `[sensor] mount` and `alert_at_percent` as a floor** (gregoryfoster/skills#390). It refuses a step whose download and install would take `/` to 85%, and stops on a config the GC would refuse.
- **The GC's daily run (05:15–05:45) falls inside the Tuesday window.** It never touches apt's trees, and it yields: `Nice=10`, idle I/O.
- **After a reboot,** if the GC runs before VS Code reconnects, it finds no build running and keeps the newest (VERSION 3, #99). Under VERSION 2 it kept none, and the next session re-downloaded its build (about 640M).
- **A run the reboot cuts off** finishes the next day. A missed day pages only as `gc-stale`, after 72 h.

**Read it** without sudo: `exedev` is in `adm`, so `journalctl -u cannobserv-disk-headroom -p err --since -1d` reads the alerts. verifying.md's `sudo` is for users outside `adm`, for whom the same command prints `-- No entries --`. Its rule holds here too: only `checked in ok (ok): HTTP 202` proves a check-in.

**Updates and removal** are the skill's ([SKILL.md](../skills/maintaining-host-disk/SKILL.md)). A new release arrives as a pin bump in the daily skills-vendor refresh.

## Memory: no earlyoom

**earlyoom is not installed** (operator, 2026-10-30, #83). The kernel's own OOM killer is the only one on this host, and it already ranks agent sessions ahead of the service. That is broker's reasoning (CannObserv/broker#58, broker#71): sessions sit at adj 0 once they're off exe.dev's -1000, and what earlyoom would add is acting at its `-m` floor instead of at exhaustion. The cohort's other two positions:
- **co-status runs it** (`-m 12,6`, `--avoid` the service, `--prefer` the sessions; copied from notifier). Its reason: with no swap, the kernel's killer acts late.
- **broker installs it and keeps it disabled**, with its config current, so enabling it is one command.

**The figures** `[measured 2026-10-30 on release d29e240cb85f, unless cited]`:

| What | Reading |
|---|---|
| Host | 7935 MiB, no swap; `available` 6373–6685 MiB; `/proc/pressure/memory` all zeros |
| `processor` | `MemoryPeak` 105M, `MemoryCurrent` 69M (both since the 15:58:47Z start); `MemoryMax=6G`, `OOMScoreAdjust=-500` |
| A patch run (#77) | the dry run peaked at 265 MB max RSS, the held steps at about 190 MB; no OOM |
| The disk GC (#82) | the report-only run peaked at 26 MB max RSS. A finished oneshot keeps no `MemoryPeak` on systemd 255, so the real prunes have no figure |
| The kernel log | no `oom-kill`, `Out of memory` or `page allocation failure` since 2026-09-29: the whole retention, 5 boots |
| The 2026-10-22 balloon event | co-processor took the same balloon (`Out of puff` ×26, NETWORK.md § Tailnet flaps) with no earlyoom: no OOM kill and no allocation failure. status (7.9 GiB, as sized in its `earlyoom.default`) and notifier (3.8 GiB), both swapless, run earlyoom, and it killed a user process on each at its floor |

**The kernel's order** (`oom_score`, 2026-10-30):
- first `exedev`'s `systemd --user` (734, adj 100) and its `(sd-pam)`, about 11 MiB between them;
- then the sessions at adj 0: `claude`, `MainThread` (VS Code's node) and `npm exec socrat` (SocratiCode), 672–691;
- `processor` (adj -500) at 342, tailscaled at -950 ([NETWORK.md § tailscaled's OOM rank](NETWORK.md#tailscaleds-oom-rank));
- only `exe-init` and `sshd` at -1000.

An extraction child volunteers at 1000 while it runs ([deploy/processor.service](../deploy/processor.service)'s `OOMPolicy=continue` comment), so it goes before all of them. The -500 is worth half the host's pages, so the service would pass a session only with about 3.9 GiB more resident than that session. `MemoryMax=6G` allows that; the measured peak is 105M.

**Re-open the decision** (install as broker does, disabled, or as co-status does) on any of these:
- `available` under 1 GiB at any reading;
- an `oom-kill` or a `page allocation failure` in the kernel log. Watch for the second as well: co-status's `deploy/earlyoom.default` records that on broker nothing was OOM-killed, while atomic allocations failed in unrelated processes;
- a session back at -1000 (an exe-init regression, as broker#58). Broker pins that with a node-only test, and nothing here does, so the check below reads it.

```bash
free -m; cat /proc/pressure/memory
journalctl -k --since -30d | grep -ciE 'oom-kill|out of memory|page allocation failure'   # 0
ps -eo oomadj=,comm= | grep -E 'claude|MainThread|npm exec' | sort | uniq -c             # every line 0
```

**Not changed here:**
- the unit's `OOMScoreAdjust` and `MemoryMax`;
- `MemoryLow=` ([docs/SOCRATICODE.md](SOCRATICODE.md#repo-specific-notes));
- `vm.min_free_kbytes`, 11399 here. Broker raised its own to 64 MiB for atomic allocations (broker#21).
