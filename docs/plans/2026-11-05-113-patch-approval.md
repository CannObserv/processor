---
title: Patching — `automatic` for -security (A), a Status "patch due" check (B) (#113)
date: 2026-11-05
status: approved by the operator 2026-11-05 (timer times and alert-at-open as recommended) — per #113 (body; the coordinator's brief of 2026-11-03 and its addendum of 2026-11-05; the implementer's corrections, posted on #113 before building)
---

# Patching: `automatic` for -security, and a "patch due" check

## Problem

The first window under #77's knob (Tue 2026-11-03 05:00–10:00Z) passed with no run and nothing noticed: `posture scheduled` needs the owner's approval, and nothing asks for it. A missed window defers `-security` by a week. Operator decision, 2026-11-03: both (A) `posture automatic`, so `-security` lands daily by itself, and (B) a Status check-in that asks for the attended run when its window opens and pages again if it closes without one. Target: B live before the **Tue 2026-11-10** window.

## Approach

Two PRs, A then B, each through `shipping-work` (`merge_mode pr`, FF push from the main checkout).

**A — host config and docs.** The skill's `automatic` (policy.md § automatic) is: the apt timers on, `APT::Periodic::Enable "1"` effective, unattended-upgrades on `-security` only, no automatic reboot, needrestart `l`. A probe of this host against a scratch `posture automatic` copy of the knob (non-root, 2026-11-05) gives exactly four findings: `timer:apt-daily.timer`, `timer:apt-daily-upgrade.timer`, `periodic:Enable` (set to 0 by `docker-disable-periodic-update:1`), `uu:automatic-reboot` (set nowhere). The origins are already the release pocket, `-security` and the two ESM security pockets; needrestart `l` is in (#77). A's tracked host files, compared by `scripts/deploy.sh` (`HOST_CONFIGS`) and installed by hand like `needrestart.conf.d/processor.conf`:

| Tracked | Installed | What |
|---|---|---|
| `deploy/apt/apt.conf.d/processor-periodic` | `/etc/apt/apt.conf.d/` | `APT::Periodic::Enable "1"` (+ restates `Update-Package-Lists`, `Unattended-Upgrade` "1"), `Unattended-Upgrade::Automatic-Reboot "false"`. Its name sorts after `docker-disable-periodic-update`; a `99…` name sorts before `d` and loses |
| `deploy/apt-daily-upgrade.timer.d/processor-window.conf` | `/etc/systemd/system/apt-daily-upgrade.timer.d/` | `OnCalendar=` reset, then `*-*-* 13:00`, `RandomizedDelaySec=60m`. **Required, not optional:** `apply.sh`'s `gate_apt` refuses any step whose span meets this timer's range, and the stock range, 06:00–07:00 daily, sits inside our window |
| `deploy/apt-daily.timer.d/processor-window.conf` | `/etc/systemd/system/apt-daily.timer.d/` | `*-*-* 12:00`, `RandomizedDelaySec=30m`. The stock 06,18:00 + 12 h covers every hour; its `apt update` can meet the bulk's own list refresh. `apply.sh` doesn't read this timer, so it's ours to keep clear |

Then, by hand: `systemctl unmask` + `enable --now` both timers. `unattended-upgrades.service` (the shutdown hook) stays masked: the probe reads nothing of it. No apt timer has a stamp under `/var/lib/systemd/timers/`, so `Persistent=true` fires nothing at enable. The knob reads `posture automatic`; the maintenance lane (`-updates`, the followed origins, Tailscale's held step, any reboot) stays the attended weekly run in `Tue 05:00–10:00`. `apply.sh` doesn't branch on posture.

**B — `processor patching`, hourly, to `co-processor-patching`.** Drift's shape (#35/#58): a pure verdict, a stateless check, one check-in per tick, `metadata.fault` = kind.
- **The window:** the release's own knob, `/srv/processor/live/.skills/patching-hosts`. `read-knob.sh` isn't in a release (`git archive` leaves the submodule empty; measured), and the unit can't read the deploy checkout (`ProtectHome`, a user with no home). So `patching.py` parses the weekday `window` lines (`Tue`, `Sun#2`, wraps past midnight). A dated line opens no due period: it's a one-off. A `[host` header, or a window line it can't read, gives kind `knob`. A CI test holds its parse equal to `read-knob.sh`'s resolved weekday `window` on the committed knob and on fixtures. `.skills/patching-hosts` joins `RUNTIME_FILES`: a knob change is now a deploy, and drift alerts if it isn't deployed.
- **The signal:** the record, the one artifact every run leaves, including a run with nothing pending. That's an issue comment in this repo (`records CannObserv/processor`, public, so the read is unauthenticated). It counts when it was created at or after the latest window open `W` ≤ now, its first line matches `## Patch run record: co-processor`, and its `author_association` is `OWNER` or `MEMBER`. Anyone can comment on a public repo, so without that filter a stranger could silence the alert. ~~The 2026-11-03 record is `MEMBER`.~~ **Corrected 2026-11-06:** that read was authenticated. Anonymously, which is how the check reads, the record is `CONTRIBUTOR`, because the author's CannObserv membership was private. Operator's decision: make the membership public; no code change. The read is `GET issues/comments?since=W&sort=created&direction=desc&per_page=100`, up to 3 pages. That's 20 comments since 11-01, so one request per tick, inside the 60/h shared with drift and the deploy gate.
- **Kinds:** `ok` (a record since `W`; the body links it); `due` (the window is open, with no record); `missed` (the window closed with no record, until the next `W`); `knob`; `test` (`--test-alert`); `silent` (GitHub didn't answer: no check-in). `due` pages at window open. The change to `missed` opens a new fault, reported at once. Status's grouping gives daily reminders after that.
- **Units:** `processor-patching.{service,timer}` copy drift's: the same key by `LoadCredential=`, `User=processor`, `TimeoutStartSec=90`. `OnCalendar=*-*-* *:00:30`, so the first `due` comes within a minute of an on-the-hour window. `scripts/deploy.sh` installs and enables it as it does drift's.
- **Monitor** (cross-repo, CannObserv/status, approval needed): `interval_seconds 3600`, `grace_seconds 5400`, `renotify_seconds 86400`. The check runs every tick, so the interval is the tick's, not the brief's ~7 d. Created `enabled: false` if a monitor can go `missing` before its first check-in; enabled on our first `ok`.

## Tradeoffs / alternatives

- **The session checks in `ok` after posting the record** — rejected: the key is root-600, so each run would add a sudo step, and it depends on the session remembering. The record already exists.
- **`/var/backups/patching-hosts-*` as the signal** — rejected: a run with nothing pending makes no run directory.
- **Ship `read-knob.sh` into the release** (`deploy.sh` archives the submodule at the gitlink) — rejected for now: it couples every deploy to an initialised, fetched submodule. The parity test catches drift at CI instead. Revisit if the parse grows past weekday windows.
- **Read the knob from GitHub `main`** — rejected: one more request per tick, and it reads a different copy from the one the skill and the release read.
- **One long-lived "patch records" issue** — not needed: the repo-level comment read finds a record on any issue (#113 holds 11-03's).
- **Report "a restart is due" in B** — deferred: the service user can't read `processor`'s maps (it's undumpable) or run needrestart. The weekly run's step 6 picks it up (a known limit: up to a week on an old libssl).

## Steps

1. **Corrections** posted on #113 (done before building). Plan reviewed.
2. **A, red → green:**
   - `tests/test_patching_knob.py`: posture `automatic`; the timer drop-ins' ranges are disjoint from every weekday window `read-knob.sh` resolves; the apt.conf.d name sorts after `docker-disable-periodic-update`.
   - `tests/test_units.py` / `test_deploy.py`: each new file is in `HOST_CONFIGS`.
   - Installed-file tests on the node, as `test_needrestart.py`'s.
   - Then the files, the knob, and the docs: § Patching (posture, host files, install, step 6 keys restarts on needrestart's list and not the run's set, the stale "no cadence directive" claim), AGENTS.md's Patching row (within budget).
3. **A, review and ship:** code review, then fix or record each finding; report the final SHA; PR; FF merge.
4. **A, on the host (each sudo step approved in the thread):**
   - install the three files, `daemon-reload`, unmask and enable the timers;
   - `deploy_command` in the background (it touches `deploy/`), then read `journalctl -t processor-deploy` and the drift check;
   - `probe.sh --refresh-into` (sudo): 0 findings;
   - read the first unattended run (`journalctl -u apt-daily-upgrade`, `/var/log/unattended-upgrades/`).
   - Record on #113, and the deploy's first use on #115.
5. **B, Status request** drafted and shown to the operator; posted only on approval. The monitor id goes into the unit.
6. **B, red → green:** `src/processor/patching.py` (window parse, `W`, record match, verdict); `processor patching [--test-alert]`; the units; settings `patching_monitor_id`; tests mirroring `test_drift.py` and `test_units.py`; the parity test against `read-knob.sh`; `RUNTIME_FILES` + its docs (AGENTS.md, DEPLOYMENT.md § Deploy a change).
7. **B, review and ship:** code review, fix or record, final SHA, PR, FF merge. Then `deploy_command` (approved): the timer is installed and enabled, the verdict line is read, and the first check-in is `ok` (11-03's record). `--test-alert` is read in Status.
8. **The 2026-11-10 window:** `due` at about 05:00:30Z, then the run, then a record, then `ok` at the next tick. The record goes on #113. The closing comment ends with `Follow-up:`, `Known limits:` and `For #N:` lines. Never close #84.

## Open questions / risks

- **Timer times** (13:00 ± 60 m upgrade, 12:00 ± 30 m lists, UTC daily; 05:00–06:00 Pacific): clear of our window and the broker's. Operator: confirm, or pick others.
- **Lead time:** alert at window open (21:00 PST), or *N* hours before? Recommend at open; a lead is one constant.
- **Unattended `-security` restarts by maintainer scripts** (the profile's table: `redis-server` scratch here; `tailscale` isn't in u-u's origins) now land outside any window. `processor` itself isn't restarted (needrestart `l`).
- **If B isn't live by 11-10:** the fallback is the coordinator's 6-hourly check plus a one-off session reminder. A alone already lands `-security`.
- **Status:** whether a new monitor goes `missing` before its first check-in decides `enabled: false` (asked in the draft).
