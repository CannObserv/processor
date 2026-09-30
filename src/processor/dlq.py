"""Draining ``content.process.dlq``: list, show, drop (spec §4). No replay.

The writer of a queue is its drainer. Everything dead-lettered here is a frame that
did not decode or was not a ``content_process`` command; there is nothing to replay.
Uses only ``XRANGE`` / ``XDEL`` on the DLQ, as broker#75 grants.
"""

from co_core.pure.adapters.bus.dead_letter import split_dead_letter
from co_core.pure.adapters.bus.streams import CONTENT_PROCESS, dlq_name
from redis.asyncio import Redis

from processor.consumer import as_str, decode_fields

DLQ = dlq_name(CONTENT_PROCESS)


async def list_entries(client: Redis, *, count: int = 100) -> list[dict]:
    """One summary per entry, oldest first."""
    rows = []
    for entry_id, raw in await client.xrange(DLQ, count=count):
        fields, provenance = split_dead_letter(decode_fields(raw))
        rows.append(
            {
                "id": as_str(entry_id),
                "source_id": provenance.source_id,
                "reason": provenance.reason,
                "event_type": fields.get("event_type"),
            }
        )
    return rows


async def show(client: Redis, entry_id: str) -> dict | None:
    """The original fields and the provenance of one entry, or ``None``.

    The ``id`` is the entry's own: ``XRANGE`` reads a bare millisecond id as a range
    over that millisecond, so it can differ from the ``entry_id`` asked for.
    """
    entries = await client.xrange(DLQ, min=entry_id, max=entry_id)
    if not entries:
        return None
    found_id, raw = entries[0]
    fields, provenance = split_dead_letter(decode_fields(raw))
    return {
        "id": as_str(found_id),
        "fields": fields,
        "provenance": {
            "source_id": provenance.source_id,
            "group": provenance.group,
            "consumer": provenance.consumer,
            "reason": provenance.reason,
        },
    }


async def drop(client: Redis, entry_id: str) -> bool:
    """Delete one entry; whether it existed."""
    return bool(await client.xdel(DLQ, entry_id))
