"""The patching-hosts knob reads clean, and keeps the decisions it records (#77).

``.skills/patching-hosts`` is what the vendored ``patching-hosts`` skill reads to
learn what only this repo's owner knows: the window, the service the reboot chain
stops, the origins its maintenance lane follows. **A malformed line makes the host
report-only** until it is fixed, which reads as "nothing to do" at the next monthly
run rather than as an error, so the skill's own reader is run here. CI initialises
the submodule (tests/test_skills.py walks it too), so a missing reader fails.

The rest are pure parses of the file. Ported from CannObserv/broker#67.
"""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOB = ROOT / ".skills" / "patching-hosts"
READ_KNOB = ROOT / "skills" / "patching-hosts" / "scripts" / "read-knob.sh"
APT = ROOT / "deploy" / "apt"
HOST = "co-processor"


def _directives() -> list[list[str]]:
    """Each directive line's words. ``#`` opens a comment at a line's start or after
    whitespace (the skill's knob.md), so a command keeps a bare ``#``."""
    lines = []
    for raw in KNOB.read_text().splitlines():
        words = []
        for word in raw.split():
            if word.startswith("#"):
                break
            words.append(word)
        if words:
            lines.append(words)
    return lines


def _named(directive: str) -> list[list[str]]:
    return [words[1:] for words in _directives() if words[0] == directive]


def _resolved() -> dict:
    assert READ_KNOB.is_file(), f"{READ_KNOB} missing: run `bash .skills/doctor.sh`"
    out = subprocess.run(
        ["bash", str(READ_KNOB), "--config", str(KNOB), "--host", HOST],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def test_the_knob_reads_clean() -> None:
    knob = _resolved()
    assert knob["findings"] == []
    assert knob["report_only"] is False, knob["report_only_reasons"]
    assert knob["class"]["value"] == "production"
    assert knob["posture"]["value"] == "scheduled"


def test_the_window_is_the_owners() -> None:
    """Monday 23:00 to Tuesday 05:00, the broker's hours, on a Monday the broker's
    run doesn't use (operator, 2026-10-26). Every step must fit inside it."""
    window = _resolved()["window"]
    assert [(w["weekday"], w["start"], w["end"]) for w in window] == [("Mon", "23:00", "05:00")]


def test_no_quiet_range_is_declared() -> None:
    """processor-drift.timer's ticks drift (OnUnitActiveSec=1h), so no fixed range
    fits them; liveness runs inside the service."""
    assert _named("quiet") == []


def test_no_knob_command_escalates() -> None:
    """``health`` and ``inflight`` run as the invoking user by design; a ``sudo`` in
    one would hand root to whoever commits the knob."""
    commands = [" ".join(words) for words in _named("health") + _named("inflight")]
    assert commands, "the knob declares no health line"
    for command in commands:
        for word in ("sudo", "runuser", "pkexec", "systemd-run", "/etc/processor"):
            assert word not in command, f"{word!r} in a knob command: {command}"


def test_the_reboot_chain_stops_the_service() -> None:
    assert _named("service") == [["processor.service"]]


def test_the_scratch_redis_is_no_datastore() -> None:
    """``localhost:6379`` holds test data only: no recovery point. Its packages stay
    the skill's default ``redis`` held step, since their postinst restarts it."""
    assert _named("datastore") == []
    assert _named("backup") == []
    assert not any(words[-1] == "redis" for words in _named("hold"))


def test_each_third_party_source_is_followed_by_its_site() -> None:
    """A third-party origin with no line is a finding. NodeSource is named by its
    site: its ``o=`` field, ``. nodistro``, is aptly's generic one."""
    sources = (APT / "nodesource.sources").read_text()
    nodesource = re.search(r"^URIs: https://([^/]+)/", sources, re.M)
    tailscale = re.search(r"^Pin: origin (\S+)$", (APT / "tailscale.pref").read_text(), re.M)
    assert nodesource and tailscale
    assert sorted(_named("origin")) == sorted([[nodesource[1], "follow"], [tailscale[1], "follow"]])


def test_tailscale_is_its_own_held_step() -> None:
    """Its postinst restarts tailscaled, which carries the only path to the broker."""
    assert _named("hold") == [["tailscale", "tailscale"]]


def test_the_release_tree_is_this_repos() -> None:
    """``/srv/processor/live`` is an extract with no git origin, so the probe can't
    name its language-dependency owner without this line."""
    assert _named("owner") == [["/srv/processor/live", "self"]]


def test_the_runbook_sections_exist() -> None:
    """The knob, AGENTS.md and docs/SKILLS.md link DEPLOYMENT.md's sections by anchor."""
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
    for heading in ("## Patching", "### Patching Node"):
        assert re.search(rf"^{heading}$", deployment, re.M), heading
