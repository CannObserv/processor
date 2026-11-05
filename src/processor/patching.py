"""Has this week's attended patch run been recorded? The patch-due check (#113).

Under ``posture automatic`` unattended-upgrades takes ``-security`` daily; the rest
(``-updates``, the followed origins, the held steps, any reboot or restart due) is an
attended run the owner approves in the knob's weekly window. Nothing else asks for it:
the first window, 2026-11-03, passed unnoticed. ``processor patching``, hourly from
``processor-patching.timer``, checks in to Status's ``co-processor-patching``
(:mod:`processor.checkin`), which pages:

- **ok** while a run record has been posted since the window last opened;
- **due** (alert) while the window is open and none has: the request for the run;
- **missed** (alert) once it has closed with none, until the next opening;
- **knob** (alert) when the window can't be read;
- **nothing** when GitHub can't say: silence, which the monitor's grace turns into
  ``missing``, never a false ok.

**The window** comes from the release's own knob (``.skills/patching-hosts``, a
``RUNTIME_FILES`` path). The skill's reader, ``read-knob.sh``, isn't in a release: a
``git archive`` leaves the submodule empty. So the weekday ``window`` lines are parsed
here with the skill's grammar, and ``tests/test_patching.py`` holds the parse equal to
the reader's. A dated line is a one-off (a bulletin's) and opens no due period.

**The record** is the one artifact every run leaves, a nothing-pending run's too
(run.md §7): an issue comment in this repo whose first line is
:data:`RECORD_HEADING`. The repo is public, so anyone can comment: only an ``OWNER``'s
or a ``MEMBER``'s counts. Read unauthenticated, newest first, since the opening: one
request an hour within the 60/h an address gets, shared with the drift check and the
deploy gate.
"""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from processor import drift
from processor.drift import GitHubSilent

logger = logging.getLogger(__name__)

#: This repo's REST API, the drift check's; here so a test can point it at a stub.
GITHUB_API = drift.GITHUB_API

RECORD_HEADING = "## Patch run record: co-processor"
#: The heading, then nothing that would make it another host's (``co-processor-2``).
_RECORD = re.compile(re.escape(RECORD_HEADING) + r"(?![\w-])")
#: GitHub's ``author_association`` values for the repo's own side.
TRUSTED = frozenset({"OWNER", "MEMBER"})
PER_PAGE = 100
#: Past this many full pages since the opening the record may be on one not read:
#: silent, never a guess.
PAGE_LIMIT = 3

DAY = 24 * 60
#: How far back an opening is looked for: a ``#5`` window, which most months lack,
#: opens up to 119 days after the last (every weekday, 2026-2040).
LOOKBACK_DAYS = 120
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAY = re.compile(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:#([1-5]))?")
_DATE = re.compile(r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])")
_HHMM = r"([01][0-9]|2[0-3]):([0-5][0-9])"
_RANGE = re.compile(_HHMM + "-" + _HHMM)

#: ``silent`` sends no check-in; ``ok`` sends ``ok``; every other kind an ``alert``.
Kind = Literal["ok", "due", "missed", "knob", "test", "silent"]
Get = Callable[[str], object]


@dataclass(frozen=True)
class Window:
    """A weekday window: Monday is 0; *nth* the week of the month (``Sun#2``) or None;
    *start* and *end* in minutes from that day's 00:00 UTC, *end* past :data:`DAY` when
    it wraps past midnight."""

    weekday: int
    nth: int | None
    start: int
    end: int

    def opens_on(self, day: date) -> bool:
        """Whether the window opens on *day*: its weekday, in its week of the month."""
        if day.weekday() != self.weekday:
            return False
        return self.nth is None or (day.day - 1) // 7 + 1 == self.nth


@dataclass(frozen=True)
class Opening:
    """One opening of a window: when it opened and when it closes."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class Verdict:
    """What to tell ``co-processor-patching``, and the variables its template renders."""

    kind: Kind
    body: str
    window: str = "unknown"
    record: str = "none"

    @property
    def status(self) -> Literal["ok", "alert"] | None:
        """The check-in's ``status``; ``None``: send none."""
        if self.kind == "silent":
            return None
        return "ok" if self.kind == "ok" else "alert"

    def variables(self) -> dict[str, str]:
        """Every variable on every check-in, so the monitor's template always renders."""
        return {"kind": self.kind, "window": self.window, "record": self.record, "body": self.body}

    def metadata(self) -> dict[str, str] | None:
        """An alert's fault, its kind; ``None`` on an ``ok`` (#58, status#28). ``due``
        then ``missed`` are two faults, so the window's closing reports at once."""
        return {"fault": self.kind} if self.status == "alert" else None


class KnobUnreadable(Exception):
    """The knob gives no weekday window this check can read."""


def parse_windows(text: str) -> list[Window]:
    """The weekday ``window`` lines of a knob's global scope, in the skill's grammar.

    ``#`` opens a comment at a line's start or after whitespace (knob.md). Raises
    :class:`KnobUnreadable` for a ``[host`` section (resolved by hostname and
    specificity, which this doesn't do), a ``window`` line the skill would call
    malformed, or no weekday window at all.
    """
    windows = []
    for number, raw in enumerate(text.splitlines(), start=1):
        words = []
        for word in raw.split():
            if word.startswith("#"):
                break
            words.append(word)
        if not words:
            continue
        if words[0].startswith("["):
            raise KnobUnreadable(f"line {number}: [host] sections are not read by this check")
        if words[0] != "window":
            continue
        if len(words) >= 3 and _DATE.fullmatch(words[1]) and _RANGE.fullmatch(words[2]):
            continue  # a one-off: it opens no due period
        day = _WEEKDAY.fullmatch(words[1]) if len(words) == 3 else None
        span = _RANGE.fullmatch(words[2]) if day else None
        if not (day and span):
            raise KnobUnreadable(f"line {number}: not a window this check can read")
        start = int(span[1]) * 60 + int(span[2])
        end = int(span[3]) * 60 + int(span[4])
        if end == start:
            raise KnobUnreadable(f"line {number}: the window ends where it starts")
        nth = int(day[2]) if day[2] else None
        windows.append(Window(_DAYS.index(day[1]), nth, start, end if end > start else end + DAY))
    if not windows:
        raise KnobUnreadable("no weekday window")
    return windows


def opening(windows: list[Window], now: datetime) -> Opening:
    """The latest opening of any of *windows* at or before *now*.

    Looks back :data:`LOOKBACK_DAYS`, so a ``#5`` week, which most months lack, is
    still found.
    """
    found: list[Opening] = []
    for back in range(LOOKBACK_DAYS + 1):
        day = (now - timedelta(days=back)).date()
        for window in windows:
            if not window.opens_on(day):
                continue
            start = datetime.combine(day, time(), tzinfo=UTC) + timedelta(minutes=window.start)
            if start <= now:
                found.append(Opening(start, start + timedelta(minutes=window.end - window.start)))
        if found:
            return max(found, key=lambda o: o.start)
    raise KnobUnreadable(f"no window opened in the last {LOOKBACK_DAYS} days")


def is_record(comment: Mapping, since: datetime) -> bool:
    """Whether an issue *comment* is a run record posted at or after *since*, by the
    repo's own side. Raises ``KeyError``/``ValueError`` on a shape GitHub never sends."""
    if comment["author_association"] not in TRUSTED:
        return False
    body = comment["body"] or ""
    first = body.splitlines()[0] if body else ""
    if not _RECORD.match(first):
        return False
    return _parse(comment["created_at"]) >= since


def comments_path(since: datetime, page: int) -> str:
    """The repo's issue comments updated since *since*, newest first: one page."""
    return (
        f"issues/comments?since={_stamp(since)}&sort=created&direction=desc"
        f"&per_page={PER_PAGE}&page={page}"
    )


def assess(knob: str, *, now: datetime, get: Get) -> Verdict:
    """The verdict at *now* from the *knob*'s text and GitHub, through *get*. Never
    raises for anything the knob or GitHub does."""
    try:
        this = opening(parse_windows(knob), now)
    except KnobUnreadable as exc:
        return Verdict("knob", f"the patching knob gives no window this check can read: {exc}")
    window = _stamp(this.start)
    try:
        return _assess(get, this, now)
    except GitHubSilent as exc:
        return Verdict("silent", f"GitHub did not answer: {exc}", window)
    except (KeyError, TypeError, IndexError, ValueError, AttributeError):
        logger.warning("patching check: GitHub's answer is not the JSON expected", exc_info=True)
        return Verdict("silent", "GitHub's answer is not the JSON expected", window)


def _assess(get: Get, this: Opening, now: datetime) -> Verdict:
    window = _stamp(this.start)
    closes = f"{this.end:%Y-%m-%d %H:%M}Z"
    for page in range(1, PAGE_LIMIT + 1):
        comments = get(comments_path(this.start, page))
        if not isinstance(comments, list):
            raise TypeError(f"comments page {page} is not a list")
        for comment in comments:
            if is_record(comment, this.start):
                url = comment["html_url"]
                return Verdict(
                    "ok", f"this week's patch run is recorded: {url}", window, record=url
                )
        if len(comments) < PER_PAGE:
            break
    else:
        return Verdict(
            "silent",
            f"more than {PAGE_LIMIT * PER_PAGE} comments since the window opened, "
            "and no record among them",
            window,
        )
    if now < this.end:
        return Verdict(
            "due",
            f"the patch window is open until {closes}, and this week's attended run "
            "isn't recorded: approve it in co-processor's session (docs/DEPLOYMENT.md "
            "§ Patching)",
            window,
        )
    return Verdict(
        "missed",
        f"the patch window closed at {closes} with no run recorded: run it out of window "
        "or record why not (docs/DEPLOYMENT.md § Patching)",
        window,
    )


def deliberate_alert() -> Verdict:
    """The deliberate alert ``processor patching --test-alert`` sends: proves the channels."""
    return Verdict("test", "a test alert from co-processor's patching check, sent by hand")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")
