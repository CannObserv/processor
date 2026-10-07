"""One check-in to a Status monitor: CannObserv/status's dead-man's timers (#35).

Status's contract, notifier's unchanged (its spec D6):
``POST /api/v1/monitors/{id}/checkin`` with ``{"status": "ok"|"alert",
"variables": {...}}`` and an ``X-API-Key``, answered 202. An ``ok`` records the
check-in; an ``alert`` renders the monitor's templates against ``variables``
and dispatches them; silence past the interval and grace goes ``missing``.

**One attempt, never a loop** (#35 trap 4): the next run is the retry, and the
monitor's grace absorbs one missed check-in. A failure raises
:class:`CheckinFailed`, whose message names Status's answer and never the key.

**The key is a systemd credential** (``LoadCredential=``, status D13), read from
``$CREDENTIALS_DIRECTORY``: in no process environment, never logged.
"""

from pathlib import Path
from typing import Literal

import requests

#: The ``LoadCredential=`` name ``processor-drift.service`` gives the key.
CREDENTIAL_NAME = "status-checkin-key"
TIMEOUT_SECONDS = 10.0


class CheckinFailed(Exception):
    """Status did not take the check-in: its answer, or why there was none."""


def read_key(directory: Path | None) -> str:
    """The key credential under *directory*, stripped; ``""`` when there is none.

    Absent, empty and the unit's lone-newline ``SetCredential=`` fallback all read
    as ``""``; outside a unit there is no directory at all.
    """
    if directory is None:
        return ""
    try:
        return (directory / CREDENTIAL_NAME).read_text().strip()
    except FileNotFoundError:
        return ""


def post_checkin(
    base_url: str,
    monitor_id: str,
    key: str,
    status: Literal["ok", "alert"],
    variables: dict[str, str],
    *,
    timeout: float = TIMEOUT_SECONDS,
) -> int:
    """Check in once; return Status's answer code (202), or raise :class:`CheckinFailed`."""
    url = f"{base_url.rstrip('/')}/api/v1/monitors/{monitor_id}/checkin"
    try:
        response = requests.post(
            url,
            json={"status": status, "variables": variables},
            headers={"X-API-Key": key},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        # By type and requests' own text, which quotes the URL, never a header.
        raise CheckinFailed(f"{type(exc).__name__}: {exc}") from None
    if response.status_code != 202:
        raise CheckinFailed(f"{response.status_code} {_detail(response)}".strip())
    return response.status_code


def _detail(response: requests.Response) -> str:
    try:
        detail = response.json().get("detail", "")
    except (ValueError, AttributeError):
        return ""
    return detail if isinstance(detail, str) else str(detail)
