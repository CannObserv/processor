"""Contain the extraction child: Landlock and seccomp; the parent made undumpable (#2).

The child parses untrusted documents, and shares the service's uid. Before it reads
its request, it restricts itself (``contain``), so a parser bug that gives it code
execution reaches only:

- **Files:** read (and execute) on a derived allowlist: each ``sys.path`` entry, the
  stdlib, the shared-library directory, ``/etc/ld.so.cache`` and the mime-types
  files co-core reads at import. No write anywhere; nothing under ``/proc``,
  ``/etc/processor``, ``/run/credentials`` or a home directory.
- **Network:** no TCP bind or connect (Landlock), and no socket of any family at all
  (seccomp: ``socket``, ``socketpair`` and ``io_uring_*`` fail with EPERM). Landlock
  alone does not stop a connect to an existing pathname unix socket, such as
  tailscaled's or docker's (measured on 6.12, 2026-10-06): seccomp does.
- **Processes:** no signal to, and no ptrace of, a process outside its domain.

The parent calls ``make_undumpable`` at start, which closes ``/proc/<pid>/environ``
(the broker credential) and ptrace to every same-uid process.

Only the stdlib, through ``ctypes``: no binding to vendor (spec §3, #2). libc is
loaded at import, before any restriction.
"""

import ctypes
import errno
import mimetypes
import os
import platform
import pwd
import sys
import sysconfig
from collections.abc import Sequence
from typing import Literal

Containment = Literal["required", "off"]

# Abstract unix sockets and signals are scoped from ABI 6 (Linux 6.12); TCP from 4.
REQUIRED_ABI = 6

SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000

_PR_SET_DUMPABLE = 4
_PR_SET_SECCOMP = 22
_PR_SET_NO_NEW_PRIVS = 38
_SECCOMP_MODE_FILTER = 2

# The same numbers on x86_64 and aarch64 (the unified table from 424 up).
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1

_FS_EXECUTE = 1 << 0
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_ALL_ABI_6 = (1 << 16) - 1  # EXECUTE … IOCTL_DEV
_NET_BIND_TCP = 1 << 0
_NET_CONNECT_TCP = 1 << 1
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_SCOPE_SIGNAL = 1 << 1

# arch → (AUDIT_ARCH, syscalls refused with EPERM, whether x32 numbers exist)
_SECCOMP_ARCHES = {
    "x86_64": (0xC000003E, (41, 53, 425, 426, 427), True),
    "aarch64": (0xC00000B7, (198, 199, 425, 426, 427), False),
}
_X32_SYSCALL_BIT = 0x40000000

# Never allowlisted, nor anything beneath them, nor an ancestor of them.
_SECRET_ROOTS = ("/etc/processor", "/run/credentials", "/proc", "/sys")
# Never allowlisted whole, nor an ancestor; a path beneath one may be (dev's src/).
_WIDE_ROOTS = ("/home", "/root", "/tmp", "/var", "/run", "/dev", "/etc")

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


class ContainmentUnavailable(RuntimeError):
    """``required`` containment cannot be applied here; the child must not run."""


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


def _fail(what: str) -> ContainmentUnavailable:
    return ContainmentUnavailable(f"{what}: {os.strerror(ctypes.get_errno())}")


def landlock_abi() -> int:
    """The kernel's Landlock ABI version; 0 when Landlock is absent or disabled."""
    version = _libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), _LANDLOCK_CREATE_RULESET_VERSION
    )
    return max(int(version), 0)


def unavailable_reason(*, abi: int | None = None, machine: str | None = None) -> str | None:
    """Why ``required`` containment cannot run here, or None when it can."""
    abi = landlock_abi() if abi is None else abi
    machine = platform.machine() if machine is None else machine
    if abi < REQUIRED_ABI:
        return f"Landlock ABI {abi} < {REQUIRED_ABI}"
    if machine not in _SECCOMP_ARCHES:
        return f"no seccomp syscall table for {machine}"
    return None


def strongest_available() -> Containment:
    """``required`` where it can run, else ``off``: what the test suite runs under."""
    return "required" if unavailable_reason() is None else "off"


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def shared_library_dir() -> str:
    """Where the extension modules' shared libraries are: ``LIBDIR``, multiarch-qualified.

    Debian's ``LIBDIR`` already ends in the multiarch triplet; a plain build's is
    ``/usr/lib``, with the triplet beneath it.
    """
    libdir = sysconfig.get_config_var("LIBDIR") or "/usr/lib"
    multiarch = sysconfig.get_config_var("MULTIARCH") or ""
    if multiarch and os.path.basename(libdir.rstrip("/")) != multiarch:
        return os.path.join(libdir, multiarch)
    return libdir


def read_allowlist(sys_path: Sequence[str]) -> list[str]:
    """The paths the child may read: derived from the interpreter, never hard-coded.

    Raises ``ContainmentUnavailable`` for an entry that would widen the set to a
    secret or to a whole tree: ``/``, ``/etc``, ``/home``, a home directory, or a
    directory holding a ``.env`` (the repo root; its ``src/`` is fine).
    """
    candidates = [*sys_path, sysconfig.get_paths()["stdlib"], shared_library_dir()]
    files = ["/etc/ld.so.cache", *mimetypes.knownfiles]
    home = pwd.getpwuid(os.getuid()).pw_dir
    allowed: list[str] = []
    for entry in candidates:
        if not entry or not os.path.exists(entry):
            continue
        path = os.path.realpath(entry)
        for root in (*_SECRET_ROOTS, *_WIDE_ROOTS, home):
            if _is_within(root, path):
                raise ContainmentUnavailable(f"{entry}: refused, it contains {root}")
        for root in _SECRET_ROOTS:
            if _is_within(path, root):
                raise ContainmentUnavailable(f"{entry}: refused, it is within {root}")
        if os.path.isdir(path) and os.path.exists(os.path.join(path, ".env")):
            raise ContainmentUnavailable(f"{entry}: refused, it holds a .env")
        if path not in allowed:
            allowed.append(path)
    allowed += [f for f in dict.fromkeys(files) if os.path.isfile(f) and f not in allowed]
    return allowed


def seccomp_program(machine: str) -> list[tuple[int, int, int, int]]:
    """Classic BPF: KILL another arch; EPERM x32 and the socket family; ALLOW the rest."""
    if machine not in _SECCOMP_ARCHES:
        raise ContainmentUnavailable(f"no seccomp syscall table for {machine}")
    audit_arch, denied, has_x32 = _SECCOMP_ARCHES[machine]
    ld, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    deny = SECCOMP_RET_ERRNO | errno.EPERM
    # Each jump counts instructions to skip; the tail is [ALLOW, DENY].
    body: list[tuple[int, int, int, int]] = [(ld, 0, 0, 0)]  # seccomp_data.nr
    checks = len(denied) + (1 if has_x32 else 0)
    if has_x32:
        body.append((jge, checks, 0, _X32_SYSCALL_BIT))
    for i, nr in enumerate(denied):
        remaining = len(denied) - i - 1
        body.append((jeq, remaining + 1, 0, nr))
    return [
        (ld, 0, 0, 4),  # seccomp_data.arch
        (jeq, 1, 0, audit_arch),
        (ret, 0, 0, SECCOMP_RET_KILL_PROCESS),
        *body,
        (ret, 0, 0, SECCOMP_RET_ALLOW),
        (ret, 0, 0, deny),
    ]


def no_new_privs() -> None:
    """``PR_SET_NO_NEW_PRIVS``: required by Landlock and seccomp for an unprivileged caller."""
    if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise _fail("PR_SET_NO_NEW_PRIVS")


def restrict_landlock(read_paths: Sequence[str]) -> None:
    """Read and execute ``read_paths`` only; no TCP; scoped abstract sockets and signals."""
    attr = _RulesetAttr(
        _FS_ALL_ABI_6, _NET_BIND_TCP | _NET_CONNECT_TCP, _SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL
    )
    ruleset = _libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), 0
    )
    if ruleset < 0:
        raise _fail("landlock_create_ruleset")
    try:
        for path in read_paths:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                access = _FS_EXECUTE | _FS_READ_FILE
                if os.path.isdir(path):
                    access |= _FS_READ_DIR
                rule = _PathBeneathAttr(access, fd)
                if (
                    _libc.syscall(
                        _SYS_LANDLOCK_ADD_RULE,
                        ruleset,
                        _LANDLOCK_RULE_PATH_BENEATH,
                        ctypes.byref(rule),
                        0,
                    )
                    != 0
                ):
                    raise _fail(f"landlock_add_rule {path}")
            finally:
                os.close(fd)
        if _libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0) != 0:
            raise _fail("landlock_restrict_self")
    finally:
        os.close(ruleset)


def deny_sockets(machine: str | None = None) -> None:
    """Install ``seccomp_program`` on the calling thread (inherited by its children)."""
    program = seccomp_program(platform.machine() if machine is None else machine)
    filters = (_SockFilter * len(program))(*(_SockFilter(*ins) for ins in program))
    fprog = _SockFprog(len(program), filters)
    if _libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise _fail("PR_SET_SECCOMP")


def contain(mode: Containment, *, abi: int | None = None) -> None:
    """Apply every layer under ``required``; nothing under ``off``. Never partly."""
    if mode == "off":
        return
    if mode != "required":
        raise ValueError(f"unknown containment mode {mode!r}: 'required' or 'off'")
    if reason := unavailable_reason(abi=abi):
        raise ContainmentUnavailable(reason)
    paths = read_allowlist(sys.path)  # at call time: the child has just extended it
    no_new_privs()
    restrict_landlock(paths)
    deny_sockets()


def make_undumpable() -> None:
    """``PR_SET_DUMPABLE`` 0: no same-uid process reads our ``/proc/<pid>/environ``."""
    if _libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise _fail("PR_SET_DUMPABLE")
