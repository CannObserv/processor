# Patching

Keeping `co-processor`'s packages current: the posture and its two lanes, the weekly attended run, the patch-due check, and Node.js from NodeSource (agent tooling only). Deploying and operating: [DEPLOYMENT.md](DEPLOYMENT.md).

## At a glance

| Thing | Value |
|---|---|
| Patching | `automatic` (knob `.skills/patching-hosts`, #113): `-security` daily by unattended-upgrades (13:00 UTC, no restart, no reboot); the rest in one approved run a week, `Tue 05:00–10:00` UTC (Monday night Pacific), as the broker's window closes; `processor-patching.timer` asks for it through Status. Never a bare `apt-get upgrade`. [§ below](#patching) (#77) |
| Node.js | 24 LTS from NodeSource apt, agent tooling only (mayfly, SocratiCode): `deploy/nodesource.sh install\|check`, [§ below](#nodejs-agent-tooling-only) |

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
| `health` ×2 | The unit is active, and nothing at `-p warning` in 6 min ([§ Operate → Logs](DEPLOYMENT.md#operate); valid since #76) |
| `origin deb.nodesource.com follow` | Node, agent tooling ([§ Patching Node](#patching-node)) |
| `origin pkgs.tailscale.com follow`, `hold tailscale tailscale` | Its postinst restarts tailscaled, the only path to the broker, so it is a held step approved on its own |
| `owner /srv/processor/live self` | The release has no git origin; its `uv.lock` is this repo's ([§ The lock audit](DEPENDENCIES.md#the-lock-audit)) |
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

The first unattended run reads in `journalctl -u apt-daily-upgrade` and `/var/log/unattended-upgrades/unattended-upgrades.log` (root). No apt timer had a stamp under `/var/lib/systemd/timers/` when they were enabled (measured 2026-11-05), so `Persistent=true` had no missed run to catch up: enabling fired nothing, and the first run was the next 13:00. Both have stamps since their first runs (2026-11-10), so a timer disabled and enabled again now catches up a run it missed. **To go back to `scheduled`:** `sudo systemctl disable --now` and `mask` both timers, remove `processor-periodic`, and set the knob's `posture`.

**The probe escalates by itself.** `probe.sh` runs its root readings through `sudo -n` whenever that works, without asking. So every run, including `--dry-run-into` and `--post-boot`, is a sudo step and needs the owner's approval. For a non-root read, put a `sudo` that exits 1 first on `PATH`; the root readings then land in its `not_read`.

**This host's run** (each sudo step approved in this repo's session; `<run>` = `/var/backups/patching-hosts-<UTC>`). Run the scripts under `skills/patching-hosts/scripts/` from the deploy checkout's root, pulled: each defaults its knob to `.skills/patching-hosts` at `git rev-parse --show-toplevel`, or else the working directory, and with no knob found it falls back to `automatic`, report-only.

1. `sudo apt-get update`. The maintenance dry run reads the host's lists, which `apt-daily.timer` last refreshed between 12:00 and 12:30 UTC the day before (gregoryfoster/skills#467).
2. `probe.sh --lane maintenance --dry-run-into <dir>`, in the background: 0 findings expected, beside a `lists:stale` risk that reads the security archive's own publish date, not when the lists were refreshed (2026-10-26). Read the `redis-server` candidate; it must still be a `7.0.15` build, not Valkey, until #54.
3. The needrestart proof, as above.
4. `apply.sh --approve --step bulk --lane maintenance --dry-run <dir>`, in the background, within 24 h of the dry run. It restarts nothing. With no data store there is no recovery point: `recovery-point.sh` refuses ("nothing to dump"), and the bulk records the before-versions, the rollback path, in the `<run>` it creates and prints.
5. Held steps, each approved, each `apply.sh --approve --step <name> --run <run>`:
   - `redis`: the scratch server restarts;
   - `docker`: `containerd`. Docker and containerd are disabled and inactive here, so nothing is running to restart;
   - `tailscale`, if pending: a 2.3 s tailnet blip, sockets survive ([§ Tailnet flaps](NETWORK.md#tailnet-flaps), "A tailscaled restart here").
6. The reboot and restart decision, from the host, never from this run's set: under `automatic` a `-security` library may have landed any day since the last run. `sudo needrestart -b -r l` and `sudo grep -c '(deleted)' /proc/1/maps`. The kernel never decides one on exe.dev. Expect a reboot when `systemd` or `libsystemd0` is in the set: after those families, needrestart listed dbus and logind on co-broker and co-status, and PID 1 mapped deleted files.
7. The reboot: `reboot-chain.sh --run <run>` prints the chain; `--approve` launches it. It stops `processor` (up to `TimeoutStopSec=240`), then reboots in-guest. **Never `exe.dev restart`**: that is a hard reset (#83).
8. After it: `probe.sh --post-boot --run <run>`, NETWORK.md § Tailscale DNS → *Check after a reboot*, `NRestarts=0`, and drift and liveness `ok` in Status. The disk sensor's first tick after boot logs `checked in ok (ok): HTTP 202` within 10 min ([§ Disk GC](MONITORING.md#disk-gc)). Read `journalctl -u processor -p warning --since <window start>` raw (systemd's own lines, such as a failed stop) and through `-o cat | jq -cR 'fromjson? | …'`. A crash is `PRIORITY=6`, so check `NRestarts` too.
9. The after-probe, last, after the reboot or the no-reboot restart below: `probe.sh --refresh-into <dir>`. It is what's left pending, by class, for the record, and the only step that audits the live `uv.lock` (`pending.language`). The dry run and `--post-boot` never ask OSV ([§ The lock audit](DEPENDENCIES.md#the-lock-audit)).

**With no reboot,** restart the service anyway when needrestart lists it, or after a libssl update by either lane: `/usr/bin/python3.12`'s `_ssl` links the system `libssl.so.3`, so the old library stays mapped until a restart. `sudo grep -c 'libssl.so.3 (deleted)' /proc/$(systemctl show -p MainPID --value processor)/maps` must read 0 afterwards.

**Liveness's grace** is 900 s. The heartbeat ticks as soon as the service starts, so the silence is at most the time since the last tick (300 s), plus the stop (240 s), plus the boot. That stays inside the grace, so Status needs a warning only if a step stalls.

**Deploys don't wait for a window** (operator, 2026-10-26, #77). The skill's policy.md says a deploy that restarts a declared `service` takes one (gregoryfoster/skills#366). Here `scripts/deploy.sh` runs after each merge, as before: it restarts `processor` alone, verifies the new build and switches back on failure ([§ Deploy a change](DEPLOYMENT.md#deploy-a-change-scriptsdeploysh)). A merge that lands during a window is deployed after the post-boot checks, so a flap stays attributable.

**The record** goes on the issue (`records CannObserv/processor`), per run.md §7, and ends with the next window's date. **Its first line is `## Patch run record: co-processor`**, then anything (a date, the lane), posted by an `OWNER` or a `MEMBER` whose membership is public (§ The patch-due check): that line is what [§ The patch-due check](#the-patch-due-check) reads as this week's run, a nothing-pending run's included. A run that can't take its window still posts one, saying why, or the monitor stays `missed` until the next. What each run leaves pending, by class, comes from step 9 and includes:
- **Ubuntu Pro / ESM:** Pro isn't attached, and 25 esm-apps updates were visible on 2026-10-26.
- **Language:** `/srv/processor/live/uv.lock`, its OSV advisory count, owner `self`. Run.md §7 asks for the owning repo's update mechanism; here that is `audit.yml`, report-only ([§ The lock audit](DEPENDENCIES.md#the-lock-audit)). Dependabot's version updates cover Actions only, never `uv.lock`, and its security updates, resolving through the co-pypi index since #121, are landed by hand ([§ CI pins and Dependabot](DEPENDENCIES.md#ci-pins-and-dependabot)). A count above 0 goes to that section's "Where a finding goes" and is linked from the record. A tree listed as not audited, or the class in `not_read`, is recorded as no read, never as 0.

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

Read a run with `journalctl -u processor-patching -n 3 -o cat`: one `patching check` record, its `kind`, `window`, `record` and `checkin` (Status's 202). A deliberate alert (`kind: test`, fault `test`, asking GitHub nothing) proves the channels, as the drift check's does ([§ The drift check](MONITORING.md#the-drift-check)):

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

**An out-of-cycle fix** (a Node security release): `sudo bash deploy/nodesource.sh install`. It refreshes the package lists first, which `apt-daily.timer` does only daily, between 12:00 and 12:30 UTC, so a bare `apt-get install nodejs` can answer "already the newest version" while the release sits unfetched. It then takes the newest `nodejs` candidate, at adj 0 with needrestart listing only, and ends with `check`. `check` alone reads the lists as of the last update.

### Moving the major line

In one commit, edit `node_<N>.x` in `deploy/apt/nodesource.sources`, `EXPECTED_MAJOR` in `tests/test_nodesource.py`, this section's "Node <N> LTS" and table row, and AGENTS.md's Node.js row; the test fails until all of them agree. Then `sudo bash deploy/nodesource.sh install`. Read `init-socraticode`'s preflight first: Node 26 crashes SocratiCode older than 1.13.

### Removing it

```bash
sudo apt-get purge nodejs
sudo rm /etc/apt/keyrings/nodesource.gpg /etc/apt/sources.list.d/nodesource.sources /etc/apt/preferences.d/nodesource.pref
sudo apt-get update
```
