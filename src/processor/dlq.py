"""Draining ``content.process.dlq``: list, show, drop (spec §4). No replay.

The writer of a queue is its drainer. Everything dead-lettered here is a frame that
did not decode or was not a ``content_process`` command; there is nothing to replay.
Uses only ``XRANGE`` / ``XDEL`` on the DLQ, as broker#75 grants.
"""

from co_core.pure.adapters.bus.dead_letter import split_dead_letter
from co_core.pure.adapters.bus.streams import CONTENT_PROCESS, dlq_name
from redis.asyncio import Redis

DLQ = dlq_name(CONTENT_PROCESS)


def _fields(raw: dict) -> dict[str, str]:
    return {_str(k): _str(v) for k, v in raw.items()}


def _str(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


async def list_entries(client: Redis, *, count: int = 100) -> list[dict]:
    """One summary per entry, oldest first."""
    rows = []
    for entry_id, raw in await client.xrange(DLQ, count=count):
        fields, provenance = split_dead_letter(_fields(raw))
        rows.append(
            {
                "id": _str(entry_id),
                "source_id": provenance.source_id,
                "reason": provenance.reason,
                "event_type": fields.get("event_type"),
            }
        )
    return rows


async def show(client: Redis, entry_id: str) -> dict | None:
    """The original fields and the provenance of one entry, or ``None``."""
    entries = await client.xrange(DLQ, min=entry_id, max=entry_id)
    if not entries:
        return None
    fields, provenance = split_dead_letter(_fields(entries[0][1]))
    return {
        "id": entry_id,
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
