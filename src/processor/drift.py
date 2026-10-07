"""Does live lag origin/main in code that runs? The drift check (#35).

Since #2, a merge changes nothing that runs until ``scripts/deploy.sh``, and
nothing else reports a merge that was never deployed. ``processor drift``, hourly
from ``processor-drift.timer``, asks GitHub how far the live release's
``REVISION`` is behind ``main``, and checks in to Status's ``co-processor-drift``
monitor (:mod:`processor.checkin`; CannObserv/status#24):

- **ok** while live is ``main``, behind only in paths that never run
  (:func:`counts`), or behind in code for no longer than :data:`GRACE`;
- **alert** once code has waited longer than that since the push that brought
  it, naming ``main``'s CI result (#34's gate refuses a red one); and at once
  when live is not on ``main``, or is unstamped;
- **nothing** when GitHub cannot say. A long outage goes silent, and the
  monitor's grace turns that into ``missing``: never a false ok.

Ported from CannObserv/status's ``src/core/drift.py`` (status#12), its CR
numbers kept. Where it differs: what runs is an allowlist, the verdict is a
check-in rather than a healthchecks.io ping, and the calls are synchronous.

The clock starts at the push, never the commit: a CI run's ``created_at`` is
when its push landed, and a commit can be days older than that. Unauthenticated,
like the deploy gate: the repo is public, and GitHub allows an address 60
requests an hour, shared with ``scripts/deploy.sh``. A run costs 1 in sync or
behind in docs, 2 behind in code, and at most 2 + :data:`WALK_LIMIT` past the
grace (status CR 15).
"""

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import requests

logger = logging.getLogger(__name__)

#: How long code may sit on ``main`` undeployed. Deploys are by hand; CI takes
#: about three minutes.
GRACE = timedelta(hours=8)
GITHUB_API = "https://api.github.com/repos/CannObserv/processor"
#: CI's push runs on main, newest first: when each push landed (#34's gate asks the same).
RUNS_PATH = "actions/workflows/ci.yml/runs?event=push&branch=main&per_page=100"
#: At most this many extra compares to find the push that brought code. Past
#: it, the first push left unchecked starts the clock: the earliest the code
#: can have come, so an alert sooner, never later (status CR 1).
WALK_LIMIT = 8
#: No call to GitHub starts past this. Each call is bounded per socket read by
#: ``REQUEST_TIMEOUT_SECONDS`` (requests' timeout is per read, not per request), so
#: one answer trickling in can run past it. The hard stop is the unit's
#: ``TimeoutStartSec=90``; a kill there sends no check-in: silence, never a false ok.
CHECK_TIMEOUT_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 10.0
#: GitHub lists at most this many files in a compare; a list this long may be
#: cut short, and what was cut could be anything.
COMPARE_FILES_LIMIT = 300

#: What a release runs: the package, the units, the deploy, the dependencies.
#: AGENTS.md's rule for when a merge needs ``scripts/deploy.sh``, defined here
#: once. Everything else (docs, tests, CI, skills, the other scripts) never runs.
RUNTIME_DIRS = ("src/", "deploy/")
RUNTIME_FILES = frozenset({"scripts/deploy.sh", "pyproject.toml", "uv.lock"})

UNSTAMPED = "dev"

#: ``silent`` sends no check-in; ``ok`` sends ``ok``; every other kind an ``alert``.
Kind = Literal["ok", "lag", "off_main", "unstamped", "test", "silent"]
Get = Callable[[str], Mapping]


@dataclass(frozen=True)
class Verdict:
    """What to tell ``co-processor-drift``, and the variables its template renders."""

    kind: Kind
    body: str
    live: str
    main: str = "unknown"

    @property
    def status(self) -> Literal["ok", "alert"] | None:
        """The check-in's ``status``; ``None``: send none."""
        if self.kind == "silent":
            return None
        return "ok" if self.kind == "ok" else "alert"

    def variables(self) -> dict[str, str]:
        """Every variable on every check-in, so the monitor's template always renders."""
        return {"kind": self.kind, "live": self.live, "main": self.main, "body": self.body}


@dataclass(frozen=True)
class Push:
    """A push to ``main``: its newest commit, and when it landed."""

    sha: str
    at: datetime


def counts(path: str) -> bool:
    """Whether a change to *path* changes what a release runs."""
    return path in RUNTIME_FILES or path.startswith(RUNTIME_DIRS)


def diff_counts(compare: Mapping) -> bool:
    """Whether a GitHub compare touches a path that :func:`counts`, or may.

    A rename counts by either name: a file moved out of ``src/`` left the release.
    """
    files = compare.get("files")
    if files is None or len(files) >= COMPARE_FILES_LIMIT:
        return True
    if len(compare["commits"]) < compare["total_commits"]:
        return True
    return any(counts(f["filename"]) or counts(f.get("previous_filename", "")) for f in files)


def first_look(live: str, compare: Mapping) -> Verdict | None:
    """The verdict from ``compare/<live>...main`` alone, or ``None`` if it needs the clock."""
    if live == UNSTAMPED:
        return Verdict("unstamped", "live is unstamped (dev): its release has no REVISION", live)
    status = compare["status"]
    if status == "identical":
        return Verdict("ok", f"live {live} is main", live, live)
    main = _main(compare)
    if status != "ahead":
        return Verdict("off_main", f"live {live} is not on main (GitHub: {status})", live, main)
    if not diff_counts(compare):
        return Verdict("ok", f"{_ahead(live, compare)}, none that runs", live, main)
    return None


def pushes(compare: Mapping, runs: Mapping) -> list[Push]:
    """The pushes to ``main`` since live, oldest first, from CI's push runs.

    GitHub runs CI once per push, on its newest commit. A ``main`` with no run
    (``[skip ci]``) falls back to its commit date, the only clock left, but no
    earlier than any push under it: it landed after them, and the walk needs
    ``main``'s push last (status CR 10).

    ``main`` is the compare's last commit, up to the 250 commits a compare lists;
    past that the diff already counts (:func:`diff_counts`), but the tip named,
    its date and its CI may be an older commit's (status CR 6).
    """
    undeployed = {c["sha"] for c in compare["commits"]}
    landed: dict[str, datetime] = {}
    for run in runs["workflow_runs"]:
        if run["event"] != "push" or run["head_branch"] != "main":
            continue
        if run["head_sha"] not in undeployed:
            continue
        at = _parse(run["created_at"])
        landed[run["head_sha"]] = min(at, landed.get(run["head_sha"], at))
    tip = compare["commits"][-1]
    if tip["sha"] not in landed:
        committed = _parse(tip["commit"]["committer"]["date"])
        landed[tip["sha"]] = max([committed, *landed.values()])
    return sorted((Push(s, at) for s, at in landed.items()), key=lambda p: p.at)


def tip_ci(runs: Mapping, tip: str) -> str:
    """``main``'s CI result: its newest push run's conclusion, else its status."""
    mine = [
        r
        for r in runs["workflow_runs"]
        if r["head_sha"] == tip and r["event"] == "push" and r["head_branch"] == "main"
    ]
    if not mine:
        return "no run"
    newest = max(mine, key=lambda r: r["created_at"])
    if newest["status"] != "completed":
        return newest["status"]
    return newest["conclusion"] or "nothing"


def lagging(live: str, compare: Mapping, since: Push, *, ci: str, now: datetime) -> Verdict:
    """The verdict for live behind in code, the clock started at the push *since*.

    Within the grace nothing is walked, so *since* is the oldest push since live,
    which may be docs only: the body names the clock, never "the code since"
    (status CR 4).
    """
    age = now - since.at
    body = (
        f"{_ahead(live, compare)}, the clock started at the push of {_stamp(since.at)} "
        f"({age / timedelta(hours=1):.1f} h ago; grace {GRACE / timedelta(hours=1):.0f} h)"
    )
    if age <= GRACE:
        return Verdict("ok", body, live, _main(compare))
    remedy = "scripts/deploy.sh" if ci == "success" else "deploy.sh refuses it until CI passes"
    return Verdict("lag", f"{body}. main's CI: {ci} — {remedy}", live, _main(compare))


class GitHubSilent(Exception):
    """GitHub gave no answer this check can use."""


class GitHubNotFound(GitHubSilent):
    """GitHub answered 404."""


def assess(live: str, *, now: datetime, get: Get) -> Verdict:
    """Ask GitHub, through *get*, about *live*, the live release's build id. Never raises
    for anything GitHub does."""
    try:
        return _assess(get, live, now)
    except GitHubSilent as exc:
        return Verdict("silent", f"GitHub did not answer: {exc}", live)
    except (KeyError, TypeError, IndexError, ValueError, AttributeError):
        # GitHub's shape changed, or this module has a bug: the traceback says which
        # (status CR 3).
        logger.warning("drift check: GitHub's answer is not the JSON expected", exc_info=True)
        return Verdict("silent", "GitHub's answer is not the JSON expected", live)


def _assess(get: Get, live: str, now: datetime) -> Verdict:
    if live == UNSTAMPED:
        return first_look(live, {})
    try:
        compare = get(f"compare/{live}...main")
    except GitHubNotFound as exc:
        # A base GitHub does not know is live off main, not GitHub silent (status CR 2).
        return Verdict(
            "off_main",
            f"GitHub does not know live {live} ({exc}): main rewritten since the deploy, "
            "or the repo is no longer public",
            live,
        )
    verdict = first_look(live, compare)
    if verdict is not None:
        return verdict
    runs = get(RUNS_PATH)
    found = pushes(compare, runs)
    since = found[0]
    if now - since.at > GRACE:
        since = _first_counting(get, live, found, now)
    return lagging(live, compare, since, ci=tip_ci(runs, compare["commits"][-1]["sha"]), now=now)


def _first_counting(get: Get, live: str, found: list[Push], now: datetime) -> Push:
    """The oldest push whose diff from *live* counts; ``main``'s (the last) is known to.

    Past :data:`WALK_LIMIT`, the first push not asked about: every one before it
    is known not to count, so the code came with it at the earliest. A push
    inside the grace ends the walk too: it, or a newer one, brought the code,
    and the verdict is ok either way (status CR 14).
    """
    for push in found[:-1][:WALK_LIMIT]:
        if now - push.at <= GRACE:
            return push
        if diff_counts(get(f"compare/{live}...{push.sha}")):
            return push
    return found[min(WALK_LIMIT, len(found) - 1)]


def github(api: str | None = None) -> Get:
    """A ``get`` for :func:`assess`: GitHub's REST API, no call started past one deadline.

    Raises :class:`GitHubSilent` for anything but a JSON object: a refusal (with
    GitHub's message), an error page, a timeout, an empty or wrong-shaped 200.
    """
    base = (api or GITHUB_API).rstrip("/")
    deadline = time.monotonic() + CHECK_TIMEOUT_SECONDS

    def get(path: str) -> Mapping:
        left = deadline - time.monotonic()
        if left <= 0:
            raise GitHubSilent(f"Timeout: no GitHub call starts past {CHECK_TIMEOUT_SECONDS:.0f} s")
        try:
            response = requests.get(
                f"{base}/{path}",
                headers={"Accept": "application/vnd.github+json"},
                timeout=min(REQUEST_TIMEOUT_SECONDS, left),
            )
        except requests.RequestException as exc:
            raise GitHubSilent(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
        if not response.ok:
            try:
                message = response.json().get("message", "")
            except (ValueError, AttributeError):
                message = ""
            error = GitHubNotFound if response.status_code == 404 else GitHubSilent
            raise error(f"{response.status_code} {message}".strip())
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not isinstance(answer, Mapping):
            raise GitHubSilent(f"{response.status_code}, not a JSON object")
        return answer

    return get


def deliberate_alert(live: str) -> Verdict:
    """The deliberate alert ``processor drift --test-alert`` sends: proves the channels."""
    return Verdict(
        "test", f"a test alert from co-processor's drift check, sent by hand; live {live}", live
    )


def _main(compare: Mapping) -> str:
    return compare["commits"][-1]["sha"][:12]


def _ahead(live: str, compare: Mapping) -> str:
    n = compare["total_commits"]
    return f"live {live}, main {_main(compare)}: {n} commit{'' if n == 1 else 's'} ahead"


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")
