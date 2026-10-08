"""``processor.drift``: does live lag origin/main in code that runs (#35)?

Ported from CannObserv/status's ``tests/core/test_drift.py`` (status#12), its CR
numbers kept. GitHub's answers are built here in the shapes its REST API returns:
the compare (``status``, ``total_commits``, ``commits``, ``files``) and the CI
workflow's runs. The verdicts are tested on those through a fake ``get``; the
getter that asks GitHub, and ``processor drift`` end to end, against local HTTP
stubs (``tests/conftest.py``), never the network.
"""

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from processor import drift
from processor.__main__ import main
from processor.drift import (
    COMPARE_FILES_LIMIT,
    GRACE,
    RUNS_PATH,
    GitHubNotFound,
    GitHubSilent,
    Push,
    Verdict,
    assess,
    counts,
    diff_counts,
    first_look,
    lagging,
    pushes,
    tip_ci,
)

NOW = datetime(2026, 10, 2, 22, 0, tzinfo=UTC)
LIVE = "deff23b0c287"


def sha(n: int) -> str:
    """A distinct full-length commit id."""
    return f"{n:040x}"


def commit(n: int, committed: datetime = NOW) -> dict:
    return {"sha": sha(n), "commit": {"committer": {"date": committed.isoformat()}}}


def compare(*shas: int, files=("src/processor/handler.py",), status="ahead", total=None) -> dict:
    """GitHub's ``compare/<live>...main``: *shas* oldest first, the last one main's."""
    answer = {
        "status": status,
        "total_commits": len(shas) if total is None else total,
        "commits": [commit(n) for n in shas],
    }
    if files is not None:
        answer["files"] = [{"filename": f} for f in files]
    return answer


def run(
    n: int,
    created: datetime,
    *,
    event="push",
    branch="main",
    status="completed",
    conclusion="success",
) -> dict:
    return {
        "head_sha": sha(n),
        "event": event,
        "head_branch": branch,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
    }


def runs(*items: dict) -> dict:
    return {"workflow_runs": list(items)}


class TestCounts:
    """What runs is an allowlist: AGENTS.md's deploy rule, defined once here."""

    @pytest.mark.parametrize(
        "path",
        [
            "src/processor/handler.py",
            "src/processor/drift.py",
            "deploy/processor.service",
            "deploy/processor-drift.timer",
            "scripts/deploy.sh",
            "pyproject.toml",
            "uv.lock",
        ],
    )
    def test_what_runs_counts(self, path):
        assert counts(path)

    @pytest.mark.parametrize(
        "path",
        [
            "docs/plans/2026-10-06-35-drift-signal.md",
            "AGENTS.md",
            "CLAUDE.md",
            "tests/test_drift.py",
            "tests/fixtures/parity/real/export.json",
            ".github/workflows/ci.yml",
            ".claude/settings.json",
            "skills/brainstorming",
            "skills-vendor/obra-superpowers",
            ".skills/doctor.sh",
            "scripts/smoke_scratch_bus.py",
            "scripts/gen_parity_goldens.py",
            ".gitmodules",
            "LICENSE",
            "srcery/x.py",
            "deployment.md",
        ],
    )
    def test_what_never_runs_does_not(self, path):
        assert not counts(path)


class TestDiffCounts:
    def test_docs_alone_do_not_count(self):
        assert not diff_counts(compare(1, files=["docs/a.md", "tests/test_a.py"]))

    def test_one_path_that_runs_is_enough(self):
        assert diff_counts(compare(1, files=["docs/a.md", "src/processor/drift.py"]))

    def test_a_file_moved_out_of_a_runtime_path_counts(self):
        """Its old path left the release: ``previous_filename`` says where it was."""
        answer = compare(1, files=[])
        answer["files"] = [{"filename": "docs/old.py", "previous_filename": "src/processor/x.py"}]
        assert diff_counts(answer)

    def test_no_file_list_counts(self):
        assert diff_counts(compare(1, files=None))

    def test_a_file_list_at_githubs_limit_counts(self):
        """GitHub cuts the list off there; what follows could be anything."""
        assert diff_counts(compare(1, files=["docs/a.md"] * COMPARE_FILES_LIMIT))

    def test_a_commit_list_cut_short_counts(self):
        assert diff_counts(compare(1, files=["docs/a.md"], total=300))


class TestVerdict:
    @pytest.mark.parametrize(
        ("kind", "status"),
        [("ok", "ok"), ("lag", "alert"), ("off_main", "alert"), ("unstamped", "alert"),
         ("test", "alert"), ("silent", None)],
    )  # fmt: skip
    def test_the_check_in_each_kind_sends(self, kind, status):
        assert Verdict(kind, "b", LIVE).status == status

    def test_every_check_in_carries_every_variable(self):
        """The monitor's template renders on any alert: none may be missing (status#24)."""
        assert Verdict("off_main", "b", LIVE).variables() == {
            "kind": "off_main",
            "live": LIVE,
            "main": "unknown",
            "body": "b",
        }


class TestFirstLook:
    def test_unstamped_alerts(self):
        verdict = first_look("dev", compare())
        assert (verdict.kind, verdict.status) == ("unstamped", "alert")
        assert "unstamped" in verdict.body

    def test_identical_is_ok(self):
        verdict = first_look(LIVE, compare(status="identical", files=[]))
        assert verdict == Verdict("ok", f"live {LIVE} is main", LIVE, LIVE)

    @pytest.mark.parametrize("status", ["diverged", "behind"])
    def test_live_not_on_main_alerts(self, status):
        verdict = first_look(LIVE, compare(1, status=status))
        assert verdict.kind == "off_main"
        assert f"live {LIVE} is not on main" in verdict.body
        assert status in verdict.body
        assert verdict.main == sha(1)[:12]

    def test_ahead_in_docs_alone_is_ok(self):
        verdict = first_look(LIVE, compare(1, 2, files=["docs/plans/x.md"]))
        assert verdict.kind == "ok"
        assert "2 commits ahead, none that runs" in verdict.body
        assert verdict.main == sha(2)[:12]

    def test_ahead_in_code_needs_the_clock(self):
        assert first_look(LIVE, compare(1)) is None


class TestPushes:
    def test_each_push_is_its_runs_creation_oldest_first(self):
        found = pushes(
            compare(1, 2, 3),
            runs(run(3, NOW - timedelta(hours=1)), run(1, NOW - timedelta(hours=5))),
        )
        assert found == [
            Push(sha(1), NOW - timedelta(hours=5)),
            Push(sha(3), NOW - timedelta(hours=1)),
        ]

    def test_runs_of_deployed_commits_are_not_pushes_since(self):
        found = pushes(compare(2), runs(run(1, NOW - timedelta(days=2)), run(2, NOW)))
        assert found == [Push(sha(2), NOW)]

    def test_only_push_runs_on_main(self):
        found = pushes(
            compare(1, 2),
            runs(
                run(1, NOW - timedelta(hours=9), event="workflow_dispatch"),
                run(1, NOW - timedelta(hours=8), event="pull_request", branch="35-drift-signal"),
                run(2, NOW),
            ),
        )
        assert found == [Push(sha(2), NOW)]

    def test_a_commit_with_two_push_runs_was_pushed_at_the_first(self):
        found = pushes(compare(1), runs(run(1, NOW), run(1, NOW - timedelta(hours=2))))
        assert found == [Push(sha(1), NOW - timedelta(hours=2))]

    def test_a_tip_with_no_run_falls_back_to_its_commit_date(self):
        """``[skip ci]``: GitHub ran nothing, so the commit is the only clock."""
        answer = compare(1, 2)
        answer["commits"][1] = commit(2, NOW - timedelta(hours=3))
        found = pushes(answer, runs(run(1, NOW - timedelta(hours=4))))
        assert found == [
            Push(sha(1), NOW - timedelta(hours=4)),
            Push(sha(2), NOW - timedelta(hours=3)),
        ]

    def test_no_runs_at_all_is_the_tips_commit_date(self):
        answer = compare(1)
        answer["commits"][0] = commit(1, NOW - timedelta(hours=3))
        assert pushes(answer, runs()) == [Push(sha(1), NOW - timedelta(hours=3))]

    def test_a_tip_with_no_run_landed_no_earlier_than_the_pushes_under_it(self):
        """An old ``[skip ci]`` commit pushed on top: never sorted before main's pushes (CR 10)."""
        answer = compare(1, 2)
        answer["commits"][1] = commit(2, NOW - timedelta(days=3))
        found = pushes(answer, runs(run(1, NOW - timedelta(hours=2))))
        assert found[-1] == Push(sha(2), NOW - timedelta(hours=2)), "the walk relies on it"


class TestTipCi:
    def test_the_newest_push_runs_conclusion(self):
        answer = runs(
            run(2, NOW - timedelta(hours=2), conclusion="failure"),
            run(2, NOW - timedelta(hours=1), conclusion="success"),
            run(1, NOW, conclusion="cancelled"),
        )
        assert tip_ci(answer, sha(2)) == "success"

    def test_unfinished_is_its_status(self):
        assert tip_ci(runs(run(2, NOW, status="in_progress")), sha(2)) == "in_progress"

    def test_no_run(self):
        assert tip_ci(runs(run(1, NOW)), sha(2)) == "no run"


class TestLagging:
    def test_within_grace_is_ok(self):
        """The clock's push, never "in code since": unwalked, it may be docs only (CR 4)."""
        since = Push(sha(1), NOW - GRACE + timedelta(minutes=1))
        verdict = lagging(LIVE, compare(1, 2), since, ci="success", now=NOW)
        assert verdict == Verdict(
            "ok",
            f"live {LIVE}, main {sha(2)[:12]}: 2 commits ahead, the clock started at the push "
            "of 2026-10-02T14:01:00Z (8.0 h ago; grace 8 h)",
            LIVE,
            sha(2)[:12],
        )

    def test_past_grace_is_a_lag_naming_mains_ci(self):
        since = Push(sha(1), NOW - GRACE - timedelta(minutes=30))
        verdict = lagging(LIVE, compare(1), since, ci="failure", now=NOW)
        assert (verdict.kind, verdict.status) == ("lag", "alert")
        assert verdict.body.endswith(
            "(8.5 h ago; grace 8 h). main's CI: failure — deploy.sh refuses it until CI passes"
        )

    def test_past_grace_with_green_ci_says_deploy(self):
        since = Push(sha(1), NOW - timedelta(days=2))
        verdict = lagging(LIVE, compare(1), since, ci="success", now=NOW)
        assert verdict.body.endswith("main's CI: success — scripts/deploy.sh")

    def test_one_commit_is_singular(self):
        since = Push(sha(1), NOW)
        assert "1 commit ahead," in lagging(LIVE, compare(1), since, ci="success", now=NOW).body


class Unrouted(Exception):
    """A path the test did not expect asked; assess must not swallow it."""


class FakeGitHub:
    """``get`` for :func:`assess`: answers by path, records each call."""

    def __init__(self) -> None:
        self.answers: dict[str, object] = {}
        self.calls: list[str] = []

    def route(self, path: str, answer: object) -> None:
        self.answers[path] = answer

    def __call__(self, path: str):
        self.calls.append(path)
        if path not in self.answers:
            raise Unrouted(path)
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


def _route_compare(github: FakeGitHub, head: str, answer: dict) -> None:
    github.route(f"compare/{LIVE}...{head}", answer)


class TestAssess:
    def test_unstamped_asks_github_nothing(self, github):
        assert assess("dev", now=NOW, get=github).kind == "unstamped"
        assert not github.calls

    def test_in_sync_is_one_call(self, github):
        _route_compare(github, "main", compare(status="identical", files=[]))
        assert assess(LIVE, now=NOW, get=github).kind == "ok"
        assert len(github.calls) == 1

    def test_docs_alone_never_ask_for_runs(self, github):
        _route_compare(github, "main", compare(1, files=["docs/a.md"]))
        assert assess(LIVE, now=NOW, get=github).kind == "ok"
        assert len(github.calls) == 1

    def test_code_within_grace_is_two_calls(self, github):
        _route_compare(github, "main", compare(1))
        github.route(RUNS_PATH, runs(run(1, NOW - timedelta(hours=1))))
        verdict = assess(LIVE, now=NOW, get=github)
        assert verdict.kind == "ok"
        assert "1.0 h ago" in verdict.body
        assert len(github.calls) == 2

    def test_code_past_grace_is_a_lag(self, github):
        _route_compare(github, "main", compare(1))
        github.route(RUNS_PATH, runs(run(1, NOW - timedelta(hours=9))))
        verdict = assess(LIVE, now=NOW, get=github)
        assert verdict.kind == "lag"
        assert "main's CI: success" in verdict.body
        assert len(github.calls) == 2

    def test_old_docs_then_new_code_starts_the_clock_at_the_code(self, github):
        """Live sat behind a docs push for two days; code pushed an hour ago is not late."""
        _route_compare(github, "main", compare(1, 2))
        github.route(
            RUNS_PATH, runs(run(1, NOW - timedelta(days=2)), run(2, NOW - timedelta(hours=1)))
        )
        _route_compare(github, sha(1), compare(1, files=["docs/a.md"]))
        verdict = assess(LIVE, now=NOW, get=github)
        assert verdict.kind == "ok"
        assert "1.0 h ago" in verdict.body

    def test_old_code_then_new_docs_is_late(self, github):
        _route_compare(github, "main", compare(1, 2))
        github.route(
            RUNS_PATH, runs(run(1, NOW - timedelta(days=2)), run(2, NOW - timedelta(hours=1)))
        )
        _route_compare(github, sha(1), compare(1))
        verdict = assess(LIVE, now=NOW, get=github)
        assert verdict.kind == "lag"
        assert "48.0 h ago" in verdict.body

    @staticmethod
    def _docs_pushes_then_code(github: FakeGitHub, third: timedelta) -> None:
        """Pushes 4 and 3 days ago, docs only; a third *third* ago; main now."""
        _route_compare(github, "main", compare(1, 2, 3, 4))
        github.route(
            RUNS_PATH,
            runs(
                run(1, NOW - timedelta(days=4)),
                run(2, NOW - timedelta(days=3)),
                run(3, NOW - third),
                run(4, NOW),
            ),
        )
        for n in (1, 2, 3):
            _route_compare(github, sha(n), compare(n, files=["docs/a.md"]))

    def test_an_old_skip_ci_tip_on_new_code_is_not_late(self, github):
        """Code pushed 2 h ago under a [skip ci] commit written 3 days ago (CR 10)."""
        answer = compare(1, 2)
        answer["commits"][1] = commit(2, NOW - timedelta(days=3))
        _route_compare(github, "main", answer)
        github.route(RUNS_PATH, runs(run(1, NOW - timedelta(hours=2))))
        verdict = assess(LIVE, now=NOW, get=github)
        assert verdict.kind == "ok"
        assert "2.0 h ago" in verdict.body

    def test_the_walk_stops_at_the_first_push_inside_the_grace(self, github):
        """It, or a newer push, brought the code: ok either way, so ask no further (CR 14)."""
        _route_compare(github, "main", compare(1, 2, 3))
        github.route(
            RUNS_PATH,
            runs(
                run(1, NOW - timedelta(days=2)),
                run(2, NOW - timedelta(hours=1)),
                run(3, NOW),
            ),
        )
        _route_compare(github, sha(1), compare(1, files=["docs/a.md"]))
        verdict = assess(LIVE, now=NOW, get=github)
        assert verdict.kind == "ok"
        assert "1.0 h ago" in verdict.body
        assert f"compare/{LIVE}...{sha(2)}" not in github.calls

    def test_the_walk_stops_at_its_limit_at_the_first_push_unchecked(self, github, monkeypatch):
        """Pushes 1 and 2 are known not to count: the code came with 3 at the earliest (CR 1)."""
        monkeypatch.setattr(drift, "WALK_LIMIT", 2)
        self._docs_pushes_then_code(github, timedelta(days=2))
        verdict = assess(LIVE, now=NOW, get=github)
        assert verdict.kind == "lag", "the code may be as old as push 3"
        assert "48.0 h ago" in verdict.body
        assert len(github.calls) == 2 + 2

    def test_past_the_limit_recent_code_is_not_late(self, github, monkeypatch):
        monkeypatch.setattr(drift, "WALK_LIMIT", 2)
        self._docs_pushes_then_code(github, timedelta(hours=1))
        assert assess(LIVE, now=NOW, get=github).kind == "ok"


class TestAssessWhenGitHubCannotSay:
    """Silent: no check-in at all, so a long outage ends in Status's ``missing`` (#35 trap 3)."""

    def test_a_refusal_is_silent_with_githubs_message(self, github):
        _route_compare(github, "main", GitHubSilent("403 API rate limit exceeded for 1.2.3.4."))
        verdict = assess(LIVE, now=NOW, get=github)
        assert verdict == Verdict(
            "silent", "GitHub did not answer: 403 API rate limit exceeded for 1.2.3.4.", LIVE
        )
        assert verdict.status is None

    def test_live_unknown_to_github_is_off_main(self, github):
        """GitHub answers 404 for a base it does not know: not on main, said so (CR 2)."""
        _route_compare(github, "main", GitHubNotFound("404 Not Found"))
        verdict = assess(LIVE, now=NOW, get=github)
        assert verdict.kind == "off_main"
        assert verdict.body.startswith(f"GitHub does not know live {LIVE} (404 Not Found)")

    def test_a_404_on_anything_else_is_silent(self, github):
        _route_compare(github, "main", compare(1))
        github.route(RUNS_PATH, GitHubNotFound("404 Not Found"))
        verdict = assess(LIVE, now=NOW, get=github)
        assert verdict == Verdict("silent", "GitHub did not answer: 404 Not Found", LIVE)

    @pytest.mark.parametrize("answer", [{"status": "ahead"}, {"status": "ahead", "commits": 3}])
    def test_unexpected_json_is_silent(self, github, answer):
        _route_compare(github, "main", answer)
        verdict = assess(LIVE, now=NOW, get=github)
        assert verdict.kind == "silent"
        assert "not the JSON expected" in verdict.body

    def test_unexpected_json_leaves_its_traceback_in_the_journal(self, github, caplog):
        """A bug here would read as GitHub's fault without it (CR 3)."""
        _route_compare(github, "main", {"status": "ahead"})
        with caplog.at_level("WARNING"):
            assess(LIVE, now=NOW, get=github)
        (record,) = [r for r in caplog.records if r.name == drift.logger.name]
        assert record.exc_info and record.exc_info[0] is KeyError


class TestTheGetter:
    """``drift.github``: GitHub's REST API over HTTPS, unauthenticated, bounded."""

    def test_an_answer_is_its_json_object(self, http_stub):
        http_stub.route("GET", "/compare/a...main", body={"status": "identical"})
        assert drift.github(http_stub.url)("compare/a...main") == {"status": "identical"}
        (request,) = http_stub.requests
        assert request["headers"]["Accept"] == "application/vnd.github+json"
        assert "Authorization" not in request["headers"]

    def test_a_refusal_names_githubs_message(self, http_stub):
        http_stub.route("GET", "/x", 403, {"message": "API rate limit exceeded for 1.2.3.4."})
        with pytest.raises(GitHubSilent, match="^403 API rate limit exceeded for 1.2.3.4.$"):
            drift.github(http_stub.url)("x")

    def test_a_404_is_its_own_kind(self, http_stub):
        http_stub.route("GET", "/x", 404, {"message": "Not Found"})
        with pytest.raises(GitHubNotFound, match="^404 Not Found$"):
            drift.github(http_stub.url)("x")

    def test_an_error_page_that_is_not_json_is_its_status(self, http_stub):
        http_stub.route("GET", "/x", 502, b"<html>Bad gateway</html>")
        with pytest.raises(GitHubSilent, match="^502$"):
            drift.github(http_stub.url)("x")

    @pytest.mark.parametrize("body", [b"<html>", b"[]", b""], ids=repr)
    def test_an_answer_that_is_not_a_json_object_is_refused(self, http_stub, body):
        """As deploy.sh's gate (#34 CR 5): an empty or wrong-shaped 200 never passes."""
        http_stub.route("GET", "/x", 200, body)
        with pytest.raises(GitHubSilent, match="not a JSON object"):
            drift.github(http_stub.url)("x")

    def test_unreachable_is_silent(self):
        with pytest.raises(GitHubSilent, match="^ConnectionError"):
            drift.github("http://127.0.0.1:9")("x")

    def test_every_call_shares_one_deadline(self, http_stub, monkeypatch):
        """Bounded inside the unit's TimeoutStartSec, however many calls the walk makes."""
        # The deadline's clock is the test's (#40): 0.2 s of a real 0.3 s left a 1.5x margin.
        now = [0.0]
        monkeypatch.setattr(drift, "time", SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(drift, "CHECK_TIMEOUT_SECONDS", 10)
        http_stub.route("GET", "/x", body={})
        http_stub.route("GET", "/y", body={}, delay=0.5)
        get = drift.github(http_stub.url)
        get("x")
        now[0] = 9.875  # the walk so far took 9.875 s: this call gets the 0.125 s left, not 10 s
        with pytest.raises(GitHubSilent, match=r"read timeout=0\.125\)"):
            get("y")

    def test_past_the_deadline_no_call_starts(self, http_stub, monkeypatch):
        monkeypatch.setattr(drift, "CHECK_TIMEOUT_SECONDS", 0)
        with pytest.raises(GitHubSilent, match="^Timeout: no GitHub call starts past 0 s$"):
            drift.github(http_stub.url)("x")
        assert http_stub.requests == []


# --- processor drift, end to end ---------------------------------------------

MONITOR = "01M46EXP45TVCVAMQXK043N1Q7"
KEY = "sk-test-0123456789abcdef"
CHECKIN = f"/api/v1/monitors/{MONITOR}/checkin"
ACCEPTED = {"monitor_id": MONITOR, "state": "ok"}


class World:
    """GitHub and Status as local stubs; a release whose REVISION is ``LIVE``."""

    def __init__(self, tmp: Path, github, status, monkeypatch) -> None:
        self.github, self.status = github, status
        venv = tmp / "release" / ".venv"
        venv.mkdir(parents=True)
        (tmp / "release" / "REVISION").write_text(f"{LIVE}\n")
        self.credentials = tmp / "credentials"
        self.credentials.mkdir()
        (self.credentials / "status-checkin-key").write_text(f"{KEY}\n")
        monkeypatch.setattr("sys.prefix", str(venv))
        monkeypatch.setattr(drift, "GITHUB_API", github.url)
        monkeypatch.setenv("CO_PROCESSOR_STATUS_URL", status.url)
        monkeypatch.setenv("CO_PROCESSOR_DRIFT_MONITOR_ID", MONITOR)
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(self.credentials))
        monkeypatch.delenv("CO_PROCESSOR_BUS_URL", raising=False)
        status.route("POST", CHECKIN, 202, ACCEPTED)

    def lag(self, *, hours: float, files=("src/processor/handler.py",)) -> None:
        """main one push ahead of live, *hours* ago, touching *files*."""
        self.github.route("GET", f"/compare/{LIVE}...main", body=compare(1, files=files))
        self.github.route(
            "GET", f"/{RUNS_PATH}", body=runs(run(1, datetime.now(UTC) - timedelta(hours=hours)))
        )

    def checkins(self) -> list[dict]:
        return [json.loads(r["body"]) for r in self.status.requests]


@pytest.fixture
def world(tmp_path, http_stub, http_stub_2, monkeypatch, _restore_root_logging) -> World:
    return World(tmp_path, http_stub, http_stub_2, monkeypatch)


@pytest.fixture
def _restore_root_logging():
    # main() reconfigures the process-wide root logger; put pytest's back after.
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    logging.captureWarnings(False)
    root.handlers[:] = handlers
    root.setLevel(level)


def records(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().err.splitlines() if line]


def outcome(capsys) -> dict:
    (record,) = [r for r in records(capsys) if r["message"] == "drift check"]
    return record


class TestTheCommand:
    def test_a_runtime_lag_past_the_grace_alerts(self, world, capsys):
        world.lag(hours=9)
        assert main(["drift"]) == 0
        (checkin,) = world.checkins()
        assert checkin["status"] == "alert"
        assert checkin["variables"]["kind"] == "lag"
        assert checkin["variables"]["live"] == LIVE
        assert checkin["variables"]["main"] == sha(1)[:12]
        assert "main's CI: success — scripts/deploy.sh" in checkin["variables"]["body"]
        record = outcome(capsys)
        assert record["level"] == "WARNING"
        assert (record["build"], record["kind"], record["checkin"]) == (LIVE, "lag", 202)

    def test_a_docs_only_lag_is_ok_however_old(self, world, capsys):
        world.lag(hours=72, files=["docs/DEPLOYMENT.md", "tests/test_drift.py"])
        assert main(["drift"]) == 0
        (checkin,) = world.checkins()
        assert checkin["status"] == "ok"
        assert checkin["variables"]["kind"] == "ok"
        assert outcome(capsys)["level"] == "INFO"

    def test_a_runtime_lag_within_the_grace_is_ok(self, world):
        world.lag(hours=1)
        assert main(["drift"]) == 0
        assert world.checkins()[0]["status"] == "ok"

    def test_the_check_in_is_one_post_with_the_key_in_its_header_only(self, world, capsys):
        world.lag(hours=1)
        main(["drift"])
        (request,) = world.status.requests
        assert (request["method"], request["path"]) == ("POST", CHECKIN)
        assert request["headers"]["X-API-Key"] == KEY
        assert KEY not in request["body"].decode()
        assert KEY not in capsys.readouterr().err

    def test_github_silent_sends_nothing_and_fails(self, world, capsys):
        """Never a false ok: silence, which Status's grace turns into missing (trap 3)."""
        world.github.route("GET", f"/compare/{LIVE}...main", 403, {"message": "rate limited"})
        assert main(["drift"]) == 1
        assert world.status.requests == []
        record = outcome(capsys)
        assert (record["level"], record["kind"], record["checkin"]) == ("WARNING", "silent", None)
        assert "rate limited" in record["body"]

    @pytest.mark.parametrize(
        ("code", "body", "reason"),
        [
            (500, b"oops", "500"),
            (401, {"detail": "invalid API key"}, "401 invalid API key"),
            (422, {"detail": {"section": "title", "message": "undefined"}}, "422"),
            (200, {}, "200"),
        ],
    )
    def test_status_refusing_fails_once_with_no_retry(self, world, capsys, code, body, reason):
        world.lag(hours=1)
        world.status.route("POST", CHECKIN, code, body)
        assert main(["drift"]) == 1
        assert len(world.status.requests) == 1
        record = outcome(capsys)
        assert record["level"] == "ERROR"
        assert record["checkin"].startswith(f"failed: {reason}")

    def test_status_unreachable_fails(self, world, monkeypatch, capsys):
        """A Status outage costs this check its exit code, nothing else (trap 4)."""
        world.lag(hours=1)
        monkeypatch.setenv("CO_PROCESSOR_STATUS_URL", "http://127.0.0.1:9")
        assert main(["drift"]) == 1
        assert outcome(capsys)["checkin"].startswith("failed: ConnectionError")

    def test_no_key_fails_without_asking_status(self, world, capsys):
        (world.credentials / "status-checkin-key").write_text("\n")  # SetCredential's fallback
        world.lag(hours=1)
        assert main(["drift"]) == 1
        assert world.status.requests == []
        assert "status-checkin-key" in outcome(capsys)["checkin"]

    def test_a_malformed_key_never_reaches_the_journal(self, world, capsys):
        """CR 1: a key pasted with a line break in it."""
        (world.credentials / "status-checkin-key").write_text(f"{KEY}\n{KEY}\n")
        world.lag(hours=1)
        assert main(["drift"]) == 1
        assert world.status.requests == []
        err = capsys.readouterr().err
        assert KEY not in err
        assert '"message": "drift check"' in err

    def test_no_monitor_id_fails_without_asking_status(self, world, monkeypatch, capsys):
        monkeypatch.delenv("CO_PROCESSOR_DRIFT_MONITOR_ID")
        world.lag(hours=1)
        assert main(["drift"]) == 1
        assert world.status.requests == []
        assert outcome(capsys)["checkin"] == "not sent: missing CO_PROCESSOR_DRIFT_MONITOR_ID"

    def test_both_missing_are_named_in_one_line(self, world, monkeypatch, capsys):
        """CR 3: the line an operator reads at gate 3."""
        monkeypatch.delenv("CO_PROCESSOR_DRIFT_MONITOR_ID")
        (world.credentials / "status-checkin-key").write_text("\n")
        world.lag(hours=1)
        assert main(["drift"]) == 1
        assert outcome(capsys)["checkin"] == (
            "not sent: missing CO_PROCESSOR_DRIFT_MONITOR_ID, the status-checkin-key credential"
        )

    def test_a_monitor_id_that_is_not_a_ulid_is_invalid_settings(self, world, monkeypatch, capsys):
        monkeypatch.setenv("CO_PROCESSOR_DRIFT_MONITOR_ID", "../../admin")
        assert main(["drift"]) == 2
        assert world.status.requests == [] and world.github.requests == []
        assert any(r["message"] == "invalid settings" for r in records(capsys))

    def test_it_never_needs_the_broker_credential(self, world):
        assert "CO_PROCESSOR_BUS_URL" not in os.environ
        world.lag(hours=1)
        assert main(["drift"]) == 0

    def test_test_alert_sends_one_alert_and_asks_github_nothing(self, world, capsys):
        assert main(["drift", "--test-alert"]) == 0
        assert world.github.requests == []
        (checkin,) = world.checkins()
        assert checkin["status"] == "alert"
        assert checkin["variables"]["kind"] == "test"
        assert set(checkin["variables"]) == {"kind", "live", "main", "body"}
        assert outcome(capsys)["kind"] == "test"
