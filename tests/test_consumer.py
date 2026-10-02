"""The consumer loop against the scratch redis-server, as broker#75's ACL user (spec §3, §4).

Runs on db 15 (flushed around each test) as a test user holding exactly the grants
broker#75 gives ``processor`` — plus ``+select``, which db-15 isolation needs and the
broker's db-0 user does not. After every test ``ACL LOG`` must hold no denial for
that user: the capture broker#75 asks for, that the consumer issues only granted
commands. Two tests narrow the grant or cap memory on purpose to show ``NOPERM`` and
``OOM`` leave the entry pending rather than dead-lettering it.
"""

import asyncio
import hashlib
import itertools
import logging
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from co_core.pure.adapters.bus.dead_letter import split_dead_letter
from co_core.pure.adapters.bus.envelope import from_wire, to_wire
from co_core.pure.adapters.bus.streams import CONTENT_DERIVED, CONTENT_PROCESS, dlq_name
from co_core.pure.models.changes import (
    ContentProcessCommandEmit,
    ProcessingCompleteEvent,
    ProcessingFailedEmit,
    ProcessingFailedEvent,
)
from co_core_aio.bus import AsyncBusConsumer, AsyncBusPublisher
from co_core_sync.drivers.blobstore.local import LocalBlobStore
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from processor import consumer as consumer_module
from processor.child import ChildResult, run_in_child
from processor.consumer import GROUP, Consumer, redis_client
from processor.handler import Deps
from processor.processors.extract import extract
from processor.stores import Stores

pytestmark = pytest.mark.integration

DB = 15
USER, PASSWORD = "processor-test", "scratch-only-not-a-secret"
GRANTS = [
    "~content.process", "~content.process.dlq", "resetchannels",
    "+xreadgroup", "+xack", "+xautoclaim", "+xgroup|create", "+xlen", "+xrange",
    "+xinfo|stream", "+info", "+ping",
    "(+xadd ~content.derived ~content.process.dlq)",
    "(+xdel ~content.process.dlq)",
    "+select",  # db-15 isolation only; the broker's user lives on db 0
]  # fmt: skip
HTML = (Path(__file__).parent / "fixtures" / "parity" / "inputs" / "agenda.html").read_bytes()
SPEC = {"extraction": {"algorithm": "css", "selector": "#main"}}
GiB = 1024**3
T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
async def admin():
    client = Redis.from_url(f"redis://localhost:6379/{DB}")
    try:
        await client.ping()
    except RedisConnectionError:
        pytest.skip("the scratch redis-server is not running on localhost:6379")
    await client.flushdb()
    await client.execute_command("ACL", "LOG", "RESET")
    yield client
    await client.flushdb()
    await client.aclose()


async def _setuser(admin: Redis, user: str, grants: list[str]) -> Redis:
    await admin.execute_command("ACL", "SETUSER", user, "reset", "on", f">{PASSWORD}", *grants)
    return redis_client(f"redis://{user}:{PASSWORD}@localhost:6379/{DB}", read_block_ms=100)


async def _denials(admin: Redis, user: str) -> list:
    entries = await admin.execute_command("ACL", "LOG")
    return [e for e in entries if _entry(e).get("username") == user]


def _entry(raw) -> dict:
    items = raw if isinstance(raw, list) else list(itertools.chain(*raw.items()))
    decoded = [i.decode() if isinstance(i, bytes) else i for i in items]
    return dict(zip(decoded[::2], decoded[1::2], strict=True))


@pytest.fixture
async def bus(admin: Redis):
    client = await _setuser(admin, USER, GRANTS)
    yield client
    await client.aclose()
    denials = await _denials(admin, USER)
    await admin.execute_command("ACL", "DELUSER", USER)
    assert denials == [], f"the consumer issued an ungranted command: {denials}"


class Clock:
    def __init__(self) -> None:
        self.ticks = itertools.count()

    def __call__(self) -> datetime:
        return T0 + timedelta(seconds=next(self.ticks))


@pytest.fixture
def stores(tmp_path: Path) -> Stores:
    return Stores(input=LocalBlobStore(tmp_path / "in"), output=LocalBlobStore(tmp_path / "out"))


def make_consumer(client: Redis, stores: Stores, *, run_child=run_in_child, publish=None,
                  name: str = "co-processor") -> Consumer:  # fmt: skip
    deps = Deps(
        stores=stores,
        publish=publish or AsyncBusPublisher(client).execute,
        run_child=run_child,
        clock=Clock(),
        extraction_timeout_s=60,
        rlimit_as_bytes=3 * GiB,
        max_attempts=3,
    )
    # min-idle 0 and a zero interval: every step reclaims whatever is pending.
    return Consumer(client, deps, consumer_name=name, read_block_ms=100,
                    reclaim_min_idle_ms=0, reclaim_interval_s=0)  # fmt: skip


async def issue(admin: Redis, stores: Stores, command_id: str = "cmd-1") -> str:
    """A test issuer: store the raw blob and XADD a real content.process command."""
    digest = hashlib.sha256(HTML).hexdigest()
    stores.input.store(HTML, digest, "text/html")
    command = ContentProcessCommandEmit(
        occurred_at=T0,
        command_id=command_id,
        info_source_id="src-1",
        input_uri=stores.input.uri_for(digest),
        input_digest=digest,
        processor="extract",
        source_spec=SPEC,
        media_type="text/html",
    )
    return (await admin.xadd(CONTENT_PROCESS, to_wire(command))).decode()


async def facts(admin: Redis) -> list:
    entries = await admin.xrange(CONTENT_DERIVED)
    return [
        from_wire({k.decode(): v.decode() for k, v in f.items()}, topic=CONTENT_DERIVED).payload
        for _id, f in entries
    ]


async def group_info(admin: Redis) -> dict:
    (info,) = await admin.xinfo_groups(CONTENT_PROCESS)
    return {(k.decode() if isinstance(k, bytes) else k): v for k, v in info.items()}


async def test_start_creates_the_stream_and_group_idempotently(admin, bus, stores) -> None:
    consumer = make_consumer(bus, stores)
    await consumer.start()
    await consumer.start()
    assert await admin.xlen(CONTENT_PROCESS) == 0
    info = await group_info(admin)
    assert (info["name"], info["pending"]) == (GROUP.encode(), 0)
    assert GROUP == "processor.process"


async def test_a_command_becomes_one_fact_and_is_acked(admin, bus, stores) -> None:
    consumer = make_consumer(bus, stores)
    await consumer.start()
    await issue(admin, stores)
    await consumer.step()

    (fact,) = await facts(admin)
    assert isinstance(fact, ProcessingCompleteEvent) and fact.command_id == "cmd-1"
    assert stores.output.exists(fact.output_digest.removeprefix("sha256:"))
    info = await group_info(admin)
    assert (info["pending"], info["lag"]) == (0, 0)


async def test_an_undecodable_frame_is_dead_lettered(admin, bus, stores) -> None:
    consumer = make_consumer(bus, stores)
    await consumer.start()
    source_id = (await admin.xadd(CONTENT_PROCESS, {"event_type": "content_process"})).decode()
    await consumer.step()

    ((_id, raw),) = await admin.xrange(dlq_name(CONTENT_PROCESS))
    fields, meta = split_dead_letter({k.decode(): v.decode() for k, v in raw.items()})
    assert meta.source_id == source_id and "payload" in meta.reason
    assert fields == {"event_type": "content_process"}
    assert (await group_info(admin))["pending"] == 0
    assert await facts(admin) == []


async def test_a_foreign_event_is_dead_lettered(admin, bus, stores) -> None:
    consumer = make_consumer(bus, stores)
    await consumer.start()
    foreign = ProcessingFailedEmit(
        occurred_at=T0, command_id="x", info_source_id="y", reason="transient", terminal=False
    )
    await admin.xadd(CONTENT_PROCESS, to_wire(foreign))
    await consumer.step()
    assert await admin.xlen(dlq_name(CONTENT_PROCESS)) == 1
    assert (await group_info(admin))["pending"] == 0


async def test_three_strikes_then_a_terminal_failure(admin, bus, stores) -> None:
    async def timeout(*_a, **_k) -> ChildResult:
        return ChildResult(kind="timeout", detail="no result after 60s")

    consumer = make_consumer(bus, stores, run_child=timeout)
    await consumer.start()
    await issue(admin, stores)
    for _ in range(2):
        await consumer.step()
        assert await facts(admin) == []
        assert (await group_info(admin))["pending"] == 1
    await consumer.step()

    (fact,) = await facts(admin)
    assert isinstance(fact, ProcessingFailedEvent)
    assert (fact.reason, fact.terminal) == ("extraction_error", True)
    assert "attempt 3" in fact.detail
    assert (await group_info(admin))["pending"] == 0


async def test_publish_failures_are_uncapped_and_converge(admin, bus, stores) -> None:
    publisher = AsyncBusPublisher(bus)
    broken = {"on": True}

    async def publish(effect):
        if broken["on"]:
            raise RedisConnectionError("broker unreachable")
        return await publisher.execute(effect)

    consumer = make_consumer(bus, stores, publish=publish)
    await consumer.start()
    await issue(admin, stores)
    for _ in range(4):  # past max_attempts: a broker outage is not a strike
        await consumer.step()
    assert await facts(admin) == [] and (await group_info(admin))["pending"] == 1

    broken["on"] = False
    await consumer.step()
    (fact,) = await facts(admin)
    assert isinstance(fact, ProcessingCompleteEvent)
    assert (await group_info(admin))["pending"] == 0


async def test_an_ack_failure_yields_a_duplicate_fact(admin, bus, stores, monkeypatch) -> None:
    consumer = make_consumer(bus, stores)
    await consumer.start()
    await issue(admin, stores)
    real_ack = AsyncBusConsumer.ack
    calls = itertools.count()

    async def flaky_ack(self, message_id):
        if next(calls) == 0:
            raise RedisConnectionError("lost after the publish")
        return await real_ack(self, message_id)

    monkeypatch.setattr(AsyncBusConsumer, "ack", flaky_ack)
    with pytest.raises(RedisConnectionError):
        await consumer.step()
    await consumer.step()

    first, second = await facts(admin)
    assert first.command_id == second.command_id == "cmd-1"
    assert first.output_digest == second.output_digest
    assert first.occurred_at != second.occurred_at  # distinct envelope keys: Watcher upserts
    assert (await group_info(admin))["pending"] == 0


async def test_a_new_consumer_reclaims_a_dead_ones_entries(admin, bus, stores) -> None:
    dead = AsyncBusConsumer(bus, topic=CONTENT_PROCESS, group=GROUP, consumer="old-instance")
    await dead.ensure_group()
    await issue(admin, stores)
    assert len(await dead.read(count=1)) == 1  # delivered, then the process died

    await make_consumer(bus, stores, name="co-processor").step()
    (fact,) = await facts(admin)
    assert isinstance(fact, ProcessingCompleteEvent)
    assert (await group_info(admin))["pending"] == 0


async def test_run_stops_promptly(bus, stores) -> None:
    consumer = make_consumer(bus, stores)
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_a_broker_name_that_does_not_resolve_yet_is_retried(
    admin, bus, stores, monkeypatch, caplog
) -> None:
    # The bus URL names `broker` (#8). At boot MagicDNS answers about 2 s after
    # tailscaled starts (replicator#88), so the first lookups can fail: the loop
    # backs off as from any broker fault and starts once the name resolves.
    # Never `broker` itself: since #8 it resolves here to the real broker, so a
    # resolver path that bypassed this patch would reach it. `.invalid` never
    # resolves (RFC 6761), with or without the tailnet's search domain.
    host = "broker.invalid"
    real_getaddrinfo = socket.getaddrinfo
    lookups = itertools.count()

    def getaddrinfo(name, *args, **kwargs):
        if name != host:
            return real_getaddrinfo(name, *args, **kwargs)
        if next(lookups) < 2:
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        return real_getaddrinfo("localhost", *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(consumer_module, "_BACKOFF_START_S", 0.01)
    caplog.set_level(logging.WARNING, logger="processor.consumer")
    # `bus` holds the ACL user and its denial check; this client reaches it by name.
    client = redis_client(f"redis://{USER}:{PASSWORD}@{host}:6379/{DB}", read_block_ms=100)
    consumer = make_consumer(client, stores)
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        async with asyncio.timeout(10):  # no pytest-timeout: a loop that never starts fails
            while not await admin.exists(CONTENT_PROCESS):
                assert not task.done(), "the loop exited on an unresolved name"
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        await client.aclose()
    assert (await group_info(admin))["name"] == GROUP.encode()
    retries = [r for r in caplog.records if r.getMessage() == "consumer loop error; backing off"]
    assert len(retries) == 2
    # Logged as a broker fault (a warning), not as a bug with its traceback.
    assert {r.levelno for r in retries} == {logging.WARNING}


async def test_stop_during_a_reclaim_finishes_only_the_in_flight_command(
    admin, bus, stores
) -> None:
    # A restart after a crash reclaims a backlog. SIGTERM mid-reclaim must not wait
    # for the whole backlog: that outlasts TimeoutStopSec and ends in SIGKILL.
    dead = AsyncBusConsumer(bus, topic=CONTENT_PROCESS, group=GROUP, consumer="old-instance")
    await dead.ensure_group()
    await issue(admin, stores, "cmd-1")
    await issue(admin, stores, "cmd-2")
    assert len(await dead.read(count=2)) == 2
    stop = asyncio.Event()

    async def run_child(*args, **kwargs) -> ChildResult:
        stop.set()  # SIGTERM arrives while the first reclaimed command runs
        return await run_in_child(*args, **kwargs)

    consumer = make_consumer(bus, stores, run_child=run_child)
    await asyncio.wait_for(consumer.run(stop), timeout=30)
    (fact,) = await facts(admin)
    assert fact.command_id == "cmd-1"
    assert (await group_info(admin))["pending"] == 1  # cmd-2: left for the next reclaim


async def test_an_entry_that_raises_does_not_strand_the_ones_behind_it(
    admin, bus, stores, monkeypatch
) -> None:
    # A claim resets idle clocks. Had the reclaim claimed both entries at once, the
    # ack failure on cmd-1 would leave cmd-2 claimed but unrun until the next walk
    # past reclaim_min_idle_ms, where cmd-1 comes first again.
    dead = AsyncBusConsumer(bus, topic=CONTENT_PROCESS, group=GROUP, consumer="old-instance")
    await dead.ensure_group()
    await issue(admin, stores, "cmd-1")
    await issue(admin, stores, "cmd-2")
    assert len(await dead.read(count=2)) == 2
    await asyncio.sleep(0.6)

    async def inprocess(_target, args, **_kwargs) -> ChildResult:
        return ChildResult(kind="ok", value=extract(*args))

    real_ack = AsyncBusConsumer.ack
    calls = itertools.count()

    async def flaky_ack(self, message_id):
        if next(calls) == 0:
            raise RedisConnectionError("lost after the publish")
        return await real_ack(self, message_id)

    monkeypatch.setattr(AsyncBusConsumer, "ack", flaky_ack)
    deps = make_consumer(bus, stores, run_child=inprocess)._deps
    consumer = Consumer(bus, deps, consumer_name="co-processor", read_block_ms=100,
                        reclaim_min_idle_ms=500, reclaim_interval_s=0)  # fmt: skip
    with pytest.raises(RedisConnectionError):
        await consumer.step()
    await consumer.step()
    assert [f.command_id for f in await facts(admin)] == ["cmd-1", "cmd-2"]


async def test_an_escaping_exception_strikes_then_dead_letters(
    admin, bus, stores, monkeypatch
) -> None:
    # A bug outside the handler's own try (logging, building the failure fact) must
    # not retry forever: it counts, and at the cap the entry goes to the DLQ — with a
    # terminal fact first, so Watcher is not left waiting on a command (#17).
    async def broken_handle(*_args, **_kwargs):
        raise KeyError("a bug outside the handler's try")

    monkeypatch.setattr("processor.consumer.handle", broken_handle)
    consumer = make_consumer(bus, stores)
    await consumer.start()
    source_id = await issue(admin, stores)
    for _ in range(2):
        with pytest.raises(KeyError):
            await consumer.step()
        assert (await group_info(admin))["pending"] == 1
    await consumer.step()

    ((_id, raw),) = await admin.xrange(dlq_name(CONTENT_PROCESS))
    _fields, meta = split_dead_letter({k.decode(): v.decode() for k, v in raw.items()})
    assert meta.source_id == source_id
    assert "KeyError" in meta.reason and "attempt 3" in meta.reason
    assert (await group_info(admin))["pending"] == 0
    (fact,) = await facts(admin)
    assert isinstance(fact, ProcessingFailedEvent)
    assert (fact.command_id, fact.reason, fact.terminal) == ("cmd-1", "extraction_error", True)
    assert fact.detail.startswith("dead-lettered: gave up on attempt 3: KeyError")


async def test_a_non_transient_publish_failure_dead_letters_at_the_cap(admin, bus, stores) -> None:
    publisher = AsyncBusPublisher(bus)

    async def publish(effect):
        await publisher.execute(effect)  # would land, but the reply says otherwise
        raise ResponseError("WRONGTYPE Operation against a key holding the wrong kind")

    consumer = make_consumer(bus, stores, publish=publish)
    await consumer.start()
    await issue(admin, stores)
    for _ in range(2):
        with pytest.raises(ResponseError):
            await consumer.step()
    await consumer.step()
    ((_id, raw),) = await admin.xrange(dlq_name(CONTENT_PROCESS))
    _fields, meta = split_dead_letter({k.decode(): v.decode() for k, v in raw.items()})
    assert "gave up on attempt 3" in meta.reason and "WRONGTYPE" in meta.reason
    assert (await group_info(admin))["pending"] == 0


async def test_a_refused_failure_fact_still_dead_letters(admin, bus, stores, caplog) -> None:
    # The WRONGTYPE that caused the cap refuses the failure fact too: log, then DLQ.
    async def publish(_effect):
        raise ResponseError("WRONGTYPE Operation against a key holding the wrong kind")

    consumer = make_consumer(bus, stores, publish=publish)
    await consumer.start()
    await issue(admin, stores)
    caplog.set_level(logging.INFO, logger="processor.consumer")
    for _ in range(2):
        with pytest.raises(ResponseError):
            await consumer.step()
    await consumer.step()

    assert await admin.xlen(dlq_name(CONTENT_PROCESS)) == 1
    assert (await group_info(admin))["pending"] == 0
    (refused,) = [r for r in caplog.records if r.getMessage().startswith("failure fact refused")]
    assert refused.command_id == "cmd-1" and "WRONGTYPE" in refused.error


async def test_a_transiently_refused_failure_fact_leaves_the_entry_pending(
    admin, bus, stores, monkeypatch
) -> None:
    async def broken_handle(*_args, **_kwargs):
        raise KeyError("a bug outside the handler's try")

    monkeypatch.setattr("processor.consumer.handle", broken_handle)
    publisher = AsyncBusPublisher(bus)
    calls = itertools.count()

    async def flaky_publish(effect):
        if next(calls) == 0:
            raise RedisConnectionError("broker unreachable")
        return await publisher.execute(effect)

    consumer = make_consumer(bus, stores, publish=flaky_publish)
    await consumer.start()
    await issue(admin, stores)
    for _ in range(2):
        with pytest.raises(KeyError):
            await consumer.step()
    with pytest.raises(RedisConnectionError):
        await consumer.step()
    assert (await group_info(admin))["pending"] == 1
    assert await admin.xlen(dlq_name(CONTENT_PROCESS)) == 0
    await consumer.step()  # still at the cap

    assert await admin.xlen(dlq_name(CONTENT_PROCESS)) == 1
    assert [f.reason for f in await facts(admin)] == ["extraction_error"]


async def test_a_failure_fact_is_published_once_when_the_dead_letter_is_retried(
    admin, bus, stores, monkeypatch
) -> None:
    async def broken_handle(*_args, **_kwargs):
        raise KeyError("a bug outside the handler's try")

    monkeypatch.setattr("processor.consumer.handle", broken_handle)
    real_dead_letter = AsyncBusConsumer.dead_letter
    calls = itertools.count()

    async def flaky_dead_letter(self, *args, **kwargs):
        if next(calls) == 0:
            raise RedisConnectionError("broker unreachable")
        return await real_dead_letter(self, *args, **kwargs)

    monkeypatch.setattr(AsyncBusConsumer, "dead_letter", flaky_dead_letter)
    consumer = make_consumer(bus, stores)
    await consumer.start()
    await issue(admin, stores)
    for _ in range(2):
        with pytest.raises(KeyError):
            await consumer.step()
    with pytest.raises(RedisConnectionError):
        await consumer.step()
    await consumer.step()

    assert await admin.xlen(dlq_name(CONTENT_PROCESS)) == 1
    assert [f.reason for f in await facts(admin)] == ["extraction_error"]


async def test_a_refused_ack_at_the_cap_adds_no_failure_fact(
    admin, bus, stores, monkeypatch
) -> None:
    # Each attempt's complete fact went out before its ack was refused: a terminal
    # failure on top would contradict them.
    real_ack = AsyncBusConsumer.ack
    calls = itertools.count()

    async def refused_ack(self, message_id):
        if next(calls) < 3:  # each attempt's ack; dead_letter's own ack lands
            raise ResponseError("NOGROUP No such key or consumer group")
        return await real_ack(self, message_id)

    monkeypatch.setattr(AsyncBusConsumer, "ack", refused_ack)
    consumer = make_consumer(bus, stores)
    await consumer.start()
    await issue(admin, stores)
    for _ in range(2):
        with pytest.raises(ResponseError):
            await consumer.step()
    await consumer.step()

    assert await admin.xlen(dlq_name(CONTENT_PROCESS)) == 1
    assert {type(f).__name__ for f in await facts(admin)} == {"ProcessingCompleteEvent"}


async def test_a_transient_escape_is_never_dead_lettered(admin, bus, stores, monkeypatch) -> None:
    consumer = make_consumer(bus, stores)
    await consumer.start()
    await issue(admin, stores)
    real_ack = AsyncBusConsumer.ack
    calls = itertools.count()

    async def flaky_ack(self, message_id):
        if next(calls) < 5:  # past max_attempts: a broker fault is not a strike
            raise RedisConnectionError("broker unreachable")
        return await real_ack(self, message_id)

    monkeypatch.setattr(AsyncBusConsumer, "ack", flaky_ack)
    for _ in range(5):
        with pytest.raises(RedisConnectionError):
            await consumer.step()
    await consumer.step()
    assert await admin.xlen(dlq_name(CONTENT_PROCESS)) == 0
    assert (await group_info(admin))["pending"] == 0
    assert {f.command_id for f in await facts(admin)} == {"cmd-1"}  # duplicates, one command


async def test_a_failed_ack_at_the_cap_keeps_the_count(admin, bus, stores, monkeypatch) -> None:
    # The strike count clears only once the ack lands: a broker blip on the last
    # attempt's ack must not buy the command max_attempts more child runs.
    async def timeout(*_a, **_k) -> ChildResult:
        return ChildResult(kind="timeout", detail="no result after 60s")

    consumer = make_consumer(bus, stores, run_child=timeout)
    await consumer.start()
    await issue(admin, stores)
    real_ack = AsyncBusConsumer.ack
    calls = itertools.count()

    async def flaky_ack(self, message_id):
        if next(calls) == 0:
            raise RedisConnectionError("lost after the publish")
        return await real_ack(self, message_id)

    monkeypatch.setattr(AsyncBusConsumer, "ack", flaky_ack)
    for _ in range(2):
        await consumer.step()
    with pytest.raises(RedisConnectionError):
        await consumer.step()
    await consumer.step()

    assert (await group_info(admin))["pending"] == 0
    assert [(f.reason, "attempt 3" in f.detail) for f in await facts(admin)] == [
        ("extraction_error", True),
        ("extraction_error", True),
    ]


async def test_a_failed_dead_letter_at_the_cap_keeps_the_count(
    admin, bus, stores, monkeypatch
) -> None:
    async def broken_handle(*_args, **_kwargs):
        raise KeyError("a bug outside the handler's try")

    monkeypatch.setattr("processor.consumer.handle", broken_handle)
    real_dead_letter = AsyncBusConsumer.dead_letter
    calls = itertools.count()

    async def flaky_dead_letter(self, *args, **kwargs):
        if next(calls) == 0:
            raise RedisConnectionError("broker unreachable")
        return await real_dead_letter(self, *args, **kwargs)

    monkeypatch.setattr(AsyncBusConsumer, "dead_letter", flaky_dead_letter)
    consumer = make_consumer(bus, stores)
    await consumer.start()
    await issue(admin, stores)
    for _ in range(2):
        with pytest.raises(KeyError):
            await consumer.step()
    with pytest.raises(RedisConnectionError):
        await consumer.step()
    await consumer.step()  # still at the cap: straight to the DLQ, not attempt 1 again

    assert await admin.xlen(dlq_name(CONTENT_PROCESS)) == 1
    assert (await group_info(admin))["pending"] == 0


async def test_an_escape_logs_its_command(admin, bus, stores, monkeypatch, caplog) -> None:
    # Every outcome logs command_id and info_source_id (spec §4), escapes included.
    async def broken_handle(*_args, **_kwargs):
        raise KeyError("a bug outside the handler's try")

    monkeypatch.setattr("processor.consumer.handle", broken_handle)
    consumer = make_consumer(bus, stores)
    await consumer.start()
    await issue(admin, stores)
    caplog.set_level(logging.INFO, logger="processor.consumer")
    for _ in range(2):
        with pytest.raises(KeyError):
            await consumer.step()
    await consumer.step()

    outcomes = [r for r in caplog.records if getattr(r, "command_id", None) == "cmd-1"]
    assert [(r.action, r.attempt) for r in outcomes] == [
        ("strike", 1),
        ("strike", 2),
        ("dead_letter", 3),
    ]
    assert all(r.info_source_id == "src-1" for r in outcomes)
    assert outcomes[-1].exc_info is not None  # the last attempt's traceback survives


async def test_noperm_leaves_the_entry_pending(admin, stores) -> None:
    narrow = [g for g in GRANTS if not g.startswith("(+xadd")]
    narrow.append("(+xadd ~content.process.dlq)")  # can dead-letter, cannot publish facts
    client = await _setuser(admin, "processor-narrow", narrow)
    try:
        consumer = make_consumer(client, stores)
        await consumer.start()
        await issue(admin, stores)
        for _ in range(4):
            await consumer.step()
        assert await admin.xlen(dlq_name(CONTENT_PROCESS)) == 0
        assert (await group_info(admin))["pending"] == 1
        (denial, *_) = await _denials(admin, "processor-narrow")
        entry = _entry(denial)
        assert (entry["reason"], entry["object"]) == ("key", CONTENT_DERIVED)
    finally:
        await client.aclose()
        await admin.execute_command("ACL", "DELUSER", "processor-narrow")


async def test_oom_leaves_the_entry_pending(admin, bus, stores) -> None:
    consumer = make_consumer(bus, stores)
    await consumer.start()
    await issue(admin, stores)
    saved = await admin.config_get("maxmemory*")
    try:
        await admin.config_set("maxmemory-policy", "noeviction")
        await admin.config_set("maxmemory", 1)
        for _ in range(4):
            await consumer.step()
        assert (await group_info(admin))["pending"] == 1
    finally:
        await admin.config_set("maxmemory", saved["maxmemory"])
        await admin.config_set("maxmemory-policy", saved["maxmemory-policy"])
    await consumer.step()
    (fact,) = await facts(admin)
    assert isinstance(fact, ProcessingCompleteEvent)
