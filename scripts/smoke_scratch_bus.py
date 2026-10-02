"""Smoke test: one real command through Processor's consumer loop on the scratch Redis.

Spec §6 cutover step 3. Run it after an install, a deploy, or a key rotation, as
``exedev`` on ``co-processor``:

    set -a; . /etc/processor/.env; set +a
    .venv/bin/python scripts/smoke_scratch_bus.py [--digest <input_digest>]

What is real, and what is not:

- **Bus:** the scratch ``redis-server`` (``redis://localhost:6379/14``), never the
  broker. ``processor`` must not ``XADD content.process`` there, and a fact for a
  made-up command would be a fake fact on the real bus. Any other host is refused,
  and so is a db already holding the streams. The script deletes its streams
  afterwards.
- **Loop:** the real ``Consumer``: ``ensure_group``, read, ``handle`` in the real
  child, store, publish, ack.
- **Input:** a blob from the committed real corpus (``tests/fixtures/parity/real/``,
  watcher#325's export) in a temporary local store. It never expires, unlike
  ``gs://co-gcs-blobs``; the boot preflight covers the input bucket.
- **Output:** the production store, ``gs://co-gcs-processor``: write-if-absent, so
  re-running on a corpus input already stored writes nothing new. The default
  input's object, ``b8f6d0f1….bin``, has been there since 2026-10-02.

It passes when the fact is ``complete``, its digest equals Watcher's recorded
fingerprint, the entry is acked, and the stored object reads back intact.
"""

import argparse
import asyncio
import gzip
import hashlib
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from co_core.pure.adapters.bus.envelope import from_wire, to_wire
from co_core.pure.adapters.bus.streams import CONTENT_DERIVED, CONTENT_PROCESS, dlq_name
from co_core.pure.models.changes import ContentProcessCommandEmit, ProcessingCompleteEvent
from co_core.pure.util.hashing import bare_sha256
from co_core_aio.bus import AsyncBusPublisher
from co_core_sync.drivers.blobstore.local import LocalBlobStore

from processor.child import run_in_child
from processor.consumer import Consumer, redis_client
from processor.handler import Deps
from processor.settings import GiB, Settings
from processor.stores import Stores, build_stores

SCRATCH_URL = "redis://localhost:6379/14"
REAL = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "parity" / "real"
# LCB board meetings, recorded 2026-10-01 under 0.19.7+1; its text is b8f6d0f1….
DEFAULT_DIGEST = "2e38aa5e980b7136376f80bccd6773bedb26c00aa381fd228e2e7daa57548492"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_STREAMS = (CONTENT_PROCESS, CONTENT_DERIVED, dlq_name(CONTENT_PROCESS))


def export_item(digest: str) -> dict:
    """The real corpus's export row for ``digest``."""
    items = json.loads((REAL / "export.json").read_text())["items"]
    return next(item for item in items if item["input_digest"] == digest)


async def smoke(redis_url: str, output, item: dict) -> ProcessingCompleteEvent:
    """Run ``item`` through the consumer on ``redis_url``; return its verified fact."""
    if urlparse(redis_url).hostname not in _LOCAL_HOSTS:
        raise ValueError(f"not the scratch redis-server: {redis_url!r}")
    client = redis_client(redis_url, read_block_ms=1000)
    try:
        if await client.exists(*_STREAMS):
            raise RuntimeError(f"scratch db not empty: it already holds {_STREAMS}")
        try:
            return await _round_trip(client, output, item)
        finally:
            await client.delete(*_STREAMS)
    finally:
        await client.aclose()


async def _round_trip(client, output, item: dict) -> ProcessingCompleteEvent:
    digest = item["input_digest"]
    raw = gzip.decompress((REAL / f"{digest}.bin.gz").read_bytes())
    with tempfile.TemporaryDirectory() as tmp:
        source = LocalBlobStore(Path(tmp))
        source.store(raw, digest, item["media_type"])
        deps = Deps(
            stores=Stores(input=source, output=output),
            publish=AsyncBusPublisher(client).execute,
            run_child=run_in_child,
            clock=lambda: datetime.now(UTC),
            extraction_timeout_s=120,
            rlimit_as_bytes=3 * GiB,
            max_attempts=3,
        )
        consumer = Consumer(
            client,
            deps,
            consumer_name="smoke",
            read_block_ms=1000,
            reclaim_min_idle_ms=600_000,
            reclaim_interval_s=3600,
        )
        await consumer.start()
        command = ContentProcessCommandEmit(
            occurred_at=datetime.now(UTC),
            command_id=f"smoke-{datetime.now(UTC):%Y%m%dT%H%M%SZ}",
            info_source_id="smoke",
            input_uri=source.uri_for(digest),
            input_digest=digest,
            processor="extract",
            source_spec=item["source_spec"],
            media_type=item["media_type"],
        )
        await client.xadd(CONTENT_PROCESS, to_wire(command))
        await consumer.step()

    entries = await client.xrange(CONTENT_DERIVED)
    facts = [from_wire(_decode(fields), topic=CONTENT_DERIVED).payload for _id, fields in entries]
    if len(facts) != 1 or not isinstance(facts[0], ProcessingCompleteEvent):
        raise RuntimeError(f"expected one complete fact, got {facts!r}")
    fact = facts[0]
    if fact.output_digest != item["recorded_fingerprint"]:
        raise RuntimeError(f"parity: {fact.output_digest} != {item['recorded_fingerprint']}")
    (group,) = await client.xinfo_groups(CONTENT_PROCESS)
    if group["pending"] != 0:
        raise RuntimeError(f"entry not acked: {group['pending']} pending")
    stored = output.open(bare_sha256(fact.output_digest))
    if hashlib.sha256(stored).hexdigest() != bare_sha256(fact.output_digest):
        raise RuntimeError("stored object does not hash to its digest")
    return fact


def _decode(fields: dict) -> dict[str, str]:
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in fields.items()
    }


def main() -> int:
    """CLI: the production output store, the scratch bus."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--digest", default=DEFAULT_DIGEST, help="a real-corpus input_digest")
    parser.add_argument("--bus-url", default=SCRATCH_URL, help="the scratch redis-server")
    args = parser.parse_args()
    output = build_stores(Settings()).output
    fact = asyncio.run(smoke(args.bus_url, output, export_item(args.digest)))
    print(
        json.dumps(
            {
                "result": "pass",
                "output_uri": fact.output_uri,
                "output_size_bytes": fact.output_size_bytes,
                "processor_version": fact.processor_version,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
