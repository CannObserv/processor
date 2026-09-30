"""The killable child: timeout, ``RLIMIT_AS``, hard exits, a clean result channel (spec §3)."""

import json
import os
import time
from pathlib import Path

import pytest

from processor.child import ChildResult, run_in_child, transform_target
from processor.processors import TRANSFORMS
from processor.processors.extract import extract

TESTS = str(Path(__file__).resolve().parent)
GiB = 1024**3


async def _run(name: str, *args, timeout_s: float = 30, rlimit: int = 2 * GiB) -> ChildResult:
    return await run_in_child(
        f"child_targets:{name}",
        args,
        timeout_s=timeout_s,
        rlimit_as_bytes=rlimit,
        sys_path=(TESTS,),
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
    result = await _run("pid_then_sleep", str(pid_file), 60, timeout_s=2)
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


async def test_the_child_sees_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", "redis://processor:secret@broker:6379/0")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/etc/processor/sa.json")
    result = await _run("environment")
    assert result.kind == "ok"
    assert not {"CO_PROCESSOR_BUS_URL", "GOOGLE_APPLICATION_CREDENTIALS"} & set(result.value)


def test_transform_target_names_an_importable_function() -> None:
    assert transform_target(TRANSFORMS["extract"]) == "processor.processors.extract:extract"


async def test_parity_corpus_is_identical_through_the_child() -> None:
    corpus = Path(TESTS) / "fixtures" / "parity"
    for case in json.loads((corpus / "cases.json").read_text()):
        raw = (corpus / "inputs" / case["input"]).read_bytes()
        args = (raw, case["media_type"], case["source_spec"])
        result = await run_in_child(
            transform_target(extract), args, timeout_s=60, rlimit_as_bytes=3 * GiB
        )
        assert result.kind == "ok", (case["id"], result.detail)
        assert result.value == extract(*args), case["id"]


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
    result = await _run("oom_score_adj")
    assert (result.kind, result.value) == ("ok", "1000")
