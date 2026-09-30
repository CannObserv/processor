"""Which failures are infrastructure (publish nothing, leave pending) — spec §4."""

import pytest
import requests
from google.api_core import exceptions as gapi
from google.auth import exceptions as gauth
from redis import exceptions as rx
from redis._parsers.base import BaseParser

from processor.errors import is_transient


@pytest.mark.parametrize(
    "exc",
    [
        gapi.ServiceUnavailable("503"),
        gapi.InternalServerError("500"),
        gapi.BadGateway("502"),
        gapi.TooManyRequests("429"),
        gapi.Forbidden("403"),
        gapi.Unauthorized("401"),
        gapi.DeadlineExceeded("deadline"),
        gapi.RetryError("gave up", cause=None),
        gauth.RefreshError("token refresh"),
        gauth.TransportError("metadata server"),
        gauth.DefaultCredentialsError("no ADC"),
        requests.ConnectionError("reset"),
        requests.Timeout("read timeout"),
        ConnectionResetError("reset"),
        TimeoutError("timed out"),
        rx.ConnectionError("broker gone"),
        rx.TimeoutError("socket timeout"),
        rx.BusyLoadingError("loading"),
        rx.AuthenticationError("WRONGPASS"),
        rx.NoPermissionError("NOPERM this user has no permissions"),
        rx.OutOfMemoryError("OOM command not allowed when used memory > 'maxmemory'."),
        rx.ReadOnlyError("READONLY"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_infrastructure_failures_are_transient(exc: BaseException) -> None:
    assert is_transient(exc)


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("bug"),
        KeyError("bug"),
        FileNotFoundError("blobs/ab.bin"),  # the input is gone: a terminal reason, not a retry
        gapi.NotFound("404"),
        gapi.BadRequest("400"),
        rx.ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value"),
        rx.DataError("bad argument"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_everything_else_is_not(exc: BaseException) -> None:
    assert not is_transient(exc)


def _reply(raw: str) -> BaseException:
    """The exception redis-py raises for this error reply (its parser, not a guess)."""
    return BaseParser().parse_error(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "MISCONF Redis is configured to save RDB snapshots, but it is currently not able to persist on disk.",
        "BUSY Redis is busy running a script. You can only call SCRIPT KILL or SHUTDOWN NOSAVE.",
        "MASTERDOWN Link with MASTER is down and replica-serve-stale-data is set to 'no'.",
        "TRYAGAIN Multiple keys request during rehashing of slot",
        "CLUSTERDOWN The cluster is down",
        "NOREPLICAS Not enough good replicas to write.",
        "NOPERM User processor has no permissions to run the 'xadd' command",
        "OOM command not allowed when used memory > 'maxmemory'.",
    ],
    ids=lambda raw: raw.split()[0],
)
def test_broker_replies_that_mean_infrastructure_are_transient(raw: str) -> None:
    assert is_transient(_reply(raw))


@pytest.mark.parametrize(
    "raw",
    [
        "BUSYGROUP Consumer Group name already exists",
        "BUSYKEY Target key name already exists.",
        "WRONGTYPE Operation against a key holding the wrong kind of value",
        "ERR syntax error",
    ],
    ids=lambda raw: raw.split()[0],
)
def test_other_broker_replies_are_not(raw: str) -> None:
    assert not is_transient(_reply(raw))
