"""``processor.patching``: has this week's attended patch run been recorded (#113)?

The verdicts are tested on the release's knob text and on GitHub's issue-comment
answers, in the shapes its REST API returns, through a fake ``get``. The window parse
is held equal to the vendored skill's own reader, ``read-knob.sh``, which the release
doesn't carry. ``processor patching`` runs end to end against local HTTP stubs
(``tests/conftest.py``), never the network.
"""

import json
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from processor import drift, patching
from processor.__main__ import main
from processor.drift import GitHubSilent
from processor.patching import (
    PAGE_LIMIT,
    PER_PAGE,
    KnobUnreadable,
    Opening,
    Verdict,
    Window,
    assess,
    comments_path,
    is_record,
    opening,
    parse_windows,
)

ROOT = Path(__file__).resolve().parent.parent
KNOB = ROOT / ".skills" / "patching-hosts"
READ_KNOB = ROOT / "skills" / "patching-hosts" / "scripts" / "read-knob.sh"

TUE = "window Tue 05:00-10:00\n"
#: Tuesday 2026-11-10, the first window under #113.
OPEN = datetime(2026, 11, 10, 5, 0, tzinfo=UTC)
CLOSE = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
URL = "https://github.com/CannObserv/processor/issues/113#issuecomment-1"


def comment(
    at: datetime,
    *,
    body: str = "## Patch run record: co-processor, 2026-11-10 (maintenance lane)\n\nrest",
    association: str = "MEMBER",
    url: str = URL,
) -> dict:
    return {
        "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "author_association": association,
        "body": body,
        "html_url": url,
    }


class FakeGitHub:
    """A ``get`` that answers per path, recording each one asked."""

    def __init__(self) -> None:
        self.answers: dict[str, object] = {}
        self.asked: list[str] = []

    def route(self, path: str, answer) -> None:
        self.answers[path] = answer

    def __call__(self, path: str):
        self.asked.append(path)
        answer = self.answers.get(path, [])
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


# --- the window -------------------------------------------------------------


class TestParseWindows:
    def test_a_weekday_window(self):
        assert parse_windows(TUE) == [Window(1, None, 5 * 60, 10 * 60)]

    def test_a_week_of_the_month_and_a_wrap_past_midnight(self):
        (window,) = parse_windows("window Sun#2 22:00-02:00\n")
        assert window == Window(6, 2, 22 * 60, 26 * 60)

    def test_a_dated_window_opens_no_due_period(self):
        """A one-off, such as a bulletin's: it never makes a week's run due."""
        text = TUE + "window 2026-11-03 15:37-17:37 processor#113\n"
        assert parse_windows(text) == parse_windows(TUE)

    def test_comments_and_other_directives_are_skipped(self):
        text = "# a comment\nclass production\nposture automatic\n" + TUE + "health x # y\n"
        assert parse_windows(text) == parse_windows(TUE)

    def test_a_trailing_comment_on_the_window_line(self):
        assert parse_windows("window Tue 05:00-10:00  # Monday night Pacific\n") == parse_windows(
            TUE
        )

    @pytest.mark.parametrize(
        "line",
        [
            "window Tue 05:00-05:00",  # ends where it starts
            "window Tue 24:00-02:00",
            "window Tue 5:00-10:00",
            "window Tues 05:00-10:00",
            "window Tue#6 05:00-10:00",
            "window Tue 05:00-10:00 extra",
            "window Tue",
        ],
    )
    def test_a_window_the_skill_would_refuse_is_unreadable(self, line):
        with pytest.raises(KnobUnreadable, match="line 1"):
            parse_windows(line + "\n")

    def test_a_host_section_is_unreadable(self):
        """Sections resolve by hostname and specificity; this reads global lines only."""
        with pytest.raises(KnobUnreadable, match=r"\[host"):
            parse_windows(TUE + "[host co-*]\n" + "window Wed 05:00-10:00\n")

    def test_no_weekday_window_is_unreadable(self):
        with pytest.raises(KnobUnreadable, match="no weekday window"):
            parse_windows("window 2026-11-03 15:37-17:37\n")


def _read_knob(path: Path) -> list[tuple]:
    out = subprocess.run(
        ["bash", str(READ_KNOB), "--config", str(path), "--host", "co-processor"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    knob = json.loads(out)
    assert knob["findings"] == [], knob["findings"]
    return [
        (w["weekday"], w["week"], w["start"], w["end"], w["wraps"])
        for w in knob["window"]
        if w["date"] is None
    ]


def _ours(text: str) -> list[tuple]:
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    return [
        (
            days[w.weekday],
            w.nth,
            f"{w.start // 60:02d}:{w.start % 60:02d}",
            f"{w.end // 60 % 24:02d}:{w.end % 60:02d}",
            w.end > 24 * 60,
        )
        for w in parse_windows(text)
    ]


@pytest.mark.parametrize(
    "text",
    [
        TUE,
        "window Sun#2 22:00-02:00\nwindow Wed 13:15-14:45\n",
        "window Mon 23:00-05:00\nwindow 2026-12-01 01:00-02:00 bulletin\n",
    ],
    ids=["weekly", "nth-and-two", "wrap-and-dated"],
)
def test_the_parse_agrees_with_the_skills_reader(tmp_path, text):
    """The release carries the knob but not ``read-knob.sh`` (the submodule is empty in
    a ``git archive``), so the check parses the knob itself. CI has the submodule."""
    assert READ_KNOB.is_file(), f"{READ_KNOB} missing: run `bash .skills/doctor.sh`"
    knob = tmp_path / "knob"
    knob.write_text("class production\nposture automatic\n" + text)
    assert _ours(knob.read_text()) == _read_knob(knob)


def test_the_committed_knob_agrees_with_the_skills_reader():
    assert _ours(KNOB.read_text()) == _read_knob(KNOB)


class TestOpening:
    def test_inside_the_window(self):
        now = OPEN + timedelta(hours=1)
        assert opening(parse_windows(TUE), now) == Opening(OPEN, CLOSE)

    def test_at_the_opening_minute(self):
        assert opening(parse_windows(TUE), OPEN) == Opening(OPEN, CLOSE)

    def test_a_minute_before_it_is_last_weeks(self):
        last = Opening(OPEN - timedelta(days=7), CLOSE - timedelta(days=7))
        assert opening(parse_windows(TUE), OPEN - timedelta(minutes=1)) == last

    def test_the_week_after_the_window_is_still_this_one(self):
        now = OPEN + timedelta(days=6, hours=23)
        assert opening(parse_windows(TUE), now) == Opening(OPEN, CLOSE)

    def test_a_wrap_closes_the_next_day(self):
        windows = parse_windows("window Mon 23:00-05:00\n")
        start = datetime(2026, 11, 9, 23, 0, tzinfo=UTC)
        assert opening(windows, start + timedelta(hours=2)) == Opening(
            start, start + timedelta(hours=6)
        )

    def test_the_nth_weekday_of_the_month(self):
        """``Sun#2`` is the 8th to the 14th: in November 2026, the 8th."""
        windows = parse_windows("window Sun#2 05:00-10:00\n")
        start = datetime(2026, 11, 8, 5, 0, tzinfo=UTC)
        assert opening(windows, datetime(2026, 11, 30, tzinfo=UTC)).start == start
        assert opening(windows, start - timedelta(minutes=1)).start == datetime(
            2026, 10, 11, 5, 0, tzinfo=UTC
        )

    def test_a_fifth_week_window_across_the_months_without_one(self):
        """CR 1: ``Sun#5`` opened 2026-08-30, then not until 2026-11-29: 91 days."""
        windows = parse_windows("window Sun#5 05:00-10:00\n")
        now = datetime(2026, 11, 28, tzinfo=UTC)
        assert opening(windows, now).start == datetime(2026, 8, 30, 5, 0, tzinfo=UTC)

    def test_the_latest_of_two_windows(self):
        windows = parse_windows(TUE + "window Fri 05:00-10:00\n")
        now = datetime(2026, 11, 13, 6, 0, tzinfo=UTC)
        assert opening(windows, now).start == datetime(2026, 11, 13, 5, 0, tzinfo=UTC)


# --- the record -------------------------------------------------------------


class TestIsRecord:
    def test_a_members_record_since_the_opening(self):
        assert is_record(comment(OPEN + timedelta(hours=2)), OPEN)

    def test_posted_at_the_opening_second(self):
        assert is_record(comment(OPEN), OPEN)

    def test_last_weeks_record_is_not_this_weeks(self):
        assert not is_record(comment(OPEN - timedelta(seconds=1)), OPEN)

    @pytest.mark.parametrize("association", ["OWNER", "MEMBER"])
    def test_the_owners_side(self, association):
        assert is_record(comment(OPEN, association=association), OPEN)

    @pytest.mark.parametrize(
        "association", ["COLLABORATOR", "CONTRIBUTOR", "FIRST_TIMER", "NONE", None]
    )
    def test_anyone_else_on_a_public_repo_never_silences_it(self, association):
        assert not is_record(comment(OPEN, association=association), OPEN)

    @pytest.mark.parametrize(
        "body",
        [
            "## Patch run record: co-processor\n",
            "## Patch run record: co-processor, 2026-11-10\r\n\r\nrest",
            "## Patch run record: co-processor (nothing pending)",
        ],
    )
    def test_its_first_line_names_the_host(self, body):
        assert is_record(comment(OPEN, body=body), OPEN)

    @pytest.mark.parametrize(
        "body",
        [
            "## Patch run record: co-processor-2, 2026-11-10",
            "## Patch run record: co-broker, 2026-11-10",
            "Before the run:\n## Patch run record: co-processor",
            "> ## Patch run record: co-processor",
            "",
            None,
        ],
    )
    def test_anything_else_is_not_a_record(self, body):
        assert not is_record(comment(OPEN, body=body), OPEN)


def test_the_comments_read_is_newest_first_since_the_opening():
    assert comments_path(OPEN, 2) == (
        f"issues/comments?since=2026-11-10T05:00:00Z&sort=created&direction=desc"
        f"&per_page={PER_PAGE}&page=2"
    )


# --- the verdict ------------------------------------------------------------


class TestAssess:
    def test_a_record_since_the_opening_is_ok(self, github):
        github.route(comments_path(OPEN, 1), [comment(OPEN + timedelta(hours=1))])
        verdict = assess(TUE, now=OPEN + timedelta(hours=2), get=github)
        assert verdict.kind == "ok"
        assert verdict.status == "ok"
        assert verdict.record == URL
        assert verdict.window == "2026-11-10T05:00:00Z"
        assert URL in verdict.body
        assert verdict.metadata() is None

    def test_the_window_open_with_no_record_is_due(self, github):
        verdict = assess(TUE, now=OPEN + timedelta(seconds=30), get=github)
        assert verdict.kind == "due"
        assert verdict.status == "alert"
        assert verdict.metadata() == {"fault": "due"}
        assert verdict.record == "none"
        assert "10:00Z" in verdict.body

    def test_the_window_closed_with_no_record_is_missed(self, github):
        verdict = assess(TUE, now=CLOSE, get=github)
        assert verdict.kind == "missed"
        assert verdict.metadata() == {"fault": "missed"}

    def test_missed_lasts_until_the_next_opening(self, github):
        assert assess(TUE, now=OPEN + timedelta(days=6), get=github).kind == "missed"

    def test_last_weeks_record_covers_the_week_before_the_window(self, github):
        last = OPEN - timedelta(days=7)
        github.route(comments_path(last, 1), [comment(last + timedelta(hours=10))])
        assert assess(TUE, now=OPEN - timedelta(minutes=1), get=github).kind == "ok"

    def test_a_strangers_record_is_passed_over(self, github):
        github.route(
            comments_path(OPEN, 1),
            [
                comment(OPEN + timedelta(hours=1), association="NONE", url="spam"),
                comment(OPEN + timedelta(minutes=30)),
            ],
        )
        verdict = assess(TUE, now=OPEN + timedelta(hours=2), get=github)
        assert (verdict.kind, verdict.record) == ("ok", URL)

    def test_a_full_page_reads_the_next(self, github):
        noise = [comment(OPEN + timedelta(hours=1), body="chatter")] * PER_PAGE
        github.route(comments_path(OPEN, 1), noise)
        github.route(comments_path(OPEN, 2), [comment(OPEN + timedelta(minutes=5))])
        assert assess(TUE, now=OPEN + timedelta(hours=2), get=github).kind == "ok"
        assert github.asked == [comments_path(OPEN, 1), comments_path(OPEN, 2)]

    def test_a_short_page_ends_the_read(self, github):
        github.route(comments_path(OPEN, 1), [comment(OPEN, body="chatter")])
        assess(TUE, now=OPEN + timedelta(hours=2), get=github)
        assert github.asked == [comments_path(OPEN, 1)]

    def test_past_the_page_limit_it_cannot_say(self, github):
        """Never a false alert or ok: the record may be on a page not read."""
        noise = [comment(OPEN + timedelta(hours=1), body="chatter")] * PER_PAGE
        for page in range(1, PAGE_LIMIT + 1):
            github.route(comments_path(OPEN, page), noise)
        verdict = assess(TUE, now=OPEN + timedelta(hours=2), get=github)
        assert verdict.kind == "silent"
        assert verdict.status is None
        assert len(github.asked) == PAGE_LIMIT

    def test_an_unreadable_knob_alerts_and_asks_github_nothing(self, github):
        verdict = assess("window Tue 5-10\n", now=OPEN, get=github)
        assert verdict.kind == "knob"
        assert verdict.status == "alert"
        assert "line 1" in verdict.body
        assert github.asked == []

    def test_github_silent_is_silent(self, github):
        github.route(comments_path(OPEN, 1), GitHubSilent("403 rate limited"))
        verdict = assess(TUE, now=OPEN + timedelta(hours=1), get=github)
        assert verdict == Verdict(
            "silent", "GitHub did not answer: 403 rate limited", "2026-11-10T05:00:00Z"
        )

    @pytest.mark.parametrize("answer", [[{"body": "x"}], [None], ["x"]])
    def test_unexpected_json_is_silent_with_its_traceback(self, github, caplog, answer):
        github.route(comments_path(OPEN, 1), answer)
        with caplog.at_level("WARNING"):
            verdict = assess(TUE, now=OPEN + timedelta(hours=1), get=github)
        assert verdict.kind == "silent"
        assert "not the JSON expected" in verdict.body
        (record,) = [r for r in caplog.records if r.name == patching.logger.name]
        assert record.exc_info

    def test_every_check_in_carries_every_variable(self, github):
        for now in (OPEN, CLOSE):
            verdict = assess(TUE, now=now, get=github)
            assert set(verdict.variables()) == {"kind", "window", "record", "body"}


class TestTheGetterTakesALists:
    """``drift.github`` takes an array where the caller asks for one."""

    def test_an_array_when_asked_for(self, http_stub):
        http_stub.route("GET", "/issues/comments", body=[{"a": 1}])
        assert drift.github(http_stub.url, expect=list)("issues/comments") == [{"a": 1}]

    def test_an_object_where_an_array_is_asked_for_is_silent(self, http_stub):
        http_stub.route("GET", "/x", body={"a": 1})
        with pytest.raises(GitHubSilent, match="not a JSON array"):
            drift.github(http_stub.url, expect=list)("x")

    def test_the_default_still_wants_an_object(self, http_stub):
        http_stub.route("GET", "/x", body=[1])
        with pytest.raises(GitHubSilent, match="not a JSON object"):
            drift.github(http_stub.url)("x")


# --- processor patching, end to end ------------------------------------------

MONITOR = "01M46EXP45TVCVAMQXK043N1Q8"
KEY = "sk-test-0123456789abcdef"
CHECKIN = f"/api/v1/monitors/{MONITOR}/checkin"
ACCEPTED = {"monitor_id": MONITOR, "state": "ok"}


class World:
    """GitHub and Status as local stubs; a release holding a knob, as its cwd."""

    def __init__(self, tmp: Path, github, status, monkeypatch) -> None:
        self.github, self.status = github, status
        release = tmp / "release"
        (release / ".skills").mkdir(parents=True)
        self.knob = release / ".skills" / "patching-hosts"
        self.knob.write_text("class production\nposture automatic\n" + TUE)
        self.credentials = tmp / "credentials"
        self.credentials.mkdir()
        (self.credentials / "status-checkin-key").write_text(f"{KEY}\n")
        monkeypatch.chdir(release)
        monkeypatch.setattr(patching, "GITHUB_API", github.url)
        monkeypatch.setenv("CO_PROCESSOR_STATUS_URL", status.url)
        monkeypatch.setenv("CO_PROCESSOR_PATCHING_MONITOR_ID", MONITOR)
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(self.credentials))
        monkeypatch.delenv("CO_PROCESSOR_BUS_URL", raising=False)
        status.route("POST", CHECKIN, 202, ACCEPTED)

    @property
    def path(self) -> str:
        """The first comments page this week's check asks for (the stub routes the
        query string too). The opening moves only at a window's start."""
        start = opening(parse_windows(TUE), datetime.now(UTC)).start
        return "/" + comments_path(start, 1)

    def comments(self, *items: dict) -> None:
        self.github.route("GET", self.path, body=list(items))

    def checkins(self) -> list[dict]:
        return [json.loads(r["body"]) for r in self.status.requests]


@pytest.fixture
def world(tmp_path, http_stub, http_stub_2, monkeypatch, _restore_root_logging) -> World:
    return World(tmp_path, http_stub, http_stub_2, monkeypatch)


@pytest.fixture
def _restore_root_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    logging.captureWarnings(False)
    root.handlers[:] = handlers
    root.setLevel(level)


def records(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().err.splitlines() if line]


def outcome(capsys) -> dict:
    (record,) = [r for r in records(capsys) if r["message"] == "patching check"]
    return record


class TestTheCommand:
    def test_a_record_this_week_checks_in_ok(self, world, capsys):
        world.comments(comment(datetime.now(UTC)))
        assert main(["patching"]) == 0
        (checkin,) = world.checkins()
        assert checkin["status"] == "ok"
        assert checkin["variables"]["kind"] == "ok"
        assert checkin["variables"]["record"] == URL
        assert "metadata" not in checkin
        record = outcome(capsys)
        assert (record["level"], record["kind"], record["checkin"]) == ("INFO", "ok", 202)
        (request,) = world.github.requests
        assert "since=" in request["path"] and "sort=created" in request["path"]

    def test_no_record_alerts_with_its_fault(self, world, capsys):
        world.comments()
        assert main(["patching"]) == 0
        (checkin,) = world.checkins()
        assert checkin["status"] == "alert"
        assert checkin["variables"]["kind"] in ("due", "missed")
        assert checkin["metadata"] == {"fault": checkin["variables"]["kind"]}
        assert outcome(capsys)["level"] == "WARNING"

    def test_the_check_in_is_one_post_with_the_key_in_its_header_only(self, world, capsys):
        world.comments()
        main(["patching"])
        (request,) = world.status.requests
        assert (request["method"], request["path"]) == ("POST", CHECKIN)
        assert request["headers"]["X-API-Key"] == KEY
        assert KEY not in request["body"].decode()
        assert KEY not in capsys.readouterr().err

    def test_github_silent_sends_nothing_and_fails(self, world, capsys):
        world.github.route("GET", world.path, 403, {"message": "rate limited"})
        assert main(["patching"]) == 1
        assert world.status.requests == []
        record = outcome(capsys)
        assert (record["level"], record["kind"], record["checkin"]) == ("WARNING", "silent", None)

    def test_a_missing_knob_alerts(self, world, capsys):
        world.knob.unlink()
        assert main(["patching"]) == 0
        (checkin,) = world.checkins()
        assert checkin["variables"]["kind"] == "knob"
        assert world.github.requests == []

    def test_status_refusing_fails_once(self, world, capsys):
        world.comments()
        world.status.route("POST", CHECKIN, 500, b"oops")
        assert main(["patching"]) == 1
        assert len(world.status.requests) == 1
        assert outcome(capsys)["checkin"].startswith("failed: 500")

    def test_no_monitor_id_fails_without_asking_status(self, world, monkeypatch, capsys):
        monkeypatch.delenv("CO_PROCESSOR_PATCHING_MONITOR_ID")
        world.comments()
        assert main(["patching"]) == 1
        assert world.status.requests == []
        assert outcome(capsys)["checkin"] == "not sent: missing CO_PROCESSOR_PATCHING_MONITOR_ID"

    def test_no_key_fails_without_asking_status(self, world, capsys):
        (world.credentials / "status-checkin-key").write_text("\n")
        world.comments()
        assert main(["patching"]) == 1
        assert world.status.requests == []
        assert "status-checkin-key" in outcome(capsys)["checkin"]

    def test_a_monitor_id_that_is_not_a_ulid_is_invalid_settings(self, world, monkeypatch, capsys):
        monkeypatch.setenv("CO_PROCESSOR_PATCHING_MONITOR_ID", "../../admin")
        assert main(["patching"]) == 2
        assert world.status.requests == [] and world.github.requests == []

    def test_it_never_needs_the_broker_credential(self, world):
        assert "CO_PROCESSOR_BUS_URL" not in os.environ
        world.comments()
        assert main(["patching"]) == 0

    def test_test_alert_sends_one_alert_and_asks_github_nothing(self, world, capsys):
        assert main(["patching", "--test-alert"]) == 0
        assert world.github.requests == []
        (checkin,) = world.checkins()
        assert checkin["status"] == "alert"
        assert checkin["variables"]["kind"] == "test"
        assert set(checkin["variables"]) == {"kind", "window", "record", "body"}
        assert checkin["metadata"] == {"fault": "test"}
        assert outcome(capsys)["kind"] == "test"
