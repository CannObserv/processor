"""``processor run`` — the service; ``processor dlq list|show|drop`` — the drainer."""

import argparse
import asyncio
import json
import logging
import signal
import sys
from datetime import UTC, datetime

from co_core.pure.adapters.bus.streams import CONTENT_PROCESS
from co_core_aio.bus import AsyncBusConsumer, AsyncBusPublisher

from processor import dlq
from processor.child import run_in_child
from processor.consumer import GROUP, Consumer, redis_client
from processor.handler import Deps
from processor.logging import configure_logging
from processor.processors.extract import PROCESSOR_VERSION
from processor.settings import Settings
from processor.stores import build_stores

logger = logging.getLogger("processor")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="processor")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="consume content.process until SIGTERM")
    commands.add_parser(
        "ensure-group", help="create processor.process from $ (MKSTREAM) if absent, and exit"
    )
    drain = commands.add_parser("dlq", help="inspect or drop content.process.dlq entries")
    drain_commands = drain.add_subparsers(dest="dlq_command", required=True)
    listing = drain_commands.add_parser("list")
    listing.add_argument("--count", type=int, default=100)
    drain_commands.add_parser("show").add_argument("id")
    drain_commands.add_parser("drop").add_argument("id")
    args = parser.parse_args(argv)

    configure_logging()
    settings = Settings()
    if args.command == "run":
        return asyncio.run(_run(settings))
    if args.command == "ensure-group":
        return asyncio.run(_ensure_group(settings))
    return asyncio.run(_dlq(settings, args))


async def _ensure_group(settings: Settings) -> int:
    """The hard ordering (spec §6): the group exists before Watcher's first command."""
    client = redis_client(settings.bus_url.get_secret_value(), read_block_ms=settings.read_block_ms)
    try:
        await AsyncBusConsumer(
            client, topic=CONTENT_PROCESS, group=GROUP, consumer=settings.consumer_name
        ).ensure_group(start_id="$")
    finally:
        await client.aclose()
    logger.info("group ensured", extra={"group": GROUP, "stream": CONTENT_PROCESS})
    return 0


async def _run(settings: Settings) -> int:
    try:
        stores = await asyncio.to_thread(build_stores, settings)
        await asyncio.to_thread(stores.input.preflight)
        await asyncio.to_thread(stores.output.preflight)
    except Exception:
        logger.exception("store preflight failed; exiting for systemd to restart")
        return 1

    client = redis_client(settings.bus_url.get_secret_value(), read_block_ms=settings.read_block_ms)
    deps = Deps(
        stores=stores,
        publish=AsyncBusPublisher(client).execute,
        run_child=run_in_child,
        clock=lambda: datetime.now(UTC),
        extraction_timeout_s=settings.extraction_timeout_s,
        rlimit_as_bytes=settings.rlimit_as_bytes,
        max_attempts=settings.max_attempts,
    )
    consumer = Consumer(
        client,
        deps,
        consumer_name=settings.consumer_name,
        read_block_ms=settings.read_block_ms,
        reclaim_min_idle_ms=settings.reclaim_min_idle_ms,
        reclaim_interval_s=settings.reclaim_interval_s,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info(
        "starting",
        extra={
            "processor_version": PROCESSOR_VERSION,
            "group": GROUP,
            "consumer_name": settings.consumer_name,
            "store_backend": settings.store_backend,
            "input": stores.input.uri_for("0" * 64).rsplit("/", 1)[0],
            "output": stores.output.uri_for("0" * 64).rsplit("/", 1)[0],
        },
    )
    try:
        await consumer.run(stop)
    finally:
        await client.aclose()
    logger.info("stopped")
    return 0


async def _dlq(settings: Settings, args: argparse.Namespace) -> int:
    client = redis_client(settings.bus_url.get_secret_value(), read_block_ms=settings.read_block_ms)
    try:
        if args.dlq_command == "list":
            for row in await dlq.list_entries(client, count=args.count):
                print(json.dumps(row))
            return 0
        if args.dlq_command == "show":
            entry = await dlq.show(client, args.id)
            if entry is None:
                print(f"no DLQ entry {args.id}", file=sys.stderr)
                return 1
            print(json.dumps(entry, indent=2))
            return 0
        if not await dlq.drop(client, args.id):
            print(f"no DLQ entry {args.id}", file=sys.stderr)
            return 1
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(main())
