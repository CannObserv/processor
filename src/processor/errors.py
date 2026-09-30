"""Infrastructure failures: publish nothing, ack nothing, let the reclaim retry (spec §4).

Uncapped by design — Watcher's reaper re-issues a stale command, and a fact for a
superseded command is discarded. Everything else is either a terminal reason the
handler names (``FileNotFoundError`` on the input is ``input_unreadable``) or a
strike toward the 3-attempt cap.

``NOPERM`` and ``OOM command not allowed`` are ``ResponseError``s, not connection
errors: both are transient here, as the broker requires of every participant
(broker#62, broker#75).
"""

import requests
from google.api_core import exceptions as gapi
from google.auth import exceptions as gauth
from redis import exceptions as rx

_TRANSIENT: tuple[type[BaseException], ...] = (
    # GCS: 5xx, 429, auth, deadlines.
    gapi.ServerError,
    gapi.TooManyRequests,
    gapi.Forbidden,
    gapi.Unauthorized,
    gapi.DeadlineExceeded,
    gapi.RetryError,
    gauth.RefreshError,
    gauth.TransportError,
    gauth.DefaultCredentialsError,
    requests.ConnectionError,
    requests.Timeout,
    # Broker: connection loss (incl. auth and loading), timeouts, ACL, maxmemory.
    rx.ConnectionError,
    rx.TimeoutError,
    rx.NoPermissionError,
    rx.OutOfMemoryError,
    rx.ReadOnlyError,
    # Sockets below either client.
    ConnectionError,
    TimeoutError,
)


def is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` is an infrastructure failure the reclaim should retry."""
    return isinstance(exc, _TRANSIENT)
