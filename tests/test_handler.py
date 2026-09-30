"""The per-message handler: spec §4's failure table, one test per row, plus ordering."""

import hashlib
import importlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from co_core.effects.bus import BusMessage, BusPublish
from co_core.pure.adapters.bus.envelope import from_wire
from co_core.pure.adapters.bus.streams import CONTENT_DERIVED, CONTENT_PROCESS
from co_core.pure.extract.canonical import CANONICAL_TEXT_MEDIA_TYPE
from co_core.pure.models.changes import (
    BlobAvailableEvent,
    ContentProcessCommand,
    ProcessingCompleteEvent,
    ProcessingFailedEvent,
)
from co_core.pure.util.hashing import bare_sha256
from co_core_sync.drivers.blobstore.local import LocalBlobStore
from google.api_core import exceptions as gapi
from google.auth import exceptions as gauth
from google.cloud.storage.exceptions import DataCorruption
from redis import exceptions as rx

from processor.child import ChildResult, run_in_child
from processor.handler import Deps, Disposition, handle
from processor.processors.extract import PROCESSOR_VERSION, extract
from processor.stores import Stores

CORPUS = Path(__file__).resolve().parent / "fixtures" / "parity"
HTML = (CORPUS / "inputs" / "agenda.html").read_bytes()
BLANK_PDF = (CORPUS / "inputs" / "scanned.pdf").read_bytes()
SPEC = {"extraction": {"algorithm": "css", "selector": "#main"}}
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
GiB = 1024**3


async def inprocess_child(target: str, args: tuple, **_: object) -> ChildResult:
    """The child's contract without the process: fast, for every row but the child's own."""
    module, _, name = target.partition(":")
    try:
        return ChildResult(kind="ok", value=getattr(importlib.import_module(module), name)(*args))
    except Exception as exc:
        return ChildResult(kind="raised", detail=f"{type(exc).__name__}: {exc}")


def fixed_child(result: ChildResult):
    async def run(*_a: object, **_k: object) -> ChildResult:
        return result

    return run


class FakePublisher:
    def __init__(self) -> None:
        self.effects: list[BusPublish] = []
        self.fail: BaseException | None = None
        self.before: list = []

    async def __call__(self, effect: BusPublish) -> None:
        for hook in self.before:
            hook(effect)
        if self.fail is not None:
            raise self.fail
        self.effects.append(effect)

    @property
    def facts(self) -> list:
        assert all(e.topic == CONTENT_DERIVED for e in self.effects)
        return [from_wire(dict(e.fields), topic=CONTENT_DERIVED).payload for e in self.effects]


class FlakyStore:
    """A store whose named method raises ``exc`` (or returns ``returns`` from ``open``)."""

    def __init__(self, inner, *, method: str, exc=None, returns: bytes | None = None) -> None:
        self._inner, self._method, self._exc, self._returns = inner, method, exc, returns

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if name != self._method:
            return attr

        def replaced(*args, **kwargs):
            if self._exc is not None:
                raise self._exc
            return self._returns

        return replaced


@dataclass
class Harness:
    input: LocalBlobStore
    output: LocalBlobStore
    publisher: FakePublisher = field(default_factory=FakePublisher)
    run_child: object = inprocess_child
    input_override: object = None
    output_override: object = None

    @property
    def deps(self) -> Deps:
        return Deps(
            stores=Stores(
                input=self.input_override or self.input, output=self.output_override or self.output
            ),
            publish=self.publisher,
            run_child=self.run_child,
            clock=lambda: NOW,
            extraction_timeout_s=120,
            rlimit_as_bytes=3 * GiB,
            max_attempts=3,
        )

    def message(self, raw: bytes = HTML, *, store: bool = True, **overrides) -> BusMessage:
        digest = hashlib.sha256(raw).hexdigest()
        if store:
            self.input.store(raw, digest, "text/html")
        fields = {
            "occurred_at": NOW,
            "command_id": "cmd-1",
            "info_source_id": "src-1",
            "input_uri": self.input.uri_for(digest),
            "input_digest": digest,
            "processor": "extract",
            "source_spec": SPEC,
            "media_type": "text/html",
        } | overrides
        command = ContentProcessCommand(**fields)
        return BusMessage(topic=CONTENT_PROCESS, message_id="1-0", fields={}, payload=command)

    async def run(self, message: BusMessage, attempt: int = 1) -> Disposition:
        return await handle(message, attempt=attempt, deps=self.deps)

    def only_failure(self) -> ProcessingFailedEvent:
        (fact,) = self.publisher.facts
        assert isinstance(fact, ProcessingFailedEvent)
        assert fact.terminal is True
        assert (fact.command_id, fact.info_source_id) == ("cmd-1", "src-1")
        return fact


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(input=LocalBlobStore(tmp_path / "in"), output=LocalBlobStore(tmp_path / "out"))


# --- success -------------------------------------------------------------------------


async def test_complete_stores_then_publishes_then_acks(h: Harness) -> None:
    expected = extract(HTML, "text/html", SPEC)
    key = bare_sha256(expected.output_digest)

    def stored_before_publish(_effect: BusPublish) -> None:
        assert h.output.exists(key), "published before stored"

    h.publisher.before.append(stored_before_publish)

    disposition = await h.run(h.message())

    assert disposition.action == "ack"
    (fact,) = h.publisher.facts
    assert isinstance(fact, ProcessingCompleteEvent)
    assert fact.command_id == "cmd-1" and fact.info_source_id == "src-1"
    assert fact.empty is False
    assert fact.output_digest == expected.output_digest
    assert fact.output_uri == h.output.uri_for(key)
    assert fact.output_size_bytes == len(expected.text)
    assert fact.output_media_type == CANONICAL_TEXT_MEDIA_TYPE
    assert fact.spec_fingerprint == expected.spec_fingerprint
    assert fact.spec_schema_version == 1
    assert fact.processor_version == PROCESSOR_VERSION
    assert fact.occurred_at == NOW
    assert h.output.open(key) == expected.text
    assert h.publisher.effects[0].fields["key"] == f"cmd-1:{NOW.isoformat()}"


async def test_empty_stores_nothing(h: Harness) -> None:
    disposition = await h.run(h.message(BLANK_PDF, media_type="application/pdf", source_spec={}))
    assert disposition.action == "ack"
    (fact,) = h.publisher.facts
    assert fact.empty is True
    assert (fact.output_digest, fact.output_uri, fact.output_size_bytes) == (None, None, 0)
    assert not any(h.output.root.rglob("*.bin"))


async def test_underivable_spec_fingerprint_is_none(h: Harness) -> None:
    await h.run(h.message(source_spec={"extraction": {"algorithm": "full_page"}, "w": 0.5}))
    (fact,) = h.publisher.facts
    assert fact.spec_fingerprint is None and fact.empty is False


async def test_the_real_child_runs_the_transform(h: Harness) -> None:
    h.run_child = run_in_child
    assert (await h.run(h.message())).action == "ack"
    (fact,) = h.publisher.facts
    assert fact.output_digest == extract(HTML, "text/html", SPEC).output_digest


# --- terminal failures: publish processing_failed, ack -------------------------------


async def test_a_foreign_event_type_is_dead_lettered(h: Harness) -> None:
    foreign = BlobAvailableEvent.model_construct(event_type="blob_available")
    message = BusMessage(topic=CONTENT_PROCESS, message_id="1-0", fields={}, payload=foreign)
    disposition = await h.run(message)
    assert disposition.action == "dead_letter"
    assert h.publisher.effects == []


async def test_unsupported_processor(h: Harness) -> None:
    assert (await h.run(h.message(processor="ocr"))).action == "ack"
    assert h.only_failure().reason == "unsupported_processor"


@pytest.mark.parametrize("digest", ["AB" * 32, "sha256:" + "ab" * 32, "nothex"])
async def test_malformed_digest_is_invalid_input(h: Harness, digest: str) -> None:
    assert (await h.run(h.message(input_digest=digest, store=False))).action == "ack"
    assert h.only_failure().reason == "invalid_input"


async def test_an_input_uri_the_store_does_not_recognize_is_invalid_input(h: Harness) -> None:
    message = h.message(input_uri="gs://someone-elses-bucket/blobs/x.bin")
    assert (await h.run(message)).action == "ack"
    assert h.only_failure().reason == "invalid_input"


async def test_input_bytes_gone(h: Harness) -> None:
    assert (await h.run(h.message(store=False))).action == "ack"
    assert h.only_failure().reason == "input_unreadable"


async def test_a_checksum_mismatch_on_download_is_input_unreadable(h: Harness) -> None:
    # The storage read failed, not the document: Watcher's re-fetch rewrites a temp
    # blob that really is corrupt, and costs one cheap fetch if it was only in transit.
    h.input_override = FlakyStore(h.input, method="open", exc=DataCorruption(None, "crc32c"))
    assert (await h.run(h.message(), attempt=1)).action == "ack"
    failure = h.only_failure()
    assert failure.reason == "input_unreadable" and "checksum" in failure.detail


async def test_bytes_that_hash_differently(h: Harness) -> None:
    h.input_override = FlakyStore(h.input, method="open", returns=b"tampered")
    assert (await h.run(h.message())).action == "ack"
    assert h.only_failure().reason == "input_digest_mismatch"


@pytest.mark.parametrize("detail", ["ValueError: bad xref", "MemoryError: "])
async def test_extractor_raises(h: Harness, detail: str) -> None:
    h.run_child = fixed_child(ChildResult(kind="raised", detail=detail))
    assert (await h.run(h.message())).action == "ack"
    failure = h.only_failure()
    assert failure.reason == "extraction_error"
    assert detail.split(":")[0] in failure.detail


# --- the child's timeout / crash: strikes, then a terminal failure -------------------


@pytest.mark.parametrize(
    "result",
    [
        ChildResult(kind="timeout", detail="no result after 120s"),
        ChildResult(kind="crashed", detail="exit status -9", returncode=-9),
    ],
    ids=["timeout", "crash"],
)
async def test_child_failures_strike_then_fail_on_the_last_attempt(
    h: Harness, result: ChildResult
) -> None:
    h.run_child = fixed_child(result)
    for attempt in (1, 2):
        disposition = await h.run(h.message(), attempt=attempt)
        assert disposition.action == "strike"
        assert h.publisher.effects == []
    assert (await h.run(h.message(), attempt=3)).action == "ack"
    failure = h.only_failure()
    assert failure.reason == "extraction_error"
    assert "attempt 3" in failure.detail


async def test_an_unexpected_error_strikes_rather_than_looping(h: Harness) -> None:
    h.input_override = FlakyStore(h.input, method="open", exc=KeyError("bug"))
    assert (await h.run(h.message(), attempt=1)).action == "strike"
    assert (await h.run(h.message(), attempt=3)).action == "ack"
    assert h.only_failure().reason == "extraction_error"


# --- infrastructure: publish nothing, leave pending -----------------------------------


@pytest.mark.parametrize(
    "exc",
    [gapi.ServiceUnavailable("503"), gapi.TooManyRequests("429"), gauth.RefreshError("auth")],
    ids=["5xx", "429", "auth"],
)
@pytest.mark.parametrize("where", ["input", "output"])
async def test_gcs_failures_leave_the_entry_pending(h: Harness, exc, where: str) -> None:
    if where == "input":
        h.input_override = FlakyStore(h.input, method="open", exc=exc)
    else:
        h.output_override = FlakyStore(h.output, method="store", exc=exc)
    disposition = await h.run(h.message(), attempt=3)  # uncapped: even the last attempt
    assert disposition.action == "leave_pending"
    assert h.publisher.effects == []


@pytest.mark.parametrize(
    "exc",
    [
        rx.NoPermissionError(
            "NOPERM this user has no permissions to access the 'content.derived' key"
        ),
        rx.OutOfMemoryError("OOM command not allowed when used memory > 'maxmemory'."),
        rx.ConnectionError("Connection reset by peer"),
    ],
    ids=["NOPERM", "OOM", "connection"],
)
async def test_publish_failure_leaves_pending_and_the_rerun_converges(h: Harness, exc) -> None:
    h.publisher.fail = exc
    assert (await h.run(h.message(), attempt=3)).action == "leave_pending"
    stored = list(h.output.root.rglob("*.bin"))
    assert len(stored) == 1  # the store ran; the reclaim's re-run finds it there

    h.publisher.fail = None
    assert (await h.run(h.message())).action == "ack"
    (fact,) = h.publisher.facts
    assert fact.empty is False
    assert list(h.output.root.rglob("*.bin")) == stored


async def test_every_disposition_carries_what_the_log_needs(h: Harness) -> None:
    disposition = await h.run(h.message())
    assert (disposition.command_id, disposition.info_source_id) == ("cmd-1", "src-1")
    assert disposition.reason == "complete"
    assert {"read_ms", "extract_ms", "store_ms", "publish_ms", "total_ms"} <= set(
        disposition.timings
    )
