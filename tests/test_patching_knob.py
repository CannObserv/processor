"""The patching-hosts knob reads clean, and keeps the decisions it records (#77).

``.skills/patching-hosts`` is what the vendored ``patching-hosts`` skill reads to
learn what only this repo's owner knows: the window, the service the reboot chain
stops, the origins its maintenance lane follows. **A malformed line makes the host
report-only** until it is fixed, which reads as "nothing to do" at the next weekly
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
    """Weekly, Monday night Pacific, opening as the broker's Mon 23:00-05:00 closes
    (operator, 2026-10-26). Every step must fit inside it."""
    window = _resolved()["window"]
    assert [(w["weekday"], w["start"], w["end"]) for w in window] == [("Tue", "05:00", "10:00")]


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


def test_the_language_read_finds_the_live_lock() -> None:
    """The weekly window is the human read of the live ``uv.lock`` (#75). The probe
    finds a tree only through a unit's ``WorkingDirectory=``: one that holds no
    lockfile drops the tree, and the class still reads complete. So every unit runs
    from the link ``scripts/deploy.sh`` switches, and the ``owner`` line labels that
    tree ``self``, which points the record at this repo's lock audit."""
    deploy_sh = (ROOT / "scripts" / "deploy.sh").read_text()
    root = re.search(r'^ROOT="\$\{PROCESSOR_DEPLOY_ROOT:-(/[^}]+)\}"$', deploy_sh, re.M)
    assert root, "deploy.sh's ROOT default not found"
    assert re.search(r'^link="\$ROOT/live"$', deploy_sh, re.M), "deploy.sh's live link moved"
    live = f"{root[1]}/live"

    units = sorted((ROOT / "deploy").glob("*.service"))
    dirs = {
        unit.name: line.removeprefix("WorkingDirectory=")
        for unit in units
        for line in unit.read_text().splitlines()
        if line.startswith("WorkingDirectory=")
    }
    assert set(dirs) == {"processor.service", "processor-drift.service"}
    assert set(dirs.values()) == {live}, dirs
    assert [path for path, _ in _named("owner")] == [live]


def _patching_steps() -> str:
    """§ Patching's numbered run, from "This host's run" to the next paragraph."""
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
    section = deployment.split("\n## Patching\n", 1)[1].split("\n## ", 1)[0]
    run = section.split("**This host's run**", 1)[1]
    return run.split("\n\n**", 1)[0]


def test_the_runbook_takes_the_language_read() -> None:
    """The probe audits the lock only under ``--refresh-into`` (readings.md); the
    dry run and ``--post-boot`` never do. A run that skips it leaves the week's read
    in ``not_read``, so the runbook names it as a step (#75)."""
    steps = _patching_steps()
    assert re.search(r"^\d+\. .*`probe\.sh --refresh-into <dir>`", steps, re.M), steps


def test_the_runbook_sections_exist() -> None:
    """The knob, AGENTS.md and docs/SKILLS.md link DEPLOYMENT.md's sections by anchor."""
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
    for heading in ("## Patching", "### Patching Node"):
        assert re.search(rf"^{heading}$", deployment, re.M), heading


def test_the_gate_reads_the_extraction_and_the_unit() -> None:
    """The reboot chain's gate waits on ``inflight``, and each held step polls
    ``health`` until it passes twice: dropping either reads clean but blinds them."""
    assert [" ".join(words) for words in _named("inflight")] == [
        "pgrep -c -u processor -f 'processor\\._child' || true"
    ]
    assert ["systemctl", "is-active", "--quiet", "processor.service"] in _named("health")
