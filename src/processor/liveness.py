"""The liveness heartbeat: ``processor run`` checks in to Status's ``co-processor-live`` (#39).

A task beside the consume loop, on the same event loop. Every ``interval_s`` it
checks in ``ok``, **only while the loop is making progress**: its last finished read,
handled message or backoff is no older than ``stale_after_s``. A stale loop gets
silence, never an ``alert``. Status goes ``missing`` past the monitor's grace, and
that is the page. So a wedged loop (a hung read, an endless walk) pages like a
stopped process, though the event loop stays up. An empty queue still turns the loop,
so it never reads as an outage.

**Never in consumption's way** (#39 trap 1, status#24 design question 3):
:func:`~processor.checkin.post_checkin` is blocking ``requests``, so each check-in runs
in a daemon thread, awaited for at most ``timeout_s``:
- not the default executor, which the handler's GCS calls use, and which
  ``asyncio.run`` waits for at exit;
- a check-in still running at the next tick skips that tick, so threads never pile up.
  "Running" means its ``post`` has not returned, not that its thread is alive: the
  thread hands back its result a moment before it exits (#45);
- one attempt per tick, never a retry loop;
- a failure is one WARNING record, and the monitor's grace absorbs a missed tick.

Nothing here raises into the loop.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

from processor.checkin import CheckinFailed

logger = logging.getLogger(__name__)


class Progress(Protocol):
    """What the heartbeat reads off the consumer."""

    @property
    def last_progress(self) -> float:
        """``time.monotonic()`` at the loop's last finished read, message or backoff."""

    @property
    def backing_off(self) -> bool:
        """Whether the loop's last step raised (a broker outage, say)."""


class Heartbeat:
    """Checks in ``ok`` every ``interval_s`` while ``progress`` is fresh; silent otherwise."""

    def __init__(
        self,
        progress: Progress,
        post: Callable[[dict[str, str]], int],
        *,
        build: str,
        interval_s: float,
        stale_after_s: float,
        timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._progress = progress
        self._post = post
        self._build = build
        self._interval_s = interval_s
        self._stale_after_s = stale_after_s
        self._timeout_s = timeout_s
        self._clock = clock
        # Set before a check-in thread starts; cleared by that thread once its post
        # returns, never by the timeout path (#39 trap 1, #45).
        self._in_flight = False
        # Whether the last tick checked in: an ok is logged only when it was not.
        self._ok = False

    async def run(self) -> None:
        """Tick at once, then every ``interval_s``, until cancelled. Never raises.

        A tick that raises (a bug outside its own handlers) is logged with its
        traceback, and the next tick runs: the heartbeat never ends silently (CR 2).
        """
        next_tick = time.monotonic()
        while True:
            try:
                await self.tick()
            except Exception:
                self._ok = False
                logger.exception("liveness tick raised")
            next_tick += self._interval_s
            await asyncio.sleep(max(0.0, next_tick - time.monotonic()))

    async def tick(self) -> None:
        """One check-in, or a logged reason for none. Never raises but on cancel."""
        age = self._clock() - self._progress.last_progress
        if age > self._stale_after_s:
            self._ok = False
            logger.warning(
                "consume loop stale; not checking in",
                extra={"last_progress_age_s": age, "stale_after_s": self._stale_after_s},
            )
            return
        if self._in_flight:
            self._ok = False
            logger.warning("previous check-in still in flight; skipping this tick")
            return
        variables = {
            "build": self._build,
            "last_progress_age_s": str(round(age)),
            "backing_off": str(self._progress.backing_off).lower(),
        }
        try:
            async with asyncio.timeout(self._timeout_s):
                code = await self._in_thread(variables)
        except TimeoutError:
            self._failed(f"timed out after {self._timeout_s} s")
            return
        except CheckinFailed as exc:
            self._failed(str(exc))
            return
        except Exception as exc:  # anything at all: never into the loop
            self._failed(f"{type(exc).__name__}: {exc}")
            return
        if not self._ok:
            logger.info("liveness check-in", extra={"checkin": code, **variables})
        self._ok = True

    def _failed(self, error: str) -> None:
        self._ok = False
        logger.warning("liveness check-in failed", extra={"error": error})

    def _in_thread(self, variables: dict[str, str]) -> asyncio.Future:
        """``post(variables)`` in a daemon thread; its future settles on the loop.

        Owns ``_in_flight``: set here before the thread starts, cleared by the thread
        once ``post`` returns and before the loop can see the result (#45). A post
        cut off by the timeout clears it only when it returns (#39 trap 1).
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def settle(result: object, error: BaseException | None) -> None:
            if future.done():  # cut off by the timeout, or cancelled
                return
            if error is None:
                future.set_result(result)
            else:
                future.set_exception(error)

        def target() -> None:
            try:
                result, error = self._post(variables), None
            except BaseException as exc:
                result, error = None, exc
            self._in_flight = False  # before the loop can see the result (#45)
            try:
                loop.call_soon_threadsafe(settle, result, error)
            except RuntimeError:
                pass  # the loop closed while this thread waited on Status

        self._in_flight = True
        try:
            threading.Thread(target=target, name="liveness-checkin", daemon=True).start()
        except BaseException:
            self._in_flight = False  # no thread will clear it
            raise
        return future
