---
title: Contain the extraction child (#2)
date: 2026-10-06
status: draft for gate 2 (the operator approves the deploy and install changes before building); per #2 and its hand-off from epic #24 (2026-10-05)
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

## Layer 3: a dedicated user and a root-owned tree (trap 1, option a)

**The deploy model changes; gate 2 approves this section.**

- **User:** `useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin processor`. It gets no sudo and is not in group `docker`.
- **Tree:** `/opt/processor/releases/<sha>/`, holding a `git archive` of the commit plus its own `.venv`:
  - built with `uv sync --frozen --no-dev --no-editable --compile-bytecode --python /usr/bin/python3.12`, so `sys.path` holds no `src/` and no interpreter from under `/home`;
  - owned by `root:root` and not writable by anyone else.

  `/opt/processor/current` is a symlink to the live release. Switching it is atomic: `ln -sfn` to a temporary name, then `mv -T`.
- **`deploy/deploy.sh`** (new; run with `sudo` from `~/processor` after `git pull --ff-only`):
  1. Refuse unless `HEAD` equals `origin/main`. `--rev <sha>` is the escape hatch for a rollback.
  2. The build runs as `$SUDO_USER`, so neither uv nor hatchling ever runs as root. It extracts `git archive`, copies in `.wheelhouse`, and runs `uv sync`, into a release directory that root created and handed to `$SUDO_USER`.
  3. `chown -R root:root`, then `chmod -R go-w`.
  4. Repoint `current`, then `systemctl restart processor`.
  5. Keep the current release and the two before it; delete older ones.

  A release that already exists is reused, which makes a rollback instant: `sudo deploy/deploy.sh --rev <old sha>`.
- **`deploy/smoke.sh`** (new): runs `scripts/smoke_scratch_bus.py` from `current`, **as `processor`**, through `systemd-run --pipe --wait`. It passes the same `User=`, `EnvironmentFile=`, `LoadCredential=` and containment as the unit, so the smoke test exercises the contained child under the real identity.
- **Secrets:**
  - `/etc/processor` becomes `root:root 700`, and `.env` and the key `root 600`.
  - systemd reads `EnvironmentFile=` before it switches users, so the service user never reads the file.
  - The key arrives through `LoadCredential=gcs-writer-key:/etc/processor/co-gcs-processor-writer.json` and `Environment=GOOGLE_APPLICATION_CREDENTIALS=%d/gcs-writer-key`.
  - The `GOOGLE_APPLICATION_CREDENTIALS=` line **is removed from `.env`**, because `EnvironmentFile=` overrides `Environment=`. Settings doesn't read the variable (`extra="ignore"`); google-auth reads the path unchanged.
  - Under `/run/credentials/processor.service/` the key is readable by the service's uid, and so by the child's. Landlock denies the child that path.
- **Unit:**
  - `User=processor`, `Group=processor`;
  - `WorkingDirectory=/opt/processor/current`;
  - `ExecStart=/opt/processor/current/.venv/bin/processor run`;
  - `LoadCredential=` and `Environment=` as above;
  - adds `ProtectHome=yes` and `ProtectSystem=strict`. Nothing in the service writes outside `PrivateTmp`, and bytecode is compiled at build time.

  These two are one line each, and the new layout makes them free. The rest of #5's hardening stays out of scope.
- **What the change does to the docs' invariants:**
  - "Code on `main` is the deployed code" becomes: **the deployed release is `readlink /opt/processor/current`, and it carries no runtime diff from `origin/main`**.
  - `~/processor` stays the operator's clone (the skills hook still pushes from it), but it is no longer the unit's `WorkingDirectory`.
- **The epic's verify recipe, new step 3:**

  ```bash
  rel=$(basename "$(readlink /opt/processor/current)")
  git -C ~/processor diff --quiet "$rel" origin/main -- src deploy pyproject.toml uv.lock && echo deployed
  ```

  A docs-only merge then needs no redeploy, as it does today.

## Rollout and rollback (gate 3)

The rollout runs in two stages, so each risk is taken on its own during shadow (a failed command stays pending, and Watcher doesn't act on it).

- **Stage A (after the FF merge; the existing deploy path, the old unit):** `git pull --ff-only && uv sync --frozen --no-dev && sudo systemctl restart processor`.
  - Layers 1 and 2 go live while the service still runs as `exedev`. In that layout `src/` is on the allowlist and the repo root is not.
  - Check: `starting` shows `child_containment: required`, `landlock_abi: 6`, and the smoke test passes. Then wait for the next shadow command: it must be `ack: complete` with #26's digests.
  - **Rollback A:** add `CO_PROCESSOR_CHILD_CONTAINMENT=off` to the env file and restart. No code revert is needed.
- **Stage B (the operator's install):**
  1. Back up `/etc/systemd/system/processor.service` and `/etc/processor/` to `/var/backups/processor/2/`.
  2. `useradd`.
  3. `chown`, `chmod` and the `sed` that drops `GOOGLE_APPLICATION_CREDENTIALS` from `.env`.
  4. `install` the new unit, then `daemon-reload`.
  5. `sudo deploy/deploy.sh`, which builds and restarts.
  6. `deploy/smoke.sh`.
  7. Live checks:
     - the main PID's user is `processor`;
     - `/proc/<pid>/status` shows `NoNewPrivs: 1`, and `sudo -u processor cat /proc/<pid>/environ` is refused (the parent is undumpable);
     - `sudo -u processor cat /etc/processor/.env` is refused;
     - `systemctl is-enabled docker.socket` says `disabled`.
  8. The next shadow command is `ack: complete` with its digests, and Watcher still reports a match.

  **Rollback B:** restore the backed-up unit and `/etc/processor` (ownership and the removed line), `daemon-reload`, then restart. That returns to Stage A: `exedev` and `~/processor`. `/opt/processor` and the user can stay.

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
- `starting` carries the two new fields.

`tests/test_settings.py`: the default is `required`, and `off` parses.

Unit-file assertions, in `test_main.py`'s existing parsing:
- `User=processor`;
- no `/home` path anywhere;
- `LoadCredential=` together with `GOOGLE_APPLICATION_CREDENTIALS=%d/…`;
- `ProtectHome=yes` and `ProtectSystem=strict`.

`deploy/*.sh`: `bash -n`, plus a test that `deploy.sh` refuses when `HEAD` ≠ `origin/main` (run with no sudo; it refuses before doing anything).

## Steps

1. This plan → gate 2.
2. `_contain` and its tests.
3. Child wiring, settings, `Deps`, the boot check, dumpable; the child and main tests; the corpus through the contained child.
4. Unit, `deploy.sh`, `smoke.sh` and their tests.
5. Docs:
   - spec §3: the "Known limitation" paragraph becomes **Containment**, covering the layers, what stays reachable, and the limits;
   - DEPLOYMENT: prerequisites (the user, `docker.socket`), the env file and credential, build/install/deploy for `/opt`, rollback, and a new **Containment** check list;
   - AGENTS.md: the child paragraph ("not a sandbox" goes), the Service and VM rows, and "Code on `main` is the deployed code" and the checkout notes;
   - the `child.py` and `_child.py` docstrings.
6. Full suite, ruff, PR, CR → FF merge → Stage A → Stage B (gate 3) → the closing comment, with each box's evidence and one live record.

## Known limits (for the closing comment)

- The child still shares the kernel: every syscall except the socket family is reachable.
- It can read the code it runs, the stdlib and the shared libraries.
- It can spend CPU up to the timeout and memory up to `RLIMIT_AS`.
- It decides its own command's output. That can't be avoided; Watcher's shadow comparison is the check on it.
- It shares the service's uid. Landlock (filesystem, signal scope, ptrace) and the parent's non-dumpable flag carry that, not uid separation.

## Out of scope

- #5, except its `docker.socket` box. Ticking that box on #5 is a GitHub edit, made at ship time with approval.
- #29 and #21.
- A Landlock audit log (6.12 has none).
