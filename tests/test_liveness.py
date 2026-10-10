"""``processor.liveness``: the heartbeat beside the consume loop (#39), against a local stub.

It checks in ``ok`` to Status's ``co-processor-live`` only while the consume loop is
making progress, and stays silent otherwise: Status's ``missing`` is the page. A
check-in runs in a daemon thread, bounded, one attempt per tick, so a slow or failing
Status never reaches the event loop.
"""

import asyncio
import json
import logging
import socket
import threading
import time
from functools import partial
from types import SimpleNamespace

import pytest

from processor.checkin import post_checkin
from processor.liveness import Heartbeat

MONITOR = "01M46EXP45TVCVAMQXK043N1Q7"
KEY = "sk-test-0123456789abcdef"
PATH = f"/api/v1/monitors/{MONITOR}/checkin"


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def progress(clock: Clock) -> SimpleNamespace:
    return SimpleNamespace(last_progress=clock.now, backing_off=False)


def heartbeat(
    progress, url: str, clock=time.monotonic, request_timeout: float | None = None, **overrides
) -> Heartbeat:
    knobs = {"interval_s": 300.0, "stale_after_s": 605.0, "timeout_s": 10.0} | overrides
    timeout = request_timeout or knobs["timeout_s"]
    post = partial(post_checkin, url, MONITOR, KEY, "ok", timeout=timeout)
    return Heartbeat(progress, post, build="abc123def456", clock=clock, **knobs)


def records(caplog, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == message]


async def test_fresh_progress_checks_in_ok_with_every_variable(http_stub, progress, clock):
    http_stub.route("POST", PATH, 202)
    clock.now += 42.4
    progress.backing_off = True
    await heartbeat(progress, http_stub.url, clock).tick()
    (request,) = http_stub.requests
    assert request["headers"]["X-API-Key"] == KEY
    assert json.loads(request["body"]) == {
        "status": "ok",
        "variables": {
            "build": "abc123def456",
            "last_progress_age_s": "42",
            "backing_off": "true",
        },
    }


async def test_stale_progress_stays_silent_and_says_so(http_stub, progress, clock, caplog):
    http_stub.route("POST", PATH, 202)
    clock.now += 605.5
    with caplog.at_level(logging.INFO, logger="processor.liveness"):
        await heartbeat(progress, http_stub.url, clock).tick()
    assert http_stub.requests == []
    (record,) = records(caplog, "consume loop stale; not checking in")
    assert record.levelno == logging.WARNING
    assert (record.last_progress_age_s, record.stale_after_s) == (605.5, 605.0)


async def test_progress_at_the_bound_is_still_fresh(http_stub, progress, clock):
    http_stub.route("POST", PATH, 202)
    clock.now += 605.0
    await heartbeat(progress, http_stub.url, clock).tick()
    assert len(http_stub.requests) == 1


@pytest.mark.parametrize(("code", "detail"), [(500, "500"), (401, "401 bad key")])
async def test_a_refusal_is_one_warning_and_the_next_tick_tries_again(
    http_stub, progress, clock, caplog, code, detail
):
    http_stub.route("POST", PATH, code, {"detail": "bad key"} if code == 401 else None)
    beat = heartbeat(progress, http_stub.url, clock)
    with caplog.at_level(logging.INFO, logger="processor.liveness"):
        await beat.tick()
        await beat.tick()
    assert len(http_stub.requests) == 2
    failed = records(caplog, "liveness check-in failed")
    assert [r.levelno for r in failed] == [logging.WARNING] * 2
    assert failed[0].error == detail
    assert KEY not in caplog.text


async def test_unreachable_status_is_a_warning(progress, clock, caplog):
    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{probe.getsockname()[1]}"
    with caplog.at_level(logging.INFO, logger="processor.liveness"):
        await heartbeat(progress, url, clock).tick()
    (record,) = records(caplog, "liveness check-in failed")
    assert record.error.startswith("ConnectionError")


async def test_an_unexpected_error_is_a_warning_never_an_escape(progress, clock, caplog):
    def post(variables):
        raise RuntimeError("boom")

    beat = Heartbeat(
        progress, post, build="b", clock=clock, interval_s=300, stale_after_s=605, timeout_s=10
    )
    with caplog.at_level(logging.INFO, logger="processor.liveness"):
        await beat.tick()
    (record,) = records(caplog, "liveness check-in failed")
    assert record.error == "RuntimeError: boom"


async def test_a_hung_status_is_cut_off_and_the_next_tick_is_skipped(hung_status, progress, caplog):
    # requests' own timeout is longer, so the heartbeat's bound is what cuts it off.
    progress.last_progress = time.monotonic()
    beat = heartbeat(progress, hung_status, timeout_s=0.2, request_timeout=5)
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="processor.liveness"):
        await beat.tick()
        assert time.monotonic() - started < 1.0
        # The thread is still waiting on requests' 5 s.
        await beat.tick()
    (failed,) = records(caplog, "liveness check-in failed")
    assert failed.error == "timed out after 0.2 s"
    (skipped,) = records(caplog, "previous check-in still in flight; skipping this tick")
    assert skipped.levelno == logging.WARNING


async def test_a_cut_off_check_in_frees_the_next_tick_once_its_post_returns(
    progress, clock, caplog
):
    # #39 trap 1's other half (#45 CR 1): a post cut off by the timeout keeps later
    # ticks skipping only until it returns. Were the marker cleared on the loop, where
    # settle drops a cut-off result, one hung Status would silence the heartbeat.
    release = threading.Event()
    threads = []

    def post(variables):
        threads.append(threading.current_thread())
        if len(threads) == 1:
            release.wait(5)
        return 202

    beat = Heartbeat(
        progress, post, build="b", clock=clock, interval_s=300, stale_after_s=605, timeout_s=0.05
    )
    try:
        with caplog.at_level(logging.INFO, logger="processor.liveness"):
            await beat.tick()  # cut off: the post is still running
            await beat.tick()  # skipped
            release.set()
            threads[0].join(5)
            await beat.tick()
    finally:
        release.set()
    assert len(threads) == 2
    (failed,) = records(caplog, "liveness check-in failed")
    assert failed.error == "timed out after 0.05 s"
    assert len(records(caplog, "previous check-in still in flight; skipping this tick")) == 1
    (ok,) = records(caplog, "liveness check-in")
    assert ok.checkin == 202


async def test_a_finished_check_in_whose_thread_lingers_does_not_skip_the_next_tick(
    progress, clock, caplog, monkeypatch
):
    # #45: the result reaches the loop before its thread exits. A tick that awaits
    # nothing (a stale one) let the next run while that thread was still alive, and
    # it skipped. Here every check-in thread is held after its target returns.
    linger = threading.Event()

    class Lingering(threading.Thread):
        def run(self) -> None:
            super().run()
            if self.name == "liveness-checkin":
                linger.wait(5)

    monkeypatch.setattr(threading, "Thread", Lingering)
    posts = []
    beat = Heartbeat(
        progress,
        lambda v: posts.append(v) or 202,
        build="b",
        clock=clock,
        interval_s=300,
        stale_after_s=605,
        timeout_s=10,
    )
    try:
        with caplog.at_level(logging.INFO, logger="processor.liveness"):
            await beat.tick()
            await beat.tick()
    finally:
        linger.set()
    assert len(posts) == 2
    assert records(caplog, "previous check-in still in flight; skipping this tick") == []


async def test_a_thread_that_fails_to_start_does_not_wedge_the_heartbeat(
    progress, clock, caplog, monkeypatch
):
    # The in-flight marker is set before start(): a start that raises must clear it,
    # or every later tick would skip and the monitor would page for nothing.
    real_start = threading.Thread.start
    calls = 0

    def start(self) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    beat = Heartbeat(
        progress,
        lambda v: 202,
        build="b",
        clock=clock,
        interval_s=300,
        stale_after_s=605,
        timeout_s=10,
    )
    with caplog.at_level(logging.INFO, logger="processor.liveness"):
        await beat.tick()
        await beat.tick()
    (failed,) = records(caplog, "liveness check-in failed")
    assert failed.error == "RuntimeError: can't start new thread"
    (ok,) = records(caplog, "liveness check-in")
    assert ok.checkin == 202


async def test_ok_is_logged_on_the_first_and_after_a_failure_only(
    http_stub, progress, clock, caplog
):
    http_stub.route("POST", PATH, 202)
    beat = heartbeat(progress, http_stub.url, clock)
    with caplog.at_level(logging.INFO, logger="processor.liveness"):
        await beat.tick()
        await beat.tick()
        http_stub.route("POST", PATH, 503)
        await beat.tick()
        http_stub.route("POST", PATH, 202)
        await beat.tick()
        clock.now += 1000  # stale: silence
        await beat.tick()
        progress.last_progress = clock.now
        await beat.tick()
    ok = records(caplog, "liveness check-in")
    assert [r.levelno for r in ok] == [logging.INFO] * 3
    assert all(r.checkin == 202 for r in ok)


async def test_run_ticks_at_once_then_every_interval(http_stub, progress):
    http_stub.route("POST", PATH, 202)
    progress.last_progress = time.monotonic()
    beat = heartbeat(progress, http_stub.url, interval_s=0.1, timeout_s=0.05)
    task = asyncio.create_task(beat.run())
    try:
        async with asyncio.timeout(5):
            while len(http_stub.requests) < 3:
                progress.last_progress = time.monotonic()
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_ends_run_promptly_mid_check_in(hung_status, progress):
    progress.last_progress = time.monotonic()
    beat = heartbeat(progress, hung_status, timeout_s=30)
    task = asyncio.create_task(beat.run())
    await asyncio.sleep(0.2)  # the first check-in is in flight, for up to 30 s
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 0.5


async def test_a_tick_that_raises_is_logged_and_the_heartbeat_lives_on(caplog):
    # CR 2: a bug outside tick's own handlers ended the task silently; the monitor
    # went missing with no record why, and processor run's stop re-raised it.
    reads = []

    class Broken:
        backing_off = False

        @property
        def last_progress(self) -> float:
            reads.append(1)
            raise RuntimeError("bug")

    beat = Heartbeat(
        Broken(), lambda v: 202, build="b", interval_s=0.05, stale_after_s=605, timeout_s=0.04
    )
    with caplog.at_level(logging.INFO, logger="processor.liveness"):
        task = asyncio.create_task(beat.run())
        try:
            async with asyncio.timeout(5):
                while len(reads) < 3:
                    assert not task.done(), task
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    raised = records(caplog, "liveness tick raised")
    assert len(raised) >= 2 and raised[0].levelno == logging.ERROR
    assert raised[0].exc_info is not None
