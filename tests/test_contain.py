"""The child's containment primitives: Landlock, seccomp, the undumpable parent (#2).

The child's own behaviour under containment is ``tests/test_child.py``. Here each
layer is proved alone, in a subprocess, so a pass names the layer that denied.
"""

import errno
import json
import os
import socket
import subprocess
import sys
import sysconfig
import textwrap
import time
from pathlib import Path

import pytest

from processor._contain import (
    REQUIRED_ABI,
    SECCOMP_RET_ALLOW,
    SECCOMP_RET_ERRNO,
    SECCOMP_RET_KILL_PROCESS,
    ContainmentUnavailable,
    contain,
    landlock_abi,
    read_allowlist,
    seccomp_program,
    shared_library_dirs,
    strongest_available,
    unavailable_reason,
)

ABI = landlock_abi()
needs_landlock = pytest.mark.skipif(
    ABI < REQUIRED_ABI,
    reason=f"Landlock ABI {ABI} < {REQUIRED_ABI}: child containment untested here",
)

AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_I386 = 0x40000003
AUDIT_ARCH_AARCH64 = 0xC00000B7


def _python(code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(code), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


# --- availability -----------------------------------------------------------


def test_the_abi_probe_answers_a_version() -> None:
    assert isinstance(ABI, int) and ABI >= 0


def test_co_processor_never_skips_the_containment_tests() -> None:
    if socket.gethostname() != "co-processor":
        pytest.skip("not co-processor")
    assert landlock_abi() >= REQUIRED_ABI
    assert strongest_available() == "required"


@pytest.mark.parametrize(
    ("abi", "machine", "fragment"),
    [(5, "x86_64", "Landlock ABI 5"), (0, "x86_64", "Landlock ABI 0"), (6, "riscv64", "riscv64")],
)
def test_unavailable_reason_names_what_is_missing(abi: int, machine: str, fragment: str) -> None:
    assert fragment in unavailable_reason(abi=abi, machine=machine)


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_abi_6_on_a_known_arch_is_available(machine: str) -> None:
    assert unavailable_reason(abi=6, machine=machine) is None
    assert unavailable_reason(abi=7, machine=machine) is None


def test_required_refuses_rather_than_contain_partly() -> None:
    with pytest.raises(ContainmentUnavailable, match="Landlock ABI 5"):
        contain("required", abi=5)


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="best_effort"):
        contain("best_effort")  # type: ignore[arg-type]


def test_off_contains_nothing() -> None:
    result = _python(
        """
        import socket
        from processor._contain import contain
        contain("off")
        socket.socket().close()
        print(next(ln for ln in open("/proc/self/status") if ln.startswith("NoNewPrivs")))
        """
    )
    ours = next(ln for ln in open("/proc/self/status") if ln.startswith("NoNewPrivs"))
    assert (result.returncode, result.stdout.strip()) == (0, ours.strip()), result.stderr


# --- the read allowlist -------------------------------------------------------


def test_the_allowlist_is_derived_from_the_interpreter() -> None:
    allowed = read_allowlist(sys.path)
    expected = {os.path.realpath(p) for p in sys.path if p and os.path.exists(p)}
    expected |= {os.path.realpath(sysconfig.get_paths()["stdlib"]), *shared_library_dirs()}
    expected |= {p for p in ["/etc/ld.so.cache"] if os.path.exists(p)}
    assert expected <= set(allowed)
    if os.path.exists("/etc/mime.types"):  # co-core reads it at import (measured 2026-10-06)
        assert "/etc/mime.types" in allowed


def _mapped(name: str) -> str:
    for line in Path("/proc/self/maps").read_text().splitlines():
        path = line.split()[-1]
        if os.path.basename(path).startswith(name):
            return os.path.realpath(path)
    raise AssertionError(f"{name} is not mapped")


def test_the_shared_library_dirs_are_the_loaders_not_pythons() -> None:
    # CI's Python (setup-python) has a LIBDIR of its own, and libgcc_s, which an
    # extractor loads later, sits beside libc: the directory the loader uses.
    dirs = shared_library_dirs()
    assert os.path.dirname(_mapped("libc.so")) in dirs
    assert all(os.path.isabs(d) and os.path.isdir(d) for d in dirs)


def test_missing_and_empty_entries_are_skipped(tmp_path: Path) -> None:
    allowed = read_allowlist(["", str(tmp_path / "absent")])
    assert str(tmp_path / "absent") not in allowed
    assert "" not in allowed


@pytest.mark.parametrize(
    "entry", ["/", "/etc", "/home", "/run", "/tmp", "/etc/processor", "/proc/self", "/proc"]
)
def test_an_entry_that_would_widen_the_set_is_refused(entry: str) -> None:
    if not os.path.exists(entry):
        pytest.skip(f"{entry} does not exist here")
    with pytest.raises(ContainmentUnavailable, match="refused"):
        read_allowlist([entry])


def test_the_service_users_home_is_refused() -> None:
    home = Path.home()
    if not home.is_dir():
        pytest.skip(f"{home} does not exist")
    with pytest.raises(ContainmentUnavailable, match="refused"):
        read_allowlist([str(home)])


def test_a_directory_holding_an_env_file_is_refused(tmp_path: Path) -> None:
    # The repo root: it holds .env (GH tokens), and src/ beneath it is allowed.
    (tmp_path / ".env").write_text("GH_TOKEN=x\n")
    (tmp_path / "src").mkdir()
    assert str(tmp_path / "src") in read_allowlist([str(tmp_path / "src")])
    with pytest.raises(ContainmentUnavailable, match=r"\.env"):
        read_allowlist([str(tmp_path)])


# --- the seccomp program, run on a BPF interpreter ----------------------------


def _run_bpf(program: list[tuple[int, int, int, int]], *, arch: int, nr: int) -> int:
    """Classic BPF over ``struct seccomp_data``: only what ``seccomp_program`` emits."""
    data = {0: nr, 4: arch}
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = program[pc]
        if code == 0x20:  # BPF_LD | BPF_W | BPF_ABS
            acc = data[k]
        elif code == 0x15:  # BPF_JMP | BPF_JEQ | BPF_K
            pc += jt if acc == k else jf
        elif code == 0x35:  # BPF_JMP | BPF_JGE | BPF_K
            pc += jt if acc >= k else jf
        elif code == 0x06:  # BPF_RET | BPF_K
            return k
        else:
            raise AssertionError(f"unexpected opcode {code:#x}")
        pc += 1


EPERM = SECCOMP_RET_ERRNO | errno.EPERM


@pytest.mark.parametrize(
    ("machine", "arch", "nr", "action"),
    [
        ("x86_64", AUDIT_ARCH_X86_64, 41, EPERM),  # socket
        ("x86_64", AUDIT_ARCH_X86_64, 53, EPERM),  # socketpair
        ("x86_64", AUDIT_ARCH_X86_64, 425, EPERM),  # io_uring_setup
        ("x86_64", AUDIT_ARCH_X86_64, 426, EPERM),  # io_uring_enter
        ("x86_64", AUDIT_ARCH_X86_64, 427, EPERM),  # io_uring_register
        ("x86_64", AUDIT_ARCH_X86_64, 0x40000000 + 41, EPERM),  # x32 socket
        ("x86_64", AUDIT_ARCH_X86_64, 0, SECCOMP_RET_ALLOW),  # read
        ("x86_64", AUDIT_ARCH_X86_64, 42, SECCOMP_RET_ALLOW),  # connect: Landlock's
        ("x86_64", AUDIT_ARCH_I386, 102, SECCOMP_RET_KILL_PROCESS),  # i386 socketcall
        ("aarch64", AUDIT_ARCH_AARCH64, 198, EPERM),  # socket
        ("aarch64", AUDIT_ARCH_AARCH64, 199, EPERM),  # socketpair
        ("aarch64", AUDIT_ARCH_AARCH64, 425, EPERM),
        ("aarch64", AUDIT_ARCH_AARCH64, 63, SECCOMP_RET_ALLOW),  # read
        ("aarch64", AUDIT_ARCH_X86_64, 41, SECCOMP_RET_KILL_PROCESS),
    ],
)
def test_the_seccomp_program(machine: str, arch: int, nr: int, action: int) -> None:
    assert _run_bpf(seccomp_program(machine), arch=arch, nr=nr) == action


def test_an_unknown_arch_has_no_program() -> None:
    with pytest.raises(ContainmentUnavailable, match="riscv64"):
        seccomp_program("riscv64")


# --- each layer alone, in a subprocess ----------------------------------------


@pytest.fixture
def tcp_listener():
    server = socket.create_server(("127.0.0.1", 0))
    yield server.getsockname()[1]
    server.close()


@pytest.fixture
def unix_listener(tmp_path: Path):
    path = tmp_path / "s.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen()
    yield str(path)
    server.close()


_PROBE = """
    import json, socket, sys
    from processor import _contain
    layer, port, unix_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    _contain.no_new_privs()
    if layer == "landlock":
        _contain.restrict_landlock(_contain.read_allowlist(sys.path))
    else:
        _contain.deny_sockets()

    def attempt(family, address):
        try:
            s = socket.socket(family)
        except OSError as exc:
            return ["socket", exc.errno]
        try:
            s.connect(address)
            return ["connected", 0]
        except OSError as exc:
            return ["connect", exc.errno]
        finally:
            s.close()

    print(json.dumps({
        "tcp": attempt(socket.AF_INET, ("127.0.0.1", port)),
        "unix": attempt(socket.AF_UNIX, unix_path),
    }))
"""


@needs_landlock
def test_landlock_alone_denies_tcp_but_not_a_pathname_unix_socket(
    tcp_listener: int, unix_listener: str
) -> None:
    # Trap 2 (#2): on 6.12, Landlock has no right for connecting to an existing
    # pathname socket. This is why the child also runs the seccomp filter. If a
    # kernel closes the gap, this fails: update spec §3, keep the filter.
    result = _python(_PROBE, "landlock", str(tcp_listener), unix_listener)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "tcp": ["connect", errno.EACCES],
        "unix": ["connected", 0],
    }


def test_seccomp_alone_denies_every_socket(tcp_listener: int, unix_listener: str) -> None:
    if unavailable_reason(abi=REQUIRED_ABI) is not None:
        pytest.skip(unavailable_reason(abi=REQUIRED_ABI))
    result = _python(_PROBE, "seccomp", str(tcp_listener), unix_listener)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "tcp": ["socket", errno.EPERM],
        "unix": ["socket", errno.EPERM],
    }


# --- the parent: undumpable ---------------------------------------------------


@pytest.mark.parametrize("undumpable", [True, False])
def test_an_undumpable_process_hides_its_environment(tmp_path: Path, undumpable: bool) -> None:
    ready = tmp_path / "ready"
    code = f"""
        import pathlib, time
        from processor._contain import make_undumpable
        if {undumpable}:
            make_undumpable()
        pathlib.Path({str(ready)!r}).write_text("1")
        time.sleep(30)
    """
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", textwrap.dedent(code)],
        env={"PATH": "/usr/bin:/bin", "SECRET": "s3cret"},
    )
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        environ = Path(f"/proc/{proc.pid}/environ")
        if undumpable:
            with pytest.raises(PermissionError):
                environ.read_bytes()
        else:  # the control: a same-uid sibling reads it today
            assert b"SECRET=s3cret" in environ.read_bytes()
    finally:
        proc.kill()
        proc.wait()
