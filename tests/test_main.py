"""Entry points: ``processor run`` end to end, and ``processor dlq list|show|drop``."""

import asyncio
import hashlib
import json
import logging
import os
import signal
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from co_core.pure.adapters.bus.dead_letter import dead_letter_fields
from co_core.pure.adapters.bus.envelope import from_wire, to_wire
from co_core.pure.adapters.bus.streams import CONTENT_DERIVED, CONTENT_PROCESS, dlq_name
from co_core.pure.models.changes import ContentProcessCommandEmit, ProcessingCompleteEvent
from co_core_sync.drivers.blobstore.local import LocalBlobStore
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from processor.__main__ import main
from processor._contain import landlock_abi, strongest_available
from processor.build import build_id
from processor.child import ChildResult, run_in_child
from processor.settings import Settings

pytestmark = pytest.mark.integration

URL = "redis://localhost:6379/15"
SERVICE = Path(__file__).resolve().parent.parent / "deploy" / "processor.service"
HTML = (Path(__file__).parent / "fixtures" / "parity" / "inputs" / "agenda.html").read_bytes()


@pytest.fixture(autouse=True)
def _restore_root_logging():
    # main() reconfigures the process-wide root logger; put pytest's back after.
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    logging.captureWarnings(False)
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
async def admin():
    client = Redis.from_url(URL, decode_responses=True)
    try:
        await client.ping()
    except RedisConnectionError:
        pytest.skip("the scratch redis-server is not running on localhost:6379")
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "CO_PROCESSOR_BUS_URL": URL,
        "CO_PROCESSOR_STORE_BACKEND": "local",
        "CO_PROCESSOR_LOCAL_INPUT_ROOT": str(tmp_path / "in"),
        "CO_PROCESSOR_LOCAL_OUTPUT_ROOT": str(tmp_path / "out"),
        "CO_PROCESSOR_READ_BLOCK_MS": "200",
        "CO_PROCESSOR_CHILD_CONTAINMENT": strongest_available(),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


async def test_run_processes_a_command_and_stops_on_sigterm(admin, env, tmp_path) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "processor", "run",
        env=os.environ | env, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        for _ in range(100):  # the group exists once the loop is up
            if await admin.exists(CONTENT_PROCESS):
                break
            await asyncio.sleep(0.1)
        digest = hashlib.sha256(HTML).hexdigest()
        store = LocalBlobStore(tmp_path / "in")
        store.store(HTML, digest, "text/html")
        command = ContentProcessCommandEmit(
            occurred_at=datetime.now(UTC),
            command_id="e2e-1",
            info_source_id="src-1",
            input_uri=store.uri_for(digest),
            input_digest=digest,
            processor="extract",
            source_spec={"extraction": {"algorithm": "full_page"}},
            media_type="text/html",
        )
        await admin.xadd(CONTENT_PROCESS, to_wire(command))
        for _ in range(100):
            if await admin.xlen(CONTENT_DERIVED):
                break
            await asyncio.sleep(0.1)
        ((_id, fields),) = await admin.xrange(CONTENT_DERIVED)
        fact = from_wire(fields, topic=CONTENT_DERIVED).payload
        assert isinstance(fact, ProcessingCompleteEvent) and fact.command_id == "e2e-1"
    finally:
        proc.send_signal(signal.SIGTERM)
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    assert proc.returncode == 0
    records = [json.loads(line) for line in stderr.decode().splitlines() if line.startswith("{")]
    (outcome,) = [r for r in records if r.get("command_id") == "e2e-1"]
    assert (outcome["action"], outcome["reason"], outcome["level"]) == ("ack", "complete", "INFO")
    assert outcome["info_source_id"] == "src-1" and "total_ms" in outcome
    (starting,) = [r for r in records if r["message"] == "starting"]
    assert starting["child_containment"] == env["CO_PROCESSOR_CHILD_CONTAINMENT"]
    assert starting["landlock_abi"] == landlock_abi()
    assert starting["build"] == build_id()


async def _seed_dlq(admin: Redis) -> str:
    entry = dead_letter_fields(
        {"event_type": "content_process"},
        source_id="1-1",
        group="processor.process",
        consumer="co-processor",
        reason="undecodable: missing payload",
    )
    return await admin.xadd(dlq_name(CONTENT_PROCESS), entry)


async def test_dlq_list_show_drop(admin, env, capsys) -> None:
    dlq_id = await _seed_dlq(admin)

    assert await asyncio.to_thread(main, ["dlq", "list"]) == 0
    (listed,) = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert listed == {
        "id": dlq_id,
        "source_id": "1-1",
        "reason": "undecodable: missing payload",
        "event_type": "content_process",
    }

    assert await asyncio.to_thread(main, ["dlq", "show", dlq_id]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["fields"] == {"event_type": "content_process"}
    assert shown["provenance"]["consumer"] == "co-processor"

    assert await asyncio.to_thread(main, ["dlq", "drop", dlq_id]) == 0
    assert await admin.xlen(dlq_name(CONTENT_PROCESS)) == 0


async def test_dlq_show_unknown_id_fails(admin, env, capsys) -> None:
    assert await asyncio.to_thread(main, ["dlq", "show", "9-9"]) == 1
    assert "9-9" in capsys.readouterr().err


async def test_ensure_group_creates_processor_process_once(admin, env) -> None:
    assert await asyncio.to_thread(main, ["ensure-group"]) == 0
    assert await asyncio.to_thread(main, ["ensure-group"]) == 0
    (group,) = await admin.xinfo_groups(CONTENT_PROCESS)
    assert (group["name"], group["last-delivered-id"]) == ("processor.process", "0-0")


def test_a_settings_error_is_one_json_record_and_exit_2(monkeypatch, capsys) -> None:
    monkeypatch.setenv("CO_PROCESSOR_BUS_URL", "redis://processor:hunter2@127.0.0.1:1/0")
    monkeypatch.setenv("CO_PROCESSOR_RECLAIM_MIN_IDLE_MS", "1000")
    assert main(["run"]) == 2
    err = capsys.readouterr().err
    (record,) = [json.loads(line) for line in err.splitlines()]
    assert record["level"] == "ERROR" and "reclaim_min_idle_ms" in json.dumps(record)
    assert "hunter2" not in err and "redis://" not in err


def test_a_store_preflight_failure_exits_1_inside_the_units_restart_budget(
    env, monkeypatch, capsys
) -> None:
    # DEPLOYMENT.md "At boot" (#8, #15): a start whose preflight still fails after the
    # storage client's own retries exits 1, and systemd restarts it. That needs a
    # non-zero exit, Restart=on-failure, and retries that stay under systemd's default
    # start limit (5 starts in 10 s), or the unit gives up for good.
    class Unresolved:
        def preflight(self) -> None:
            raise socket.gaierror(-3, "Temporary failure in name resolution")

    stores = SimpleNamespace(input=Unresolved(), output=Unresolved())
    monkeypatch.setattr("processor.__main__.build_stores", lambda settings: stores)
    assert main(["run"]) == 1
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert any(r["message"].startswith("store preflight failed") for r in records)

    unit = dict(
        ln.strip().split("=", 1)
        for ln in SERVICE.read_text().splitlines()
        if "=" in ln and not ln.lstrip().startswith("#")
    )
    assert unit["Restart"] == "on-failure"
    assert not {"StartLimitBurst", "StartLimitIntervalSec"} & unit.keys()
    assert float(unit["RestartSec"]) * 5 > 10


def test_dlq_list_count_must_be_positive(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["dlq", "list", "--count", "0"])
    assert exc.value.code == 2
    assert "--count" in capsys.readouterr().err


def test_run_refuses_to_start_without_the_containment_it_requires(env, monkeypatch, capsys) -> None:
    # Fail closed at boot (#2): a unit that flaps says so once, where every command
    # striking three times and dead-lettering would say it a hundred times, later.
    monkeypatch.setenv("CO_PROCESSOR_CHILD_CONTAINMENT", "required")
    monkeypatch.setattr("processor.__main__.unavailable_reason", lambda: "Landlock ABI 0 < 6")

    def never(settings):
        raise AssertionError("the stores were built before the containment check")

    monkeypatch.setattr("processor.__main__.build_stores", never)
    assert main(["run"]) == 1
    (record,) = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert (record["level"], record["message"]) == ("ERROR", "child containment unavailable")
    assert record["reason"] == "Landlock ABI 0 < 6"


def test_off_starts_where_containment_is_unavailable(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CO_PROCESSOR_CHILD_CONTAINMENT", "off")
    monkeypatch.setattr("processor.__main__.unavailable_reason", lambda: "Landlock ABI 0 < 6")
    monkeypatch.setattr("processor.__main__.make_undumpable", lambda: None)

    def unreachable(settings):
        raise OSError("past the containment check")

    monkeypatch.setattr("processor.__main__.build_stores", unreachable)
    assert main(["run"]) == 1
    messages = [json.loads(line)["message"] for line in capsys.readouterr().err.splitlines()]
    assert messages[-1].startswith("store preflight failed")


def test_run_is_undumpable_before_it_reads_anything(env, monkeypatch) -> None:
    # The env file's credential is in this process's environment from exec on;
    # /proc/<pid>/environ closes before the first store or bus client exists (#2).
    order: list[str] = []
    monkeypatch.setattr("processor.__main__.make_undumpable", lambda: order.append("undumpable"))

    def build(settings):
        order.append("stores")
        raise OSError("stop here")

    monkeypatch.setattr("processor.__main__.build_stores", build)
    assert main(["run"]) == 1
    assert order == ["undumpable", "stores"]


def test_run_refuses_to_start_when_the_canary_extraction_fails(env, monkeypatch, capsys) -> None:
    # CR 1: the ABI check misses a child that fails for any other reason (a library
    # outside the allowlist, as CI's libgcc_s did). Every command would then crash
    # three times, and the third publishes a terminal extraction_error for a
    # healthy document. One real extraction at boot, before anything else runs.
    seen: list[dict] = []

    async def broken_child(target, args, **kwargs) -> ChildResult:
        seen.append({"target": target, **kwargs})
        return ChildResult(kind="crashed", detail="exit status 1: ImportError: libgcc_s.so.1")

    def never(settings):
        raise AssertionError("the stores were built after a failed canary")

    monkeypatch.setattr("processor.__main__.run_in_child", broken_child)
    monkeypatch.setattr("processor.__main__.build_stores", never)
    assert main(["run"]) == 1
    (record,) = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert (record["level"], record["message"]) == ("ERROR", "child canary failed")
    assert record["kind"] == "crashed" and "libgcc_s" in record["detail"]
    (call,) = seen
    assert call["target"] == "processor.processors.extract:extract"
    assert call["containment"] == env["CO_PROCESSOR_CHILD_CONTAINMENT"]
    assert call["rlimit_as_bytes"] == Settings().rlimit_as_bytes


def test_the_canary_is_one_real_contained_extraction(env, monkeypatch) -> None:
    results: list[ChildResult] = []

    async def recording_child(*args, **kwargs) -> ChildResult:
        results.append(await run_in_child(*args, **kwargs))
        return results[-1]

    def stop(settings):
        raise OSError("past the canary")

    monkeypatch.setattr("processor.__main__.run_in_child", recording_child)
    monkeypatch.setattr("processor.__main__.build_stores", stop)
    assert main(["run"]) == 1
    (result,) = results
    assert result.kind == "ok" and not result.value.empty
