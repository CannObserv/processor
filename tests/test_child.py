"""The killable child: timeout, ``RLIMIT_AS``, hard exits, a clean result channel, and
its containment (spec §3, #2).

Every child here runs under ``CONTAINMENT``: ``required`` where the kernel offers it
(``co-processor`` always does), ``off`` elsewhere, as the report header says. The
containment tests run ``required`` and pair each denial with an ``off`` control that
succeeds, so a pass is never a missing file or a dead listener.
"""

import asyncio
import errno
import gzip
import json
import os
import signal
import socket
import time
from pathlib import Path

import pytest

from processor._contain import REQUIRED_ABI, Containment, landlock_abi, strongest_available
from processor.child import ChildResult, run_in_child, transform_target
from processor.processors import TRANSFORMS
from processor.processors.extract import extract

TESTS = str(Path(__file__).resolve().parent)
REPO = Path(TESTS).parent
GiB = 1024**3
CONTAINMENT = strongest_available()
ABI = landlock_abi()
needs_landlock = pytest.mark.skipif(
    ABI < REQUIRED_ABI,
    reason=f"Landlock ABI {ABI} < {REQUIRED_ABI}: child containment untested here",
)


async def _run(
    name: str,
    *args,
    timeout_s: float = 30,
    rlimit: int = 2 * GiB,
    containment: Containment = CONTAINMENT,
) -> ChildResult:
    return await _run_target(
        f"child_targets:{name}", *args, timeout_s=timeout_s, rlimit=rlimit, containment=containment
    )


async def _run_target(
    target: str,
    *args,
    timeout_s: float = 30,
    rlimit: int = 2 * GiB,
    containment: Containment = CONTAINMENT,
    sys_path: tuple[str, ...] = (TESTS,),
) -> ChildResult:
    return await run_in_child(
        target,
        args,
        timeout_s=timeout_s,
        rlimit_as_bytes=rlimit,
        containment=containment,
        sys_path=sys_path,
    )


async def test_ok_carries_the_value_back() -> None:
    result = await _run("echo", b"\x00bytes", {"k": [1, None]})
    assert result.kind == "ok"
    assert result.value == (b"\x00bytes", {"k": [1, None]})


async def test_an_exception_is_raised_not_crashed() -> None:
    result = await _run("raise_value_error")
    assert result.kind == "raised"
    assert result.detail.startswith("ValueError: bad document")


async def test_rlimit_as_turns_a_huge_allocation_into_memory_error() -> None:
    result = await _run("allocate", 4 * GiB, rlimit=1 * GiB)
    assert result.kind == "raised"
    assert result.detail.startswith("MemoryError")


async def test_allocation_under_the_limit_succeeds() -> None:
    result = await _run("allocate", 64 * 1024**2, rlimit=1 * GiB)
    assert (result.kind, result.value) == ("ok", 64 * 1024**2)


async def test_timeout_kills_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    started = time.monotonic()
    # "off": the target writes a pid file, which containment refuses.
    result = await _run("pid_then_sleep", str(pid_file), 60, timeout_s=2, containment="off")
    assert result.kind == "timeout"
    assert time.monotonic() - started < 10
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # reaped: no orphan left behind


@pytest.mark.parametrize(
    ("target", "args", "code"), [("exit_hard", (3,), 3), ("kill_self", (), -9)]
)
async def test_a_hard_exit_is_a_crash(target: str, args: tuple, code: int) -> None:
    result = await _run(target, *args)
    assert result.kind == "crashed"
    assert result.returncode == code


async def test_stdout_chatter_does_not_corrupt_the_result() -> None:
    result = await _run("noisy_stdout")
    assert (result.kind, result.value) == ("ok", "result")


async def test_writes_to_fd_1_do_not_corrupt_the_result() -> None:
    result = await _run("write_fd_1")
    assert (result.kind, result.value) == ("ok", "result")


async def test_a_transform_that_will_not_import_is_a_crash_not_a_raise() -> None:
    # A venv mid-`uv sync` is the host's failure: a strike, never a terminal verdict.
    result = await _run_target("child_targets_absent:echo")
    assert result.kind == "crashed"
    assert "ModuleNotFoundError" in result.detail


async def test_only_the_tail_of_a_stderr_flood_is_kept() -> None:
    result = await _run("flood_stderr_then_exit", 8 * 1024**2)
    assert result.kind == "crashed" and result.detail.endswith("the last words")
    assert len(result.detail) < 2100


@pytest.mark.parametrize(("kind", "payload"), [("raised", 123), ("raised", ["x"]), ("odd", "x")])
async def test_a_result_off_the_protocol_is_a_crash(kind: str, payload: object) -> None:
    result = await _run("forge_protocol", kind, payload)
    assert result.kind == "crashed" and "malformed result" in result.detail


async def test_the_child_sees_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", "redis://processor:secret@broker:6379/0")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/etc/processor/sa.json")
    result = await _run("environment")
    assert result.kind == "ok"
    assert not {"CO_PROCESSOR_BUS_URL", "GOOGLE_APPLICATION_CREDENTIALS"} & set(result.value)


def test_transform_target_names_an_importable_function() -> None:
    assert transform_target(TRANSFORMS["extract"]) == "processor.processors.extract:extract"


def _whole_corpus() -> list[tuple[str, tuple]]:
    corpus = Path(TESTS) / "fixtures" / "parity"
    jobs = [
        (
            case["id"],
            (
                (corpus / "inputs" / case["input"]).read_bytes(),
                case["media_type"],
                case["source_spec"],
            ),
        )
        for case in json.loads((corpus / "cases.json").read_text())
    ]
    for item in json.loads((corpus / "real" / "export.json").read_text())["items"]:
        raw = gzip.decompress((corpus / "real" / f"{item['input_digest']}.bin.gz").read_bytes())
        jobs.append((item["input_digest"][:12], (raw, item["media_type"], item["source_spec"])))
    return jobs


async def test_the_whole_parity_corpus_is_identical_through_the_child() -> None:
    # cases.json and real/ (#16), under CONTAINMENT: the allowlist must cover every
    # import and data file an extractor reaches for (#2).
    jobs = _whole_corpus()
    assert len(jobs) > len(json.loads((Path(TESTS) / "fixtures/parity/cases.json").read_text()))
    for job_id, args in jobs:
        result = await run_in_child(
            transform_target(extract),
            args,
            timeout_s=60,
            rlimit_as_bytes=3 * GiB,
            containment=CONTAINMENT,
        )
        assert result.kind == "ok", (job_id, result.detail)
        assert result.value == extract(*args), job_id


async def test_a_compromised_child_cannot_run_code_in_the_parent(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    result = await _run("evil", str(marker))
    assert result.kind == "crashed"
    assert "unreadable result" in result.detail
    assert not marker.exists()


async def test_only_allowlisted_globals_decode() -> None:
    result = await _run("ordered_dict")
    assert result.kind == "crashed"
    assert "OrderedDict" in result.detail


async def test_the_child_volunteers_as_the_oom_victim() -> None:
    # If the host does reach the OOM killer, the child dies, not the consumer: the
    # unit's OOMPolicy=continue keeps the service up and the loss is a counted crash.
    # "off": the target reads /proc, which containment refuses (the write is before it).
    result = await _run("oom_score_adj", containment="off")
    assert (result.kind, result.value) == ("ok", "1000")


async def test_a_terminal_interrupt_lets_the_child_finish(tmp_path: Path) -> None:
    # Ctrl+C reaches the whole foreground process group. The parent treats SIGINT as
    # "finish the in-flight command"; a KeyboardInterrupt in the child would instead
    # come back as "raised", a terminal extraction_error for a healthy document.
    pid_file = tmp_path / "pid"
    task = asyncio.create_task(_run("pid_then_sleep", str(pid_file), 1.5, containment="off"))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    os.kill(int(pid_file.read_text()), signal.SIGINT)
    result = await task
    assert (result.kind, result.value) == ("ok", None)


# --- containment (#2) ---------------------------------------------------------


def test_the_suite_runs_the_strongest_containment_the_kernel_offers() -> None:
    assert CONTAINMENT == ("required" if ABI >= REQUIRED_ABI else "off")


async def test_a_child_that_cannot_contain_itself_never_runs_the_target(tmp_path: Path) -> None:
    # Fails closed on any kernel: "/" on sys.path is refused (or the ABI is), and the
    # child exits 70 before it reads its request, a strike rather than a verdict.
    marker = tmp_path / "ran"
    result = await _run_target(
        "child_targets:try_write", str(marker), containment="required", sys_path=(TESTS, "/")
    )
    assert (result.kind, result.returncode) == ("crashed", 70)
    assert "child containment failed" in result.detail
    assert not marker.exists()


async def _denied(target: str, *args) -> None:
    """``required`` refuses it; ``off``, the control, allows it."""
    contained = await _run(target, *args, containment="required")
    control = await _run(target, *args, containment="off")
    assert control.kind == "ok" and control.value[0] == "ok", control
    assert contained.kind == "ok", contained.detail
    assert contained.value[0] == "denied", contained.value
    assert contained.value[1] in (errno.EACCES, errno.EPERM), contained.value


def _secret(path: Path) -> Path:
    if not os.access(path, os.R_OK):
        pytest.skip(f"{path} absent or unreadable here; nothing to deny")
    return path


@needs_landlock
async def test_the_child_cannot_read_a_file_outside_its_allowlist(tmp_path: Path) -> None:
    # Stands in for the env file, the GCS key and the credentials directory.
    secret = tmp_path / "secret.json"
    secret.write_text('{"private_key": "x"}')
    await _denied("try_read", str(secret))


@needs_landlock
@pytest.mark.parametrize(
    "path",
    [
        "/etc/processor/.env",
        "/etc/processor/co-gcs-processor-writer.json",
        # After #2's install: the key as the unit receives it, readable by the
        # service's uid through an ACL, so by the child's but for Landlock (CR 9).
        "/run/credentials/processor.service/gcs-writer-key",
        str(REPO / ".env"),
    ],
)
async def test_the_child_cannot_read_the_real_secrets(path: str) -> None:
    await _denied("try_read", str(_secret(Path(path))))


@needs_landlock
async def test_the_child_cannot_read_the_parents_environment() -> None:
    await _denied("try_read", f"/proc/{os.getpid()}/environ")


@needs_landlock
async def test_the_child_cannot_read_the_service_users_home() -> None:
    home = Path.home()
    readable = [p for p in sorted(home.glob(".*")) if p.is_file() and os.access(p, os.R_OK)]
    if not readable:
        pytest.skip(f"no readable file in {home}")
    await _denied("try_read", str(readable[0]))


@needs_landlock
async def test_the_child_cannot_write(tmp_path: Path) -> None:
    await _denied("try_write", str(tmp_path / "dropped"))


@pytest.fixture
def tcp_listener():
    server = socket.create_server(("127.0.0.1", 0))
    yield server.getsockname()[1]
    server.close()


@needs_landlock
async def test_the_child_cannot_open_a_tcp_connection(tcp_listener: int) -> None:
    await _denied("try_connect", "AF_INET", ["127.0.0.1", tcp_listener])


@needs_landlock
async def test_the_child_cannot_reach_the_scratch_redis_port() -> None:
    # The broker's port; the scratch redis-server stands in for it (no control:
    # it may not be running here, and the socket is refused before any connect).
    result = await _run("try_connect", "AF_INET", ["127.0.0.1", 6379], containment="required")
    assert result.value == ["denied", errno.EPERM]


@needs_landlock
async def test_the_child_cannot_reach_a_unix_socket(tmp_path: Path) -> None:
    path = tmp_path / "s.sock"
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        server.listen()
        await _denied("try_connect", "AF_UNIX", str(path))


@needs_landlock
@pytest.mark.parametrize("path", ["/run/docker.sock", "/run/tailscale/tailscaled.sock"])
async def test_the_child_cannot_reach_the_hosts_sockets(path: str) -> None:
    # docker.sock means root through the docker group; tailscaled's is 0666. Both are
    # refused at socket(), before any connect (#2, #5's docker.socket box).
    if not os.path.exists(path):
        pytest.skip(f"{path} absent here")
    result = await _run("try_connect", "AF_UNIX", path, containment="required")
    assert result.value == ["denied", errno.EPERM]


@needs_landlock
async def test_the_child_cannot_signal_its_parent() -> None:
    await _denied("try_signal_parent")


def _loader() -> str:
    """The dynamic loader: an executable inside the allowlist (beside libc)."""
    for line in Path("/proc/self/maps").read_text().splitlines():
        path = line.split()[-1]
        if os.path.basename(path).startswith("ld-linux"):
            return os.path.realpath(path)
    pytest.skip("no ld-linux mapped here")


@needs_landlock
@pytest.mark.parametrize("program", ["/bin/sh", "loader"])
async def test_the_child_cannot_run_a_program(program: str) -> None:
    # CR 2: /bin/sh is outside the allowlist, so refusing it proves little; the
    # loader is inside it (it can run any ELF it can read). No path grants EXECUTE.
    await _denied("try_exec", _loader() if program == "loader" else program)
