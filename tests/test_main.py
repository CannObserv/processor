"""Entry points: ``processor run`` end to end, and ``processor dlq list|show|drop``."""

import asyncio
import hashlib
import json
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from co_core.pure.adapters.bus.dead_letter import dead_letter_fields
from co_core.pure.adapters.bus.envelope import from_wire, to_wire
from co_core.pure.adapters.bus.streams import CONTENT_DERIVED, CONTENT_PROCESS, dlq_name
from co_core.pure.models.changes import ContentProcessCommandEmit, ProcessingCompleteEvent
from co_core_sync.drivers.blobstore.local import LocalBlobStore
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from processor.__main__ import main

pytestmark = pytest.mark.integration

URL = "redis://localhost:6379/15"
HTML = (Path(__file__).parent / "fixtures" / "parity" / "inputs" / "agenda.html").read_bytes()


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
