---
title: Contain the extraction child (#2)
date: 2026-10-06
status: draft for gate 2 (the operator approves the deploy and install changes before building); per #2 and its hand-off from epic #24 (2026-10-05); Layer 3 follows the cohort deploy standard (broker#22, status#9 R1–R13), revised 2026-10-06
---

# Contain the extraction child

## Problem

The child (`python -I -m processor._child`) parses untrusted documents. Today it runs as `exedev`, who has passwordless sudo and is in group `docker`. A parser bug that gives it code execution can read `/etc/processor/.env` (the broker credential), the GCS key, the repo's `.env` (GH tokens) and `/proc/<parent>/environ`, and it can open any connection, the broker included. #2 contains it in one PR with three layers, plus seccomp (decided 2026-10-05).

## Measured on `co-processor`, 2026-10-06 (spikes in this session; nothing committed)

- Landlock ABI **6**, x86_64, `io_uring_disabled` = 0, systemd 255. No user `processor` exists.
- **`docker.socket` is disabled and inactive** (gate 1 done). `/run/docker.sock` is still there (`root:docker 0660`), but nothing listens on it.
- **A Landlock spike through `ctypes`** (syscalls 444–446) restricted the process to read and execute on: each `sys.path` entry, the stdlib, `/usr/lib/x86_64-linux-gnu`, `/etc/ld.so.cache` and `/etc/mime.types`. It also handled TCP bind and connect, and scoped abstract unix sockets and signals. Results:
  - **Denied:** `/etc/processor/.env`, the repo's `.env`, `/proc/<ppid>/environ`, a TCP connect to `127.0.0.1:6379` (EACCES), and `kill(ppid, 0)` (EPERM).
  - **Trap 2 confirmed:** `connect()` to `/run/tailscale/tailscaled.sock` **succeeded**. Landlock on 6.12 doesn't cover connecting to a pathname socket.
  - **Imports:** co-core reads `/etc/mime.types` when it is imported, so without that file the import is refused. That was the only file outside the derived set that the corpus needed.
  - **Parity:** all 22 inputs (`cases.json` plus the 9 real ones) gave identical digests with and without the restriction.
- **With a classic-BPF seccomp filter added** (it refuses `socket`, `socketpair` and `io_uring_*` with EPERM, and kills on any other arch), every socket attempt failed with EPERM: tailscaled, docker, abstract and TCP. Parity across the 22 inputs was again identical.

## Layer 1: the child contains itself (Landlock and seccomp)

Add a new module, `src/processor/_contain.py`. It is stdlib only (`ctypes`, `os`, `sys`, `sysconfig`, `mimetypes`), so `_child` keeps its rule of importing only the stdlib at module level. It adds no dependency and doesn't touch the wheelhouse.

- `landlock_abi() -> int`: `landlock_create_ruleset(NULL, 0, VERSION)`, or 0 if the kernel has no Landlock.
- `read_allowlist(sys_path) -> list[str]`, **derived, never hard-coded**:
  - each existing, non-empty `sys.path` entry;
  - `sysconfig` `stdlib`;
  - `LIBDIR/MULTIARCH`, the shared libraries;
  - `/etc/ld.so.cache`;
  - each existing entry of `mimetypes.knownfiles`.

  It refuses any entry that is `/`, or that is `/etc`, `/home`, `/run`, `/proc`, `/root` or an ancestor of one of them, so a stray `''` or `/` can't widen the set to everything. In dev the set contains `src/` but **never the repo root**, which holds `.env`.
- `contain()`. In order:
  1. `PR_SET_NO_NEW_PRIVS`.
  2. Landlock:
     - Every filesystem right the ABI knows is handled. Each allowlisted entry gets read and execute; a regular file gets read only.
     - TCP bind and connect are handled, with no port allowed.
     - Abstract unix sockets and signals are scoped.
     - `landlock_restrict_self`.
  3. Seccomp:
     - On an arch other than the native one: KILL.
     - On an x32 syscall number: EPERM.
     - `socket`, `socketpair`, `io_uring_setup`, `io_uring_enter` and `io_uring_register`: EPERM. `io_uring` would otherwise open sockets without going through seccomp.
     - The syscall table covers x86_64 and aarch64. On any other arch, `contain()` raises.

  Writes are refused everywhere. `/proc` is not allowed (the `oom_score_adj` write happens before this). Running a program is refused too, since `/usr/bin` isn't on the list.
- **Modes** (`Containment = Literal["required", "off"]`):
  - `required` raises `ContainmentUnavailable` when the ABI is below 6, the arch is unsupported, or any syscall fails.
  - `off` does nothing.

  There is no "best effort" mode. A partial sandbox that reports success is the vacuous pass that #2 warns about.

`_child.main`, in this order:
1. SIGINT ignored;
2. `oom_score_adj`;
3. `RLIMIT_AS`;
4. `sys.path` prepended;
5. the channel moved;
6. **`contain(mode)`**;
7. `pickle.load`;
8. the transform imported;
9. the transform run.

`ctypes` and libc load at `_contain`'s import, before the restriction. The new argv is `<mode> <rlimit_as> [sys_path …]`. Under `required`, a failure to contain goes to stderr and the child exits with status 70, which the parent counts as a crash (a strike). The boot check below makes this a backstop that should never fire.

## Layer 2: a non-dumpable parent

`processor run` calls `prctl(PR_SET_DUMPABLE, 0)` (through `_contain`) first thing in `_run`. That makes `/proc/<parent>/environ` and ptrace off-limits to every same-uid process. The child's `execve` resets its own dumpable flag, and its environment holds only `PATH` and `LANG`. `ensure-group` and `dlq` don't call it.

## Settings, wiring and the boot check

- `Settings.child_containment: Literal["required", "off"] = "required"` (`CO_PROCESSOR_CHILD_CONTAINMENT`). Production sets nothing.
- `run_in_child(..., containment=…)` is a **required** keyword, so every caller says which mode it runs. `Deps.containment` carries it, and `handler` passes it on.
- **Failing closed at boot.** Under `required`, `_run` exits 1 before the store preflight if `landlock_abi() < 6` or the arch is unsupported. It logs `"child containment unavailable"` with the ABI. systemd then restarts it and the unit visibly flaps. That beats every command striking three times and dead-lettering. The `starting` record gains `child_containment` and `landlock_abi`.

## Layer 3: a dedicated user, and releases that follow the cohort's deploy standard (trap 1)

**The deploy model changes; gate 2 approves this section.**

**The standard followed.** broker#22 is the cohort design: production runs a deploy of a pushed commit, never the dev tree. Its first shipped pilot is CannObserv/status#9: spec `docs/specs/2026-09-30-deploy-releases-design.md`, decisions R1–R13, `scripts/deploy.sh` with 59 tests, and `docs/DEPLOYMENT.md`. Status then added:
- the CI gate, #11;
- a drift check, #12;
- unit installs on every deploy, #18.

Provisioner#23 plans its own deploy on this repo's `DEPLOYMENT.md`, so processor matches Status's names and contract rather than inventing a third shape. Where processor deviates, the table says why. The deviations are feedback for the cohort skill (§ Feedback for the cohort deploy skill).

| Status | Processor | Why it differs |
|---|---|---|
| R1: every unit runs an immutable release, never a checkout | same | |
| R2: `/srv/status/releases/<build>/`, `live` and `dev` links, owned by `exedev`, `chmod -R a-w` | `/srv/processor/releases/<build>/` and `live`, owned by `exedev`, `a-w`, **`o+rX`** | No `dev` target (R12). The release must be readable by `processor`. **With a dedicated service user, exedev-owned read-only releases are a real boundary.** `processor` is not the owner, so it can't `chmod` them back. That is goal 5 without status#14's root ownership, and the build needs no sudo. |
| R3: `git archive <sha>` after `git fetch`; live must be an ancestor of `origin/main` | same | |
| R4: build in place; `uv sync --locked --no-dev --compile-bytecode`; `REVISION` last; a directory without it is rebuilt; a linked release is never rebuilt | same, plus `--no-editable`, `--python /usr/bin/python3.12` and `UV_PYTHON_DOWNLOADS=never`, plus the wheelhouse copied in | **Non-editable:** `sys.path` (and so the child's Landlock allowlist) then holds only the stdlib and `site-packages`. **The system interpreter:** `processor` can't read anything under `/home` (`/home/exedev` is 0750, and the unit sets `ProtectHome=yes`), so a uv-managed Python there would not start. **The wheelhouse:** co-core is private, and `find-links = ["./.wheelhouse"]` is relative (broker#22 Q5). |
| R5: units run `uv run --frozen --no-sync` | `ExecStart=/srv/processor/live/.venv/bin/processor run` | The venv's entry point never syncs either, and it needs no uv and no uv cache for a user with no home. A test holds that no unit runs `uv`. |
| R6: per target: migrate, switch, install units, restart, verify, switch back on failure | switch, install units, `reset-failed`, restart, verify, switch back on failure | There is no database, and no HTTP endpoint (see verify, below). |
| R7–R9: expand-only migrations, the schema check, skip the migration when the database is ahead | n/a | No database. |
| R10: build id from `REVISION` | the `starting` record carries `build`, which is `REVISION`, or `dev` when there is none | Only a long-running process, so its start record is the one place to report it. |
| R11: env from `/etc/<svc>/` only | same, plus `LoadCredential=` for the key | See secrets, below. |
| R12: dev is deployed too | **no dev target** | Processor has no dev service. The rehearsal is the smoke run on the scratch bus, which is part of verify. |
| R13: runs as `exedev`; sudo only for systemctl and unit files; `flock`; keeps 5 releases plus the linked ones | same, plus sudo for the smoke's `systemd-run` | |
| #11 CI gate, #12 drift check | **deferred to a follow-up issue** | They are separable, and Status deferred them from #9 the same way. Processor is public, so the gate would need no token. |
| #18: units installed from the release; host configs compared, never installed | same: `processor.service` is installed; `deploy/tailscaled.service.d/*` is compared and warns | |

**The pieces:**

- **User:** `useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin processor`. It gets no sudo and is not in group `docker`.
- **`scripts/deploy.sh [<ref>]`** (new). It runs as `exedev` from any checkout. Status's interface and names apply: `<ref>` defaults to `origin/main`, a rollback is `scripts/deploy.sh <old build>`, and `journalctl -t processor-deploy` records each switch. Variables: `PROCESSOR_DEPLOY_ROOT=/srv/processor`, `PROCESSOR_DEPLOY_ETC=/etc`, `PROCESSOR_DEPLOY_KEEP=5` and `PROCESSOR_DEPLOY_VERIFY_SECONDS=300`.
  1. Refuse to run as root, or while another deploy holds the lock (`flock`).
  2. `git fetch --prune origin`. Resolve `<ref>`, and require it to be an ancestor of `origin/main`.
  3. Build `releases/<build>`, `<build>` being the 12-character short SHA, unless a complete one exists whose venv imports `processor`:
     - `git archive`;
     - copy in `.wheelhouse`;
     - `uv sync` (R4, above);
     - write `REVISION`;
     - `chmod -R a-w,o+rX`.
  4. Record the old `live`, then switch it: `ln -s` to a temporary name, then `mv -T`.
  5. Install `deploy/processor.service` from the release if it differs from the installed copy: `sudo install -m 644`, then `daemon-reload`. Compare the host configs and warn.
  6. `sudo systemctl reset-failed processor`, then `sudo systemctl restart processor`. The restart waits for the in-flight command, up to `TimeoutStopSec=240`.
  7. **Verify:**
     - within the verify budget, the new `MainPID` logs `starting` with `build` equal to `<build>` and `child_containment: required`, then `consuming`;
     - then the **smoke**, run through `systemd-run --pipe --wait`:
       - as `processor`;
       - with the unit's `EnvironmentFile=`, `LoadCredential=`, `ProtectHome=` and `NoNewPrivileges=`;
       - running `scripts/smoke_scratch_bus.py` from the release: the scratch bus, and the production bucket, write-if-absent.

     So every deploy runs the contained child under the real identity.
  8. **On failure:** switch back, restore the replaced unit and `daemon-reload`, `reset-failed`, restart, and prove the old build the same way.
     - Exit 1 when the old build answers.
     - Exit 4 when nothing answers. That includes a first deploy, which has nothing to switch back to.
  9. Prune old releases (R13).
- **Secrets:**
  - `/etc/processor` becomes `root:root 700`, and `.env` and the key `root 600`.
  - systemd reads `EnvironmentFile=` before it switches users, so the service user never reads the file.
  - The key arrives through `LoadCredential=gcs-writer-key:/etc/processor/co-gcs-processor-writer.json` and `Environment=GOOGLE_APPLICATION_CREDENTIALS=%d/gcs-writer-key`.
  - The `GOOGLE_APPLICATION_CREDENTIALS=` line **is removed from `.env`**, because `EnvironmentFile=` overrides `Environment=`. Settings doesn't read the variable (`extra="ignore"`); google-auth reads the path unchanged.
  - Under `/run/credentials/processor.service/` the key is readable by the service's uid, and so by the child's. Landlock denies the child that path.
- **Unit:**
  - `User=processor`, `Group=processor`;
  - `WorkingDirectory=/srv/processor/live`;
  - `ExecStart=/srv/processor/live/.venv/bin/processor run`. The venv's scripts carry the release's physical path, so a later switch can't mix releases in a running process or its children;
  - `LoadCredential=` and `Environment=` as above;
  - adds `ProtectHome=yes` and `ProtectSystem=strict`. Nothing in the service writes outside `PrivateTmp`, and bytecode is compiled at build time. The rest of #5's hardening stays out of scope.
- **What the change does to the docs' invariants:**
  - "Code on `main` is the deployed code" becomes: **what runs is `/srv/processor/live/REVISION`, a commit on `origin/main`, put there by `scripts/deploy.sh`**.
  - `~/processor` stays the operator's clone, and the skills hook still pushes from it. `git pull --ff-only` after a merge then serves the hook, not the deploy, and nothing needs `.skills/worktree_venv=none` any more (skills#345 notes the same).
- **Packaging:** set `.skills/deploy_command` to `scripts/deploy.sh` if gregoryfoster/skills#345 lands first. Until then, AGENTS.md says that shipping means `scripts/deploy.sh`, as Status's does.
- **The epic's verify recipe, new step 3:**

  ```bash
  git -C ~/processor diff --quiet "$(cat /srv/processor/live/REVISION)" origin/main -- src deploy scripts/deploy.sh pyproject.toml uv.lock && echo deployed
  ```

  A docs-only merge then needs no deploy, as today.

## Rollout and rollback (gate 3)

The rollout runs in two stages, so each risk is taken on its own during shadow (a failed command stays pending, and Watcher doesn't act on it).

- **Stage A (after the FF merge; the existing deploy path, the old unit):** `git pull --ff-only && uv sync --frozen --no-dev && sudo systemctl restart processor`.
  - Layers 1 and 2 go live while the service still runs as `exedev` from `~/processor`. In that layout `src/` is on the allowlist and the repo root is not.
  - Check: `starting` shows `child_containment: required` and `landlock_abi: 6`, and the smoke test passes. Then wait for the next shadow command: it must be `ack: complete` with #26's digests.
  - **Rollback A:** add `CO_PROCESSOR_CHILD_CONTAINMENT=off` to the env file and restart. No code revert is needed.
- **Stage B (the operator's install):**
  1. Back up `/etc/systemd/system/processor.service` and `/etc/processor/` to `/var/backups/processor/2/`.
  2. `useradd`.
  3. `sudo install -d -o exedev -g exedev -m 755 /srv/processor`.
  4. `chown`, `chmod` and the `sed` that drops `GOOGLE_APPLICATION_CREDENTIALS` from `.env`.
  5. `scripts/deploy.sh`. It builds the first release, installs the new unit, restarts and verifies. A first deploy has nothing to switch back to, so a failure exits 4 and Rollback B applies.
  6. Live checks:
     - the main PID's user is `processor`;
     - `/proc/<pid>/status` shows `NoNewPrivs: 1`, and `sudo -u processor cat /proc/<pid>/environ` is refused (the parent is undumpable);
     - `sudo -u processor cat /etc/processor/.env` is refused;
     - `systemctl is-enabled docker.socket` says `disabled`.
  7. The next shadow command is `ack: complete` with its digests, and Watcher still reports a match.

  **Rollback B:** restore the backed-up unit and `/etc/processor` (ownership and the removed line), `daemon-reload`, then restart. That returns to Stage A: `exedev` and `~/processor`. `/srv/processor` and the user can stay.

## Tests (TDD, red first)

The containment tests carry `skipif(landlock_abi() < 6, reason="Landlock ABI N < 6: child containment untested here")`. `conftest.py` adds a `pytest_report_header` that prints the mode the suite runs: `required (Landlock ABI 6)`, or `off (…): containment tests skipped`. CI's log then says which mode it ran in. Every other child test runs under the conftest's `CHILD_CONTAINMENT`, which is `required` when the ABI allows it and `off` otherwise. **On `co-processor` a skip isn't allowed**: a host test in the style of `test_tailscaled` asserts ABI ≥ 6 there.

Every denial test has a positive control: the same target under `off` succeeds. That proves the file exists and is readable, or the listener is up, so a pass can't be vacuous. Denials assert the errno (EACCES or EPERM), not just any `OSError`.

`tests/test_contain.py`:
- the ABI probe;
- the allowlist derivation: stdlib, `sys.path`, the libdir, `ld.so.cache` and `mime.types` are included; the repo root is excluded; `/` and ancestors of `/etc` or `/home` are refused;
- the BPF program for each arch, and an unknown arch raises;
- `required` with ABI 5 raises `ContainmentUnavailable`, through an injected ABI;
- **layer independence**, each in a subprocess:
  - Landlock alone denies a TCP connect, but a pathname unix connect gets through. That pins trap 2 as the reason for the seccomp filter, and the test would fail if a future kernel closed the gap, prompting a doc update;
  - seccomp alone denies `socket()`;
- `make_undumpable`: a same-uid sibling can't read the subprocess's `/proc/<pid>/environ`; the control without the call can.

`tests/test_child.py`, under `required`. The child can't:
- open a `tmp_path` file (standing in for the env file, the key and a home file), or `/proc/<ppid>/environ`;
- open the real `/etc/processor/.env`, the GCS key and the repo's `.env`, each when present (all three exist and are readable by `exedev` on this VM today);
- open a file under `Path.home()`;
- TCP-connect to a listener the test opens, or to `127.0.0.1:6379`;
- reach a unix socket the test binds, `/run/docker.sock` or `/run/tailscale/tailscaled.sock`, each when present;
- signal its parent, run `/bin/sh`, or write a file.

**And the whole corpus, `cases.json` plus `real/`, gives results identical to the in-process `extract`** (today the child test covers `cases.json` only).

`tests/test_main.py`:
- `_run` makes the process undumpable before the preflight;
- under `required` with the ABI faked to 0, it exits 1 and logs `child containment unavailable`;
- `starting` carries `child_containment`, `landlock_abi` and `build` (from `REVISION`, or `dev`).

`tests/test_settings.py`: the default is `required`, and `off` parses.

`tests/test_units.py` (the repo's flat layout), following Status's `test_release_units.py`:
- no unit names `/home`, and none runs `uv`;
- `User=processor`;
- `WorkingDirectory=` and `ExecStart=` resolve into `/srv/processor/live`;
- `LoadCredential=` together with `GOOGLE_APPLICATION_CREDENTIALS=%d/…`;
- `ProtectHome=yes` and `ProtectSystem=strict`;
- every file under `deploy/` is either a unit or a known host config.

`tests/test_deploy.py`, following Status's `test_deploy.py`. It runs `scripts/deploy.sh` end to end against a throwaway root and a temporary origin, with stub `uv`, `sudo`, `systemctl`, `systemd-run`, `journalctl` and `logger` on `PATH`. It covers:
- refusing root, a held lock, and a ref that is not on `origin/main`;
- `REVISION` written last, and an interrupted build rebuilt;
- a linked release never rebuilt;
- `a-w,o+rX` after a build;
- the atomic switch;
- the unit installed only when it differs;
- the verify failure paths: no `starting` with the build, and a failed smoke, each switching back (exit 1);
- a first deploy with nothing to switch back to (exit 4);
- retention.

## Steps

1. This plan → gate 2.
2. `_contain` and its tests.
3. Child wiring, settings, `Deps`, the boot check, dumpable and `build`; the child and main tests; the corpus through the contained child.
4. The unit, `scripts/deploy.sh` and their tests.
5. Docs:
   - spec §3: the "Known limitation" paragraph becomes **Containment**, covering the layers, what stays reachable, and the limits;
   - DEPLOYMENT, restructured on Status's: releases, `scripts/deploy.sh`, rollback, units, the user and secrets, plus a **Containment** check list;
   - AGENTS.md: the child paragraph ("not a sandbox" goes); the Service and VM rows; "Code on `main` is the deployed code"; the checkout notes; "shipping means `scripts/deploy.sh`";
   - the `child.py` and `_child.py` docstrings.
6. Full suite, ruff, PR, CR → FF merge → Stage A → Stage B (gate 3) → the closing comment, with each box's evidence and one live record.
7. File the follow-ups: the CI gate and the drift check (Status's #11 and #12, for processor).

## Known limits (for the closing comment)

- The child still shares the kernel: every syscall except the socket family is reachable.
- It can read the code it runs, the stdlib and the shared libraries.
- It can spend CPU up to the timeout and memory up to `RLIMIT_AS`.
- It decides its own command's output. That can't be avoided; Watcher's shadow comparison is the check on it.
- It shares the service's uid. Landlock (filesystem, signal scope, ptrace) and the parent's non-dumpable flag carry that, not uid separation.
- `exedev` owns the releases and has sudo. As in Status's R2, that boundary is between the service and its code, not between the operator and production.

## Amended in build and review

Gate 2 approved the text above. Where the build or the review on PR #33 changed it, the code and the docs follow this list, not the text above.

- **The library directories come from the loader, not from `LIBDIR`** (4f7a6ec). They are the directories of the shared objects already mapped. On CI's setup-python, `LIBDIR` names the toolcache, so `libgcc_s` (beside libc) was refused and the extractors failed to import.
- **Read only, no execute** (CR 2, dea7c52). Layer 1's "read and execute" granted `execve` on the allowlist, the dynamic loader included. Loading a library doesn't need it.
- **A boot canary** (CR 1, f216757). One real extraction through the contained child runs before any command. The ABI check alone misses a child that fails for another reason.
- **Single-threaded only** (CR 4, 65182fd). `contain()` refuses a process with a second thread, which would escape both layers.
- **The deploy's verify** reads the new MainPID's records from this boot since the restart (CR 5). A failed first deploy restarts the service on the unit it restored (CR 3).
- **The key's live check** (CR 9). After the install the key is `/run/credentials/processor.service/gcs-writer-key`, readable by the service's uid through an ACL. DEPLOYMENT § Containment pairs a control (readable) with the contained read (denied).
- **Tests are flat:** `tests/test_units.py` and `tests/test_deploy.py`, the repo's layout.

## Feedback for the cohort deploy skill

These points are gathered for the upstream skills issue that Provisioner#23 will file to share the release structure. Each is what processor needed beyond Status's R1–R13.

1. **A dedicated service user settles status#14 without root.** Releases owned by `exedev` and `a-w` are a real boundary once the unit runs as its own user, since that user can't `chmod` them back. The skill should make "dedicated service user + exedev-owned read-only releases" the default answer to goal 5, and require `o+rX` on the release and a traversable root.
2. **The interpreter lives outside `/home`.** A user with no home, or `ProtectHome=yes`, can't run a uv-managed Python under `~/.local/share/uv`. On exe.dev `/home/exedev` is 0750 as well. The build should pin `--python /usr/bin/python3.X` with `UV_PYTHON_DOWNLOADS=never`, and a test should check that `pyvenv.cfg`'s `home` isn't under `/home`.
3. **Launcher:** treat the venv's entry point as equal to `uv run --frozen --no-sync`. `uv run` needs uv and a writable cache for the service user. The invariant worth testing is "no unit syncs" (no unit runs `uv sync`, or `uv run` without `--no-sync`), not the exact launcher.
4. **Private wheels** (broker#22 Q5): the build needs a hook to put the project's private index or wheelhouse into the release before `uv sync`. Processor copies `.wheelhouse`, because its `find-links` is relative.
5. **Non-editable releases:** worth considering as the default. An editable install puts `<release>/src` on `sys.path`; anything that derives a sandbox or allowlist from `sys.path` then has to reason about the repo layout.
6. **Verify is a per-repo hook.** `/ready` and `/health` assume an HTTP API. A bus consumer verifies by its start record (`build`, then "consuming" from the new `MainPID`) plus a smoke command. The script's skeleton (resolve, build, switch, units, verify, switch back, exits 1 and 4) is shared; the verify, and any pre-switch step such as a migration, are per repo.
7. **`dev` is optional.** R12 assumes a dev service. A service without one names its rehearsal instead: for processor, the scratch-bus smoke.
8. **Restart budget:** a unit with `KillMode=mixed` and a long `TimeoutStopSec` (processor: 240 s, so an in-flight command can finish) blocks `systemctl restart` for up to that long. The verify budget has to be derived from the unit, not fixed at 60 s.
9. **Secrets by credential, not by path in the env file.** Use `LoadCredential=` plus `Environment=…=%d/<name>` for key files, with `/etc/<svc>` root-owned 700: systemd reads `EnvironmentFile=` before the user switch. **Trap:** `EnvironmentFile=` overrides `Environment=`, so a migration must delete the old path line from the env file. Provisioner's reader key and PAT fit this shape.
10. **Hardening the layout unlocks:** once no unit reads `/home` and bytecode is compiled at build, `ProtectHome=yes` and `ProtectSystem=strict` cost nothing. They belong in the unit template.
11. **Ship the script, don't copy it.** Status's `deploy.sh` is 644 lines and 59 tests, and five CR rounds (CR 1–36) taught it about exit codes, joining a oneshot mid-pass, `reset-failed`, never rebuilding a linked release, and restoring links exactly. A copy per repo relearns each of those lessons. The skill should vendor the skeleton, with per-repo hooks: pre-switch, verify, the unit-to-target mapping and the host configs.
12. **Variable names:** fix a convention. Status uses `STATUS_DEPLOY_*`; processor's settings use `CO_PROCESSOR_*`, and pydantic-settings must ignore the deploy variables. This plan uses `PROCESSOR_DEPLOY_*` to mirror Status.
13. **"Deployed" in verify recipes and drift checks:** "live's `REVISION` has no runtime diff from `origin/main`", over a per-repo list of runtime paths. That is the same list Status's drift check means by "code (not docs or tests)", and a coordinator's recipe can read it from the repo instead of restating it.

## Out of scope

- #5, except its `docker.socket` box. Ticking that box on #5 is a GitHub edit, made at ship time with approval.
- #29 and #21.
- A Landlock audit log (6.12 has none).
- Processor's CI gate and drift check (step 7).
