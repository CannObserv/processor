"""The consumer loop: one serial consumer in ``processor.process`` (spec §3, §4).

co-core-aio's ``AsyncBusConsumer`` supplies primitives only — no delivery count, no
max-deliveries hook, no loop (spec Open Question 2) — so the loop is here:

- ``start``: ``ensure_group`` from ``$`` with ``MKSTREAM`` (idempotent).
- ``step``: reclaim when due (``XAUTOCLAIM`` past ``reclaim_min_idle_ms``, which also
  picks up a dead predecessor's entries), then one blocking ``read(count=1)``.
- Each message goes through ``handler.handle``; the loop acts on its disposition and
  keeps the in-memory strike count per stream entry id. A restart resets the count:
  a poison command then gets at most ``max_attempts`` more.
- A frame that does not decode is dead-lettered with its raw fields.
- ``run``: ``step`` until stopped, backing off on any exception (connection loss,
  ``NOPERM``, ``OOM``), never exiting on one.
"""

import asyncio
import logging
import time

from co_core.effects.bus import BusMessage
from co_core.pure.adapters.bus.exceptions import BusMessageAnomaly
from co_core.pure.adapters.bus.streams import CONTENT_PROCESS, group_name
from co_core_aio.bus import AsyncBusConsumer
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from processor.errors import is_transient
from processor.handler import Deps, Disposition, handle

GROUP = group_name(CONTENT_PROCESS, "processor")

_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 30.0

logger = logging.getLogger(__name__)

_LEVEL = {
    "ack": logging.INFO,
    "strike": logging.WARNING,
    "leave_pending": logging.WARNING,
    "dead_letter": logging.ERROR,
}


def redis_client(url: str, *, read_block_ms: int) -> Redis:
    """A broker client: the socket timeout outlasts a blocking read; retries are zero.

    A client-side retry re-sends the command — a second ``XADD`` of a fact whose
    reply was lost. The reclaim is the retry mechanism here, as in the cohort's
    connection policy (watcher#287).
    """
    return Redis.from_url(
        url,
        socket_timeout=read_block_ms / 1000 + 10,
        socket_connect_timeout=10,
        retry=Retry(NoBackoff(), 0),
        health_check_interval=30,
    )


class Consumer:
    """The serial ``content.process`` consumer."""

    def __init__(
        self,
        client: Redis,
        deps: Deps,
        *,
        consumer_name: str,
        read_block_ms: int,
        reclaim_min_idle_ms: int,
        reclaim_interval_s: float,
    ) -> None:
        self._client = client
        self._bus = AsyncBusConsumer(
            client, topic=CONTENT_PROCESS, group=GROUP, consumer=consumer_name
        )
        self._deps = deps
        self._read_block_ms = read_block_ms
        self._reclaim_min_idle_ms = reclaim_min_idle_ms
        self._reclaim_interval_s = reclaim_interval_s
        self._next_reclaim = 0.0
        self._strikes: dict[str, int] = {}

    async def start(self) -> None:
        """Create ``processor.process`` from ``$`` if it does not exist."""
        await self._bus.ensure_group(start_id="$")

    async def run(self, stop: asyncio.Event) -> None:
        """Consume until ``stop`` is set; the current message always finishes."""
        backoff = _BACKOFF_START_S
        started = False
        while not stop.is_set():
            try:
                if not started:
                    await self.start()
                    started = True
                    logger.info("consuming", extra={"group": GROUP, "stream": CONTENT_PROCESS})
                await self.step()
                backoff = _BACKOFF_START_S
            except Exception as exc:
                log = logger.warning if is_transient(exc) else logger.exception
                log(
                    "consumer loop error; backing off",
                    extra={"error": f"{type(exc).__name__}: {exc}", "backoff_s": backoff},
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def step(self) -> None:
        """Reclaim if due, then read and process at most one new message."""
        if time.monotonic() >= self._next_reclaim:
            await self.reclaim()
            self._next_reclaim = time.monotonic() + self._reclaim_interval_s
        try:
            messages = await self._bus.read(count=1, block_ms=self._read_block_ms)
        except BusMessageAnomaly as exc:
            await self._dead_letter_unread(exc)
            return
        for message in messages:
            await self._process(message)

    async def reclaim(self) -> None:
        """Walk the whole PEL once, claiming entries idle past the threshold."""
        cursor = "0-0"
        while True:
            page = await self._bus.claim_stale_page(
                min_idle_ms=self._reclaim_min_idle_ms, count=10, start_id=cursor
            )
            for frame in page.poison:
                await self._dead_letter(frame.message_id, dict(frame.fields), frame.anomaly)
            for message in page.messages:
                await self._process(message)
            for message_id in page.deleted:
                self._strikes.pop(message_id, None)
            cursor = page.cursor
            if cursor == "0-0":
                return

    async def _process(self, message: BusMessage) -> None:
        message_id = message.message_id
        attempt = self._strikes.get(message_id, 0) + 1
        disposition = await handle(message, attempt=attempt, deps=self._deps)
        _log(disposition, message_id, attempt)

        if disposition.action == "strike":
            self._strikes[message_id] = attempt
        elif disposition.action == "dead_letter":
            self._strikes.pop(message_id, None)
            await self._bus.dead_letter(message_id, dict(message.fields), reason=disposition.reason)
        elif disposition.action == "ack":
            self._strikes.pop(message_id, None)
            try:
                await self._bus.ack(message_id)
            except Exception:
                logger.error(
                    "ack failed after the fact was published; the reclaim will re-run "
                    "the command and publish a duplicate fact",
                    extra={"message_id": message_id, "command_id": disposition.command_id},
                )
                raise

    async def _dead_letter_unread(self, anomaly: BusMessageAnomaly) -> None:
        # read(count=1) raised on this one frame; it is in our PEL. Its raw fields
        # are not on the anomaly, so fetch them to keep them in the DLQ.
        message_id = anomaly.message_id
        entries = await self._client.xrange(CONTENT_PROCESS, min=message_id, max=message_id)
        fields = {_str(k): _str(v) for k, v in entries[0][1].items()} if entries else {}
        await self._dead_letter(message_id, fields, anomaly)

    async def _dead_letter(
        self, message_id: str, fields: dict[str, str], anomaly: BusMessageAnomaly
    ) -> None:
        reason = f"undecodable: {type(anomaly).__name__}: {anomaly}"
        logger.error("dead-lettering", extra={"message_id": message_id, "reason": reason})
        self._strikes.pop(message_id, None)
        await self._bus.dead_letter(message_id, fields, reason=reason)


def _str(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _log(disposition: Disposition, message_id: str, attempt: int) -> None:
    level = _LEVEL[disposition.action]
    if disposition.action == "ack" and disposition.reason != "complete":
        level = logging.WARNING  # a terminal failure fact
    logger.log(
        level,
        f"{disposition.action}: {disposition.reason}",
        extra={
            "message_id": message_id,
            "attempt": attempt,
            "action": disposition.action,
            "reason": disposition.reason,
            "detail": disposition.detail,
            "command_id": disposition.command_id,
            "info_source_id": disposition.info_source_id,
            **disposition.timings,
        },
    )
