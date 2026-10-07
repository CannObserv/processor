"""``processor run`` — the service; ``processor ensure-group`` — the hard ordering;
``processor dlq list|show|drop`` — the drainer; ``processor drift`` — the drift check."""

import argparse
import asyncio
import json
import logging
import signal
import sys
from datetime import UTC, datetime

from co_core.pure.adapters.bus.streams import CONTENT_PROCESS
from co_core_aio.bus import AsyncBusPublisher
from pydantic import ValidationError
from redis.asyncio import Redis

from processor import dlq, drift
from processor._contain import landlock_abi, make_undumpable, unavailable_reason
from processor.build import build_id
from processor.checkin import CREDENTIAL_NAME, CheckinFailed, post_checkin, read_key
from processor.child import run_in_child, transform_target
from processor.consumer import GROUP, Consumer, group_reader, redis_client
from processor.handler import Deps
from processor.logging import configure_logging
from processor.processors import TRANSFORMS
from processor.processors.extract import PROCESSOR_VERSION
from processor.settings import DriftSettings, Settings
from processor.stores import build_stores

logger = logging.getLogger("processor")

# One real extraction through the child at boot (#2 CR 1): the extractors import,
# and their libraries load, inside the child's allowlist.
_CANARY = (
    b"<html><body><p>canary</p></body></html>",
    "text/html",
    {"extraction": {"algorithm": "full_page"}},
)


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv``, configure JSON logging and settings, run one subcommand.

    Exit 2 on invalid settings, logged as one JSON record that echoes no input value
    (pydantic's own message would print part of the bus URL, credential and all).
    """
    parser = argparse.ArgumentParser(prog="processor")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="consume content.process until SIGTERM")
    commands.add_parser(
        "ensure-group", help="create processor.process from $ (MKSTREAM) if absent, and exit"
    )
    drain = commands.add_parser("dlq", help="inspect or drop content.process.dlq entries")
    drain_commands = drain.add_subparsers(dest="dlq_command", required=True)
    listing = drain_commands.add_parser("list")
    listing.add_argument("--count", type=_positive_int, default=100)
    drain_commands.add_parser("show").add_argument("id")
    drain_commands.add_parser("drop").add_argument("id")
    drift_parser = commands.add_parser(
        "drift", help="check once whether live lags origin/main, and check in to Status (#35)"
    )
    drift_parser.add_argument(
        "--test-alert", action="store_true", help="send one test alert, asking GitHub nothing"
    )
    args = parser.parse_args(argv)

    configure_logging()
    try:
        settings = DriftSettings() if args.command == "drift" else Settings()
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
        logger.error("invalid settings", extra={"errors": errors})
        return 2
    if args.command == "drift":
        return _drift(settings, test_alert=args.test_alert)
    if args.command == "run":
        return asyncio.run(_run(settings))
    if args.command == "ensure-group":
        return asyncio.run(_ensure_group(settings))
    return asyncio.run(_dlq(settings, args))


def _drift(settings: DriftSettings, *, test_alert: bool) -> int:
    """One drift check, one check-in, one ``drift check`` record (#35).

    Exit 0 when Status took the check-in, ok or alert; 1 when there was none to
    send (GitHub silent) or Status did not take it. Never a retry: the next hour is.
    """
    live = build_id()
    if test_alert:
        verdict = drift.deliberate_alert(live)
    else:
        verdict = drift.assess(live, now=datetime.now(UTC), get=drift.github())
    fields = {"build": live, "main": verdict.main, "kind": verdict.kind, "body": verdict.body}
    if verdict.status is None:
        logger.warning("drift check", extra=fields | {"checkin": None})
        return 1
    key = read_key(settings.credentials_directory)
    missing = [
        name
        for name, value in (
            ("CO_PROCESSOR_DRIFT_MONITOR_ID", settings.drift_monitor_id),
            (f"the {CREDENTIAL_NAME} credential", key),
        )
        if not value
    ]
    if missing:
        logger.error(
            "drift check", extra=fields | {"checkin": f"not sent: missing {', '.join(missing)}"}
        )
        return 1
    try:
        code = post_checkin(
            settings.status_url, settings.drift_monitor_id, key, verdict.status, verdict.variables()
        )
    except CheckinFailed as exc:
        logger.error("drift check", extra=fields | {"checkin": f"failed: {exc}"})
        return 1
    level = logging.INFO if verdict.status == "ok" else logging.WARNING
    logger.log(level, "drift check", extra=fields | {"checkin": code})
    return 0


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {number}")
    return number


def _bus(settings: Settings) -> Redis:
    return redis_client(settings.bus_url.get_secret_value(), read_block_ms=settings.read_block_ms)


async def _ensure_group(settings: Settings) -> int:
    """The hard ordering (spec §6): the group exists before Watcher's first command."""
    client = _bus(settings)
    try:
        await group_reader(client, settings.consumer_name).ensure_group(start_id="$")
    finally:
        await client.aclose()
    logger.info("group ensured", extra={"group": GROUP, "stream": CONTENT_PROCESS})
    return 0


async def _run(settings: Settings) -> int:
    # First: the env file's credential has been in our environment since exec, and
    # the extraction child shares our uid (#2).
    make_undumpable()
    if settings.child_containment == "required" and (reason := unavailable_reason()):
        logger.error(
            "child containment unavailable",
            extra={"reason": reason, "landlock_abi": landlock_abi()},
        )
        return 1
    # The ABI check cannot see a child that fails for any other reason: an
    # allowlist refusal, or a library outside it. Every command would then crash
    # three times, and the third publishes a terminal extraction_error for a
    # healthy document (spec §4). Prove the child before taking any.
    canary = await run_in_child(
        transform_target(TRANSFORMS["extract"]),
        _CANARY,
        timeout_s=settings.extraction_timeout_s,
        rlimit_as_bytes=settings.rlimit_as_bytes,
        containment=settings.child_containment,
    )
    if canary.kind != "ok":
        logger.error(
            "child canary failed",
            extra={
                "kind": canary.kind,
                "detail": canary.detail,
                "child_containment": settings.child_containment,
            },
        )
        return 1

    try:
        stores = await asyncio.to_thread(build_stores, settings)
        await asyncio.to_thread(stores.input.preflight)
        await asyncio.to_thread(stores.output.preflight)
    except Exception:
        logger.exception("store preflight failed; exiting for systemd to restart")
        return 1

    client = _bus(settings)
    deps = Deps(
        stores=stores,
        publish=AsyncBusPublisher(client).execute,
        run_child=run_in_child,
        clock=lambda: datetime.now(UTC),
        extraction_timeout_s=settings.extraction_timeout_s,
        rlimit_as_bytes=settings.rlimit_as_bytes,
        max_attempts=settings.max_attempts,
        containment=settings.child_containment,
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
            "build": build_id(),
            "child_containment": settings.child_containment,
            "landlock_abi": landlock_abi(),
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
    client = _bus(settings)
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
