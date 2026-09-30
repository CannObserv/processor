"""Infrastructure failures: publish nothing, ack nothing, let the reclaim retry (spec §4).

Uncapped by design — Watcher's reaper re-issues a stale command, and a fact for a
superseded command is discarded. Everything else is either a terminal reason the
handler names (``FileNotFoundError`` on the input is ``input_unreadable``) or a
strike toward the 3-attempt cap.

``NOPERM`` and ``OOM command not allowed`` are ``ResponseError``s, not connection
errors: both are transient here, as the broker requires of every participant
(broker#62, broker#75). So are the other replies that describe the broker's state
rather than the command: ``MISCONF`` (a failed save blocks writes), ``BUSY`` (a
script is running), ``MASTERDOWN``, ``TRYAGAIN``, ``CLUSTERDOWN``, ``NOREPLICAS``.
redis-py gives some of them a class (and strips the code) and leaves the rest a plain
``ResponseError`` carrying it, so both are checked. ``BUSYGROUP`` and ``BUSYKEY`` are
about the command, and are not transient.
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
    rx.MasterDownError,
    rx.TryAgainError,
    rx.ClusterDownError,
    # Sockets below either client.
    ConnectionError,
    TimeoutError,
)


# Broker error codes redis-py leaves on a plain ResponseError. The trailing space
# keeps BUSYGROUP and BUSYKEY out.
_TRANSIENT_REPLY_CODES = ("MISCONF ", "BUSY ", "MASTERDOWN ", "NOREPLICAS ")


def is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` is an infrastructure failure the reclaim should retry."""
    if isinstance(exc, _TRANSIENT):
        return True
    return isinstance(exc, rx.ResponseError) and str(exc).startswith(_TRANSIENT_REPLY_CODES)
