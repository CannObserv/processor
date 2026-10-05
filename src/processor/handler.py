"""One ``content.process`` command → at most one ``content.derived`` fact (spec §3, §4).

Route → validate and read the input → extract in the child → store → publish. The
handler decides; the consumer loop acts on the returned ``Disposition``:

- ``ack`` — a fact was published (complete, or a terminal failure).
- ``dead_letter`` — the frame is not a ``content_process`` command.
- ``leave_pending`` — infrastructure failed (``errors.is_transient``), the publish
  included: nothing published, the reclaim re-runs it. Uncapped. A non-transient
  publish failure escapes instead, and the consumer caps it: at the cap it
  publishes ``publish_gave_up``'s terminal fact, then dead-letters (#17).
- ``strike`` — the child timed out or crashed, or something unexpected raised:
  nothing published, counted toward ``max_attempts``; on the last attempt the
  handler publishes ``extraction_error`` instead and returns ``ack``.

A download that fails its checksum (``DataCorruption``) is ``input_unreadable``.

Order is store → publish → ack. A publish failure after a store leaves the entry
pending; the re-run finds the object already stored (write-if-absent) and publishes.
"""

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from co_core.effects.bus import BusMessage, BusPublish
from co_core.pure.adapters.bus.envelope import to_wire
from co_core.pure.adapters.bus.streams import CONTENT_DERIVED
from co_core.pure.extract.canonical import CANONICAL_TEXT_MEDIA_TYPE
from co_core.pure.models.changes import (
    ContentProcessCommand,
    ProcessingCompleteEmit,
    ProcessingFailedEmit,
    ProcessingFailureReason,
)
from co_core.pure.util.blobstore import validate_fingerprint
from co_core.pure.util.hashing import bare_sha256
from google.cloud.storage.exceptions import DataCorruption

from processor.child import ChildResult, transform_target
from processor.errors import is_transient
from processor.processors import TRANSFORMS
from processor.processors.extract import ExtractOutcome
from processor.stores import Stores

Action = Literal["ack", "dead_letter", "leave_pending", "strike"]

_DETAIL_MAX = 1000


@dataclass(frozen=True)
class Disposition:
    """What the consumer does with the entry, and what it logs."""

    action: Action
    reason: str  # "complete", a failure token, "transient", "strike", or a DLQ reason
    detail: str = ""
    command_id: str | None = None
    info_source_id: str | None = None
    timings: dict[str, float] = field(default_factory=dict)
    # What the command read and published, in wire form (#26): ``input_digest`` once
    # valid; from a complete fact, ``output_digest`` (absent when empty),
    # ``output_size_bytes``, ``empty`` and ``processor_version``.
    fields: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Deps:
    """Everything the handler touches, injected."""

    stores: Stores
    publish: Callable[[BusPublish], Awaitable[object]]
    run_child: Callable[..., Awaitable[ChildResult]]
    clock: Callable[[], datetime]
    extraction_timeout_s: float
    rlimit_as_bytes: int
    max_attempts: int


class _Terminal(Exception):
    """A deterministic failure: publish ``processing_failed`` with this reason, ack."""

    def __init__(self, reason: ProcessingFailureReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class _Strike(Exception):
    """A failure that may not recur: count it; fail terminally on the last attempt."""


class _Timer:
    def __init__(self) -> None:
        self.start = time.monotonic()
        self.timings: dict[str, float] = {}

    def lap(self, name: str, since: float) -> float:
        now = time.monotonic()
        self.timings[name] = round((now - since) * 1000, 1)
        return now

    def total(self) -> dict[str, float]:
        self.lap("total_ms", self.start)
        return self.timings


async def handle(message: BusMessage, *, attempt: int, deps: Deps) -> Disposition:
    """Decide one delivery. ``attempt`` is 1-based, counted by the consumer."""
    command = message.payload
    if not isinstance(command, ContentProcessCommand):
        return Disposition(
            action="dead_letter",
            reason=f"event_type {command.event_type!r} is not a content_process command",
        )

    timer = _Timer()
    ids = {"command_id": command.command_id, "info_source_id": command.info_source_id}
    fields = input_fields(command)
    try:
        fact = await _derive(command, deps, timer)
    except _Terminal as exc:
        fact = _failed(command, deps, exc.reason, exc.detail)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if is_transient(exc):
            return Disposition(
                "leave_pending", "transient", detail, timings=timer.total(), fields=fields, **ids
            )
        if attempt < deps.max_attempts:
            return Disposition(
                "strike", "strike", detail, timings=timer.total(), fields=fields, **ids
            )
        fact = _failed(command, deps, "extraction_error", f"gave up on attempt {attempt}: {detail}")

    if isinstance(fact, ProcessingCompleteEmit):
        fields = {**fields, **_output_fields(fact)}

    since = time.monotonic()
    try:
        await deps.publish(BusPublish(topic=CONTENT_DERIVED, fields=to_wire(fact)))
    except Exception as exc:
        if not is_transient(exc):
            # WRONGTYPE, or a bug in to_wire: a retry never helps. It escapes, and
            # the consumer counts it like a strike and dead-letters at the cap.
            raise
        # Nothing published; the reclaim re-runs the command. Never a strike: the
        # work succeeded, and the broker is what failed.
        detail = f"publish failed: {type(exc).__name__}: {exc}"
        return Disposition(
            "leave_pending", "transient", detail, timings=timer.total(), fields=fields, **ids
        )
    timer.lap("publish_ms", since)

    reason = "complete" if isinstance(fact, ProcessingCompleteEmit) else fact.reason
    detail = "" if isinstance(fact, ProcessingCompleteEmit) else (fact.detail or "")
    return Disposition("ack", reason, detail, timings=timer.total(), fields=fields, **ids)


def input_fields(command: ContentProcessCommand) -> dict[str, str]:
    """``input_digest`` for an outcome record, once it is a valid fingerprint (#26).

    Before validation it is untrusted input. A malformed one is left out, and its
    ``invalid_input`` record's ``detail`` quotes it.
    """
    try:
        return {"input_digest": validate_fingerprint(command.input_digest)}
    except ValueError:
        return {}


def _output_fields(fact: ProcessingCompleteEmit) -> dict[str, object]:
    # From the fact, so the record says what was published. An empty one has no digest.
    digest = {} if fact.output_digest is None else {"output_digest": fact.output_digest}
    return digest | {
        "output_size_bytes": fact.output_size_bytes,
        "empty": fact.empty,
        "processor_version": fact.processor_version,
    }


async def _derive(
    command: ContentProcessCommand, deps: Deps, timer: _Timer
) -> ProcessingCompleteEmit:
    transform = TRANSFORMS.get(command.processor)
    if transform is None:
        raise _Terminal("unsupported_processor", f"no transform named {command.processor!r}")

    raw = await _read_input(command, deps, timer)

    since = time.monotonic()
    result = await deps.run_child(
        transform_target(transform),
        (raw, command.media_type, command.source_spec),
        timeout_s=deps.extraction_timeout_s,
        rlimit_as_bytes=deps.rlimit_as_bytes,
    )
    since = timer.lap("extract_ms", since)
    if result.kind == "raised":
        raise _Terminal("extraction_error", result.detail)
    if result.kind != "ok":
        raise _Strike(f"child {result.kind}: {result.detail}")
    outcome: ExtractOutcome = result.value
    if not isinstance(outcome, ExtractOutcome):
        raise _Strike(f"child returned {type(outcome).__name__}, not ExtractOutcome")

    output_uri = None
    if not outcome.empty:
        output_uri = await asyncio.to_thread(
            deps.stores.output.store,
            outcome.text,
            bare_sha256(outcome.output_digest),
            CANONICAL_TEXT_MEDIA_TYPE,
        )
    timer.lap("store_ms", since)

    return ProcessingCompleteEmit(
        occurred_at=deps.clock(),
        command_id=command.command_id,
        info_source_id=command.info_source_id,
        empty=outcome.empty,
        output_digest=outcome.output_digest,
        output_uri=output_uri,
        output_size_bytes=len(outcome.text),
        output_media_type=CANONICAL_TEXT_MEDIA_TYPE,
        spec_fingerprint=outcome.spec_fingerprint,
        spec_schema_version=outcome.spec_schema_version,
        processor_version=outcome.processor_version,
    )


async def _read_input(command: ContentProcessCommand, deps: Deps, timer: _Timer) -> bytes:
    since = time.monotonic()
    try:
        digest = validate_fingerprint(command.input_digest)
    except ValueError as exc:
        raise _Terminal("invalid_input", str(exc)) from exc
    store = deps.stores.input
    # Resolved by digest, never by treating input_uri as a path (cannobserv#486).
    if command.input_uri != store.uri_for(digest):
        raise _Terminal(
            "invalid_input", f"input_uri {command.input_uri!r} is not this input store's"
        )
    try:
        raw = await asyncio.to_thread(store.open, digest)
    except FileNotFoundError as exc:
        raise _Terminal("input_unreadable", f"no blob for {digest}") from exc
    except DataCorruption as exc:
        # The storage read failed its checksum, not the document: Watcher's re-fetch
        # rewrites a temp blob that really is corrupt, and is cheap if it was in transit.
        raise _Terminal("input_unreadable", f"checksum mismatch on download: {exc}") from exc
    timer.lap("read_ms", since)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != digest:
        raise _Terminal("input_digest_mismatch", f"bytes hash to {actual}")
    return raw


async def publish_gave_up(command: ContentProcessCommand, deps: Deps, detail: str) -> None:
    """Publish a terminal ``extraction_error`` for a command about to be dead-lettered.

    Without a fact, Watcher waits on the command: its reaper re-issues only once a
    later command is answered, and its health reads the silence as Processor down
    (#17, #20).
    Raises whatever the publish raises; the consumer decides.
    """
    fact = _failed(command, deps, "extraction_error", detail)
    await deps.publish(BusPublish(topic=CONTENT_DERIVED, fields=to_wire(fact)))


def _failed(
    command: ContentProcessCommand, deps: Deps, reason: ProcessingFailureReason, detail: str
) -> ProcessingFailedEmit:
    return ProcessingFailedEmit(
        occurred_at=deps.clock(),
        command_id=command.command_id,
        info_source_id=command.info_source_id,
        reason=reason,
        terminal=True,
        detail=detail[:_DETAIL_MAX],
    )
