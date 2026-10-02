"""``scripts/smoke_scratch_bus.py``: one real command through the consumer loop (#22).

Runs on the scratch Redis (db 14, left empty) with a local output store in place of
the production one the script uses on ``co-processor``.
"""

import importlib.util
from pathlib import Path

import pytest
from co_core.pure.adapters.bus.streams import CONTENT_DERIVED, CONTENT_PROCESS
from co_core_sync.drivers.blobstore.local import LocalBlobStore
from redis.asyncio import Redis

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke_scratch_bus.py"
_spec = importlib.util.spec_from_file_location("smoke_scratch_bus", SCRIPT)
smoke_scratch_bus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke_scratch_bus)

URL = "redis://localhost:6379/14"


async def test_one_command_round_trips_and_leaves_the_scratch_db_empty(tmp_path: Path) -> None:
    item = smoke_scratch_bus.export_item(smoke_scratch_bus.DEFAULT_DIGEST)
    output = LocalBlobStore(tmp_path / "out")

    fact = await smoke_scratch_bus.smoke(URL, output, item)

    assert fact.output_digest == item["recorded_fingerprint"]
    assert output.exists(fact.output_digest.removeprefix("sha256:"))
    admin = Redis.from_url(URL)
    try:
        assert await admin.exists(CONTENT_PROCESS, CONTENT_DERIVED) == 0
    finally:
        await admin.aclose()


@pytest.mark.parametrize("url", ["redis://broker:6379/0", "redis://192.0.2.10:6379/0"])
async def test_refuses_anything_but_the_local_scratch_redis(tmp_path: Path, url: str) -> None:
    item = smoke_scratch_bus.export_item(smoke_scratch_bus.DEFAULT_DIGEST)
    with pytest.raises(ValueError, match="scratch"):
        await smoke_scratch_bus.smoke(url, LocalBlobStore(tmp_path / "out"), item)


async def test_refuses_a_scratch_db_already_holding_the_streams(tmp_path: Path) -> None:
    admin = Redis.from_url(URL)
    try:
        await admin.xadd(CONTENT_PROCESS, {"x": "1"})
        item = smoke_scratch_bus.export_item(smoke_scratch_bus.DEFAULT_DIGEST)
        with pytest.raises(RuntimeError, match="not empty"):
            await smoke_scratch_bus.smoke(URL, LocalBlobStore(tmp_path / "out"), item)
        assert await admin.xlen(CONTENT_PROCESS) == 1  # someone else's: left alone
    finally:
        await admin.delete(CONTENT_PROCESS)
        await admin.aclose()
