---
title: Host patching, posture scheduled, first window (#77)
date: 2026-10-26
status: per #77's brief, the coordinator's 2026-10-25 comment, the review comment and the operator's decisions (2026-10-26)
---

# Host patching, posture scheduled, first window

## Problem

co-processor has the lock-down half of the cohort's `scheduled` posture (the apt timers are masked, by the image) but has never had a window. 85 packages are pending, 60 of them `-security`, including `libssl3t64`, `openssh-server` and `systemd` (measured 2026-10-25). needrestart is unset, so any apt run without `NEEDRESTART_MODE=l` may restart `processor` and `tailscaled` itself. The vendored `patching-hosts` isn't linked and there's no knob.

## Approach (as broker#67 / #65, status#54)

- **Links:** `skills/` and `.claude/skills/` for `patching-hosts`, and `reviewing-code` (#76's CR 11, operator yes). docs/SKILLS.md: 13 → 15.
- **needrestart:** `deploy/needrestart.conf.d/processor.conf`, `$nrconf{restart} = 'l'`, one key. `tests/test_needrestart.py` is broker's ported: a Perl eval of the drop-in and of the live chain. The installed and live tests skip off `co-processor` by **hostname**, never by the file, and fail on the node when it's absent (broker#79/#81).
- **Tailscale's origin:** `deploy/apt/tailscale.pref` (`*` at 100, `tailscale tailscale-archive-keyring` at 600). Pin tests plus a live `AutoUpdate.Apply is False`, in `tests/test_tailscaled.py`.
- `scripts/deploy.sh` `HOST_CONFIGS` and `tests/test_units.py` gain both files.
- **Knob** `.skills/patching-hosts`:
  - `class production`, `posture scheduled`, `window Mon 23:00-05:00`;
  - `service processor.service`;
  - `inflight pgrep … processor\._child`;
  - two non-root `health` lines;
  - `origin` follow for NodeSource (by its site) and Tailscale, with `hold tailscale tailscale`;
  - `owner /srv/processor/live self`, `records CannObserv/processor`;
  - no `datastore` (the scratch redis) and no `quiet`.

  `tests/test_patching_knob.py` runs the skill's own `read-knob.sh`, plus pure parses.
- **Docs:**
  - DEPLOYMENT.md gets a new top-level § Patching: the posture, the knob, the host files, the install, the probe's self-escalation, this host's run, the restart-without-reboot rule, liveness and the record.
  - Node's subsection becomes § Patching Node, without the stale "can't name the origin" reason.
  - AGENTS.md gets an Infrastructure row and a Detail Docs entry.

## Tradeoffs

- **Hostname, not file, for the node:** the installed and live tests fail on `co-processor` until the operator installs the files. So the pre-ship gate on this VM is red until host steps 1–3 run. That's the brief's order (the drop-in goes in before any apt run), so it's accepted. CI runs off the node, where those tests skip.
- **`window Mon` cannot express "not the broker's Monday".** The gate allows any Monday. The operator kept the Monday (the coordinator's recommendation) over `Tue`, which the gate would have enforced, so it's a convention: each next date is written on the issue.
- **No `datastore` for the scratch redis:** no recovery point. Accepted, since it holds test data only (0 keys measured).
- **The docs-inventory test** still doesn't catch a vendored skill that's neither linked nor listed. A test that did would turn `main` red at each upstream skill the daily hook pulls in unreviewed. Not added.

## Steps

1. Links and SKILLS.md.
2. Host files and tests (red first).
3. Knob and test (red first, on the missing link).
4. Docs.
5. Review (`reviewing-code-python-fastapi`), then fixes, then report. **Pause before shipping.**
6. Post-merge host steps: on #77, each sudo step an approval gate. The first window is tonight, 2026-10-26 23:00Z.

## Open questions

None left for the build. The next window's date is chosen at the end of the run.
