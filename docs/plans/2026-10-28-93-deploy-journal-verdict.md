---
title: deploy.sh journals the verified verdict (#93)
date: 2026-10-28
status: per #93's body, the coordinator's brief and the implementer's corrections (2026-10-28)
---

# deploy.sh journals the verified verdict

## Problem

`scripts/deploy.sh` journals the switch (`live -> <build>`) but not the verdict: `live is on <build>` is a `note`, stderr only. `journalctl -t processor-deploy` can't tell "switched and verified" from "switched, then killed before the verify" (measured: the 2026-10-25, -26 and -27 deploys end at `live -> …`).

## Approach

**`say` (broker#122 item 1, broker `4336c92`):** `note "$*"` plus `logger -t processor-deploy "$*" || true`. A journal error never fails a verified deploy.

- The success line names both proofs: `live is on <build>: pid <N> logged starting (child_containment required), then consuming; the smoke run passed`. `started_on` sets a global `verified_pid` on success (its `pid` is local).
- The host-config notes (`compare_host_configs`) and `note_units_not_in_release` go through `say`: what a deploy left on the host outlives its terminal (#82's note).
- The existing `logger` lines stay as written: their journal text differs from their notes, and check-ins read it.

**The interrupt trap (broker `d52d292`), its own commit:** `trap interrupted INT TERM HUP`, and a `stage`:

| stage | on a signal |
|---|---|
| before the switch | exit 1, `interrupted before the switch; nothing switched`; nothing journaled (nothing changed) |
| while switching · after the switch, installing units and verifying · while switching back · after switching back, verifying the old build | journal `interrupted on <build> <stage>: live -> <link now>, unverified`; exit 4 |
| after the verify (set before the verdict line, CR 1; then the new timers, the drift notes, the prune; broker's "while pruning" starts later) | journal `interrupted after the verify; live is on <build>, verified`, logger first; exit 0. While enabling a new timer it names `sudo systemctl enable --now <timer>` (CR 2) |

The EXIT trap still removes the backup. The units it replaced are in the old release's `deploy/`, so deploying that build again reinstalls them. The units it added stay installed: the next deploy names them (journaled) and never removes them (CR 4).

**Docs:** DEPLOYMENT.md § Deploy a change names the journaled lines, the interrupt line, and exit codes.

## Tests (red first)

- A verified deploy journals `live is on <build>: pid 1 logged starting …; the smoke run passed`, after `live -> <build>`.
- A failed verify journals no `live is on`.
- A host config not installed, or differing, is journaled.
- `logger` failing leaves a verified deploy exit 0.
- Trap: a signal (TERM, INT, HUP) during the restart after the switch journals `interrupted on <build> after the switch, …: live -> releases/<build>, unverified`, exit 4, and no `live is on`. A signal before the switch exits 1 with no `interrupted` journal line. A signal during the switch back's restart names that stage. A signal after the verify (a host config's `cmp`) exits 0, verified.

The signal comes from the `systemctl`/`getent` stubs (`kill -<sig> $PPID`): both run from the script's own shell, not a subshell.

## Ship

PR `Refs #93` → FF merge → `git pull --ff-only` → `scripts/deploy.sh` (touches `scripts/deploy.sh`) → `journalctl -t processor-deploy -n 5` shows `live is on <build>: pid <N> …`, its PID the unit's `MainPID`.

## Accepted limits

- SIGKILL can't be trapped: a `kill -9` still leaves only `live -> <build>`. The verdict line's absence then means "unverified".
- A signal to bash alone (not its process group) runs the trap once the foreground child ends: up to `TimeoutStopSec=240` in a restart. Ctrl-C and a group kill end the child at once.
- Refusals before the switch (`die`: CI, build, user) stay stderr only: nothing changed on the host.
