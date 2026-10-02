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
- An exception escaping a message (a bug outside the handler's own try, or a
  non-transient refusal of an ack or dead-letter) counts like a strike; at
  ``max_attempts`` the entry is dead-lettered with the exception as its reason.
  Transient escapes (broker, GCS) stay uncapped. Before that dead-letter, a
  decodable command whose fact never went out gets a terminal ``extraction_error``
  (#17), so Watcher closes it instead of waiting; a refused one is logged and the
  entry dead-lettered anyway.
- A strike count clears only once the entry's ack or dead-letter lands, so a failed
  one on the last attempt does not restart the count.
- ``run``: ``step`` until stopped, backing off on any exception (connection loss,
  ``NOPERM``, ``OOM``), never exiting on one. A stop also ends a reclaim between
  messages: the in-flight command finishes, and the rest of a backlog stays pending
  for the next process rather than outlasting the unit's ``TimeoutStopSec``.
"""

import asyncio
import logging
import time

from co_core.effects.bus import BusMessage
from co_core.pure.adapters.bus.exceptions import BusMessageAnomaly
from co_core.pure.adapters.bus.streams import CONTENT_PROCESS, group_name
from co_core.pure.models.changes import ContentProcessCommand
from co_core_aio.bus import AsyncBusConsumer
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from processor.errors import is_transient
from processor.handler import Deps, Disposition, handle, publish_gave_up

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


def group_reader(client: Redis, consumer_name: str) -> AsyncBusConsumer:
    """``consumer_name``'s reader in ``processor.process`` on ``content.process``."""
    return AsyncBusConsumer(client, topic=CONTENT_PROCESS, group=GROUP, consumer=consumer_name)


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
        self._bus = group_reader(client, consumer_name)
        self._deps = deps
        self._read_block_ms = read_block_ms
        self._reclaim_min_idle_ms = reclaim_min_idle_ms
        self._reclaim_interval_s = reclaim_interval_s
        self._next_reclaim = 0.0
        self._strikes: dict[str, int] = {}
        # Entries whose fact is on content.derived but not yet acked: at the cap they
        # get no failure fact on top (a refused ack, or a dead-letter retried).
        self._fact_out: set[str] = set()
        self._stop: asyncio.Event | None = None

    async def start(self) -> None:
        """Create ``processor.process`` from ``$`` if it does not exist."""
        await self._bus.ensure_group(start_id="$")

    async def run(self, stop: asyncio.Event) -> None:
        """Consume until ``stop`` is set; the current message always finishes."""
        self._stop = stop
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
        if self._stopping():
            return
        try:
            messages = await self._bus.read(count=1, block_ms=self._read_block_ms)
        except BusMessageAnomaly as exc:
            await self._dead_letter_unread(exc)
            return
        for message in messages:
            await self._process(message)

    async def reclaim(self) -> None:
        """Walk the whole PEL once, claiming entries idle past the threshold.

        One entry per claim: a claim resets the entry's idle clock, so a batch would
        leave its tail claimed but unrun for as long as the head takes — past the
        reclaim threshold, and, if the head raises, until the next walk claims the
        whole batch again behind it.
        """
        cursor = "0-0"
        while True:
            page = await self._bus.claim_stale_page(
                min_idle_ms=self._reclaim_min_idle_ms, count=1, start_id=cursor
            )
            for frame in page.poison:
                await self._dead_letter(
                    frame.message_id, dict(frame.fields), _undecodable(frame.anomaly)
                )
            for message in page.messages:
                if self._stopping():
                    return  # claimed, not run: pending until the next reclaim
                await self._process(message)
            for message_id in page.deleted:
                self._strikes.pop(message_id, None)
                self._fact_out.discard(message_id)
            cursor = page.cursor
            if cursor == "0-0":
                return

    def _stopping(self) -> bool:
        return self._stop is not None and self._stop.is_set()

    async def _process(self, message: BusMessage) -> None:
        message_id = message.message_id
        attempt = self._strikes.get(message_id, 0) + 1
        try:
            await self._act(message, attempt)
        except Exception as exc:
            if is_transient(exc):
                raise  # a broker or GCS fault: uncapped, the loop backs off
            # A bug outside the handler's own try, or a non-transient refusal of an
            # ack or dead-letter. Counted like a strike; at the cap the entry goes to
            # the DLQ, since it could not even fail cleanly. Nothing more is published,
            # but a fact published before a refused ack stands (one per attempt).
            detail = f"{type(exc).__name__}: {exc}"
            ids = {"attempt": attempt, **_command_ids(message)}
            if attempt < self._deps.max_attempts:
                self._strikes[message_id] = attempt
                logger.warning(
                    "strike: escaped",
                    extra={
                        "message_id": message_id,
                        "action": "strike",
                        "reason": "escaped",
                        "detail": detail,
                        **ids,
                    },
                )
                raise
            reason = f"gave up on attempt {attempt}: {detail}"
            await self._publish_gave_up(message, reason, ids)
            await self._dead_letter(message_id, dict(message.fields), reason, exc_info=True, **ids)

    async def _publish_gave_up(
        self, message: BusMessage, reason: str, ids: dict[str, object]
    ) -> None:
        command = message.payload
        if not isinstance(command, ContentProcessCommand) or message.message_id in self._fact_out:
            return
        try:
            await publish_gave_up(command, self._deps, f"dead-lettered: {reason}")
        except Exception as exc:
            if is_transient(exc):
                raise  # pending, uncapped; the next attempt is the cap again
            logger.error(
                "failure fact refused; dead-lettering without it",
                extra={
                    "message_id": message.message_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    **ids,
                },
            )
            return
        self._fact_out.add(message.message_id)

    async def _act(self, message: BusMessage, attempt: int) -> None:
        message_id = message.message_id
        disposition = await handle(message, attempt=attempt, deps=self._deps)
        _log(disposition, message_id, attempt)

        if disposition.action == "strike":
            self._strikes[message_id] = attempt
        elif disposition.action == "dead_letter":
            await self._bus.dead_letter(message_id, dict(message.fields), reason=disposition.reason)
            self._strikes.pop(message_id, None)
        elif disposition.action == "ack":
            self._fact_out.add(message_id)
            try:
                await self._bus.ack(message_id)
            except Exception:
                logger.error(
                    "ack failed after the fact was published; a re-run publishes a duplicate fact",
                    extra={"message_id": message_id, "command_id": disposition.command_id},
                )
                raise
            self._strikes.pop(message_id, None)
            self._fact_out.discard(message_id)

    async def _dead_letter_unread(self, anomaly: BusMessageAnomaly) -> None:
        # read(count=1) raised on this one frame; it is in our PEL. Its raw fields
        # are not on the anomaly, so fetch them to keep them in the DLQ.
        message_id = anomaly.message_id
        entries = await self._client.xrange(CONTENT_PROCESS, min=message_id, max=message_id)
        fields = decode_fields(entries[0][1]) if entries else {}
        await self._dead_letter(message_id, fields, _undecodable(anomaly))

    async def _dead_letter(
        self,
        message_id: str,
        fields: dict[str, str],
        reason: str,
        *,
        exc_info: bool = False,
        **extra: object,
    ) -> None:
        fields_logged = {"message_id": message_id, "action": "dead_letter", "reason": reason}
        logger.error("dead-lettering", exc_info=exc_info, extra={**extra, **fields_logged})
        await self._bus.dead_letter(message_id, fields, reason=reason)
        self._strikes.pop(message_id, None)
        self._fact_out.discard(message_id)


def _undecodable(anomaly: BusMessageAnomaly) -> str:
    return f"undecodable: {type(anomaly).__name__}: {anomaly}"


def _command_ids(message: BusMessage) -> dict[str, str | None]:
    # None for a frame whose payload is not a command.
    payload = message.payload
    return {
        "command_id": getattr(payload, "command_id", None),
        "info_source_id": getattr(payload, "info_source_id", None),
    }


def decode_fields(raw: dict) -> dict[str, str]:
    """A raw stream entry's field map as strings, whether the client decodes or not."""
    return {as_str(k): as_str(v) for k, v in raw.items()}


def as_str(value: bytes | str) -> str:
    """A stream id, key or value as a string, whether the client decodes or not."""
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
