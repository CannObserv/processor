"""Draining ``content.process.dlq``: list, show, drop over ``XRANGE`` / ``XDEL``."""

import pytest
from co_core.pure.adapters.bus.dead_letter import dead_letter_fields
from co_core.pure.adapters.bus.streams import CONTENT_PROCESS, dlq_name
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from processor import dlq

pytestmark = pytest.mark.integration

DLQ = dlq_name(CONTENT_PROCESS)


@pytest.fixture
async def client():
    client = Redis.from_url("redis://localhost:6379/15")  # bytes, as the service's client
    try:
        await client.ping()
    except RedisConnectionError:
        pytest.skip("the scratch redis-server is not running on localhost:6379")
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def _park(client: Redis, entry_id: str = "*", reason: str = "undecodable: x") -> str:
    fields = dead_letter_fields(
        {"event_type": "content_process"},
        source_id="1-1",
        group="processor.process",
        consumer="co-processor",
        reason=reason,
    )
    return (await client.xadd(DLQ, fields, id=entry_id)).decode()


async def test_list_summarizes_oldest_first(client: Redis) -> None:
    first = await _park(client, reason="first")
    second = await _park(client, reason="second")
    rows = await dlq.list_entries(client)
    assert [(r["id"], r["reason"]) for r in rows] == [(first, "first"), (second, "second")]
    assert rows[0]["event_type"] == "content_process" and rows[0]["source_id"] == "1-1"
    assert len(await dlq.list_entries(client, count=1)) == 1


async def test_show_splits_fields_from_provenance(client: Redis) -> None:
    entry_id = await _park(client)
    shown = await dlq.show(client, entry_id)
    assert shown["id"] == entry_id
    assert shown["fields"] == {"event_type": "content_process"}
    assert shown["provenance"]["group"] == "processor.process"


async def test_show_reports_the_entry_it_found_not_the_id_asked_for(client: Redis) -> None:
    # XRANGE reads a bare millisecond id as a range over that millisecond, so the
    # entry found need not carry the id typed; `drop` must be handed the real one.
    await _park(client, "5-3")
    shown = await dlq.show(client, "5")
    assert shown["id"] == "5-3"


async def test_show_and_drop_an_unknown_id(client: Redis) -> None:
    assert await dlq.show(client, "9-9") is None
    assert await dlq.drop(client, "9-9") is False


async def test_drop_deletes_one_entry(client: Redis) -> None:
    keep = await _park(client)
    gone = await _park(client)
    assert await dlq.drop(client, gone) is True
    assert [r["id"] for r in await dlq.list_entries(client)] == [keep]
