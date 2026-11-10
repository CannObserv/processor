"""``posture automatic``: unattended-upgrades takes ``-security`` daily, clear of the window (#113).

The vendored ``patching-hosts`` policy.md § automatic asks for the apt timers enabled,
``APT::Periodic::Enable "1"`` effective, unattended-upgrades on ``-security`` only, no
automatic reboot and needrestart ``l`` (``tests/test_needrestart.py``). The image
masks the timers and sets ``Enable "0"`` in ``docker-disable-periodic-update``; apt
reads ``apt.conf.d`` in name order, so the file that sets 1 must sort after it.

The timers are moved, not only unmasked: ``apply.sh``'s ``gate_apt`` refuses a step
whose span meets ``apt-daily-upgrade.timer``'s range (its calendar times plus
``RandomizedDelaySec``), and the stock ``06:00`` + 60 m sits inside ``Tue 05:00-10:00``.
``apply.sh`` doesn't read ``apt-daily.timer``; its ``apt update`` can still meet the
bulk's own list refresh, so it moves too.

Tracked in ``deploy/``, installed by hand (docs/PATCHING.md § Patching) as:

- ``apt/apt.conf.d/processor-periodic`` -> ``/etc/apt/apt.conf.d/``
- ``apt-daily.timer.d/processor-window.conf`` -> ``/etc/systemd/system/apt-daily.timer.d/``
- ``apt-daily-upgrade.timer.d/processor-window.conf`` ->
  ``/etc/systemd/system/apt-daily-upgrade.timer.d/``

Pure assertions on the tracked copies run everywhere. Installed-parity and live
assertions skip off ``co-processor``, decided by the hostname, never by the file
(broker#79): on the node, a missing file fails.
"""

import json
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
PERIODIC = DEPLOY / "apt" / "apt.conf.d" / "processor-periodic"
TIMERS = {
    "apt-daily.timer": DEPLOY / "apt-daily.timer.d" / "processor-window.conf",
    "apt-daily-upgrade.timer": DEPLOY / "apt-daily-upgrade.timer.d" / "processor-window.conf",
}
INSTALLED = {
    PERIODIC: Path("/etc/apt/apt.conf.d/processor-periodic"),
    **{
        dropin: Path(f"/etc/systemd/system/{timer}.d/{dropin.name}")
        for timer, dropin in TIMERS.items()
    },
}
KNOB = ROOT / ".skills" / "patching-hosts"
READ_KNOB = ROOT / "skills" / "patching-hosts" / "scripts" / "read-knob.sh"
NODE = "co-processor"
#: The image's file that sets ``APT::Periodic::Enable "0"`` (measured 2026-11-05).
IMAGE_DISABLE = "docker-disable-periodic-update"
DAY = 24 * 60


def _on_node() -> bool:
    return socket.gethostname() == NODE


def _settings(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("//")
    ]


def _timer(path: Path) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.strip().split("=", 1)
            keys.setdefault(key, []).append(value)
    return keys


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")[:2]
    return int(hours) * 60 + int(minutes)


def _range(dropin: Path) -> tuple[int, int]:
    """The daily range, in minutes from 00:00 UTC, the timer can start its service in."""
    keys = _timer(dropin)
    calendar = keys["OnCalendar"][-1]
    match = re.fullmatch(r"\*-\*-\* (\d{1,2}:\d{2})", calendar)
    assert match, f"{dropin}: one daily time, as `*-*-* HH:MM`: {calendar!r}"
    start = _minutes(match[1])
    delay = re.fullmatch(r"(\d+)m", keys["RandomizedDelaySec"][-1])
    assert delay, f"{dropin}: RandomizedDelaySec in minutes"
    return start, start + int(delay[1])


def _windows() -> list[tuple[int, int]]:
    """Every weekday window the knob resolves to, as daily minute ranges (end may pass DAY)."""
    assert READ_KNOB.is_file(), f"{READ_KNOB} missing: run `bash .skills/doctor.sh`"
    out = subprocess.run(
        ["bash", str(READ_KNOB), "--config", str(KNOB), "--host", NODE],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    ranges = []
    for window in json.loads(out)["window"]:
        start, end = _minutes(window["start"]), _minutes(window["end"])
        ranges.append((start, end + DAY if window["wraps"] else end))
    assert ranges, "the knob declares no window"
    return ranges


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Whether two daily ranges meet, on any day: each shifted a day either way."""
    return any(a[0] < b[1] + s and b[0] + s < a[1] for s in (-DAY, 0, DAY))


def _apt_config(*args: str) -> dict[str, str]:
    apt_config = shutil.which("apt-config")
    if apt_config is None:
        pytest.skip("apt-config not available on this host")
    out = subprocess.run(
        [
            apt_config,
            *args,
            "shell",
            "E",
            "APT::Periodic::Enable",
            "R",
            "Unattended-Upgrade::Automatic-Reboot",
            "L",
            "APT::Periodic::Update-Package-Lists",
            "U",
            "APT::Periodic::Unattended-Upgrade",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(re.findall(r"^(\w)='([^']*)'$", out, re.M))


# --- the tracked copies ---


def test_the_periodic_file_sets_the_automatic_keys_and_nothing_else() -> None:
    assert _settings(PERIODIC) == [
        'APT::Periodic::Enable "1";',
        'APT::Periodic::Update-Package-Lists "1";',
        'APT::Periodic::Unattended-Upgrade "1";',
        'Unattended-Upgrade::Automatic-Reboot "false";',
    ]


def test_the_periodic_file_parses_and_wins_over_the_hosts_own() -> None:
    """apt-config reads the host's files, then ``-c``'s: a syntax error exits non-zero."""
    resolved = _apt_config("-c", str(PERIODIC))
    assert resolved == {"E": "1", "R": "false", "L": "1", "U": "1"}


def test_the_periodic_file_sorts_after_the_images_disable() -> None:
    """apt reads ``apt.conf.d`` in name order, and skips a name outside
    ``[A-Za-z0-9_.-]`` or with an extension other than ``.conf``: a ``99…`` name sorts
    before ``d`` and loses to the image's 0."""
    name = PERIODIC.name
    assert re.fullmatch(r"[A-Za-z0-9_-]+(\.conf)?", name)
    assert name > IMAGE_DISABLE


@pytest.mark.parametrize("timer", TIMERS, ids=str)
def test_each_timer_dropin_replaces_the_calendar(timer: str) -> None:
    """A drop-in's ``OnCalendar=`` adds to the stock list; an empty one resets it.
    Outside a ``[Timer]`` section systemd ignores each assignment, and the stock
    06:00 comes back inside the window."""
    sections = [
        line.strip()
        for line in TIMERS[timer].read_text().splitlines()
        if line.strip().startswith("[")
    ]
    assert sections == ["[Timer]"], f"{timer}: one [Timer] section"
    first = next(
        line.strip()
        for line in TIMERS[timer].read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert first == "[Timer]", f"{timer}: an assignment before [Timer] is ignored"
    keys = _timer(TIMERS[timer])
    assert keys["OnCalendar"][0] == "", f"{timer}: reset OnCalendar= first"
    assert len(keys["OnCalendar"]) == 2, f"{timer}: one calendar time after the reset"
    assert set(keys) == {"OnCalendar", "RandomizedDelaySec"}


@pytest.mark.parametrize("timer", TIMERS, ids=str)
def test_no_timer_can_start_inside_a_window(timer: str) -> None:
    """``gate_apt`` refuses a step whose span meets the upgrade timer's range; the
    lists timer's update would meet the bulk's own refresh. Daily, so any weekday."""
    start, end = _range(TIMERS[timer])
    for window in _windows():
        assert not _overlaps((start, end), window), f"{timer} {start}-{end} meets {window}"


def test_the_lists_refresh_before_the_upgrade() -> None:
    """``apt-daily-upgrade.service`` is ``After=apt-daily.service``: the stock order,
    kept, so the day's upgrade reads the day's lists."""
    lists = _range(TIMERS["apt-daily.timer"])
    upgrade = _range(TIMERS["apt-daily-upgrade.timer"])
    assert lists[1] <= upgrade[0]


@pytest.mark.parametrize("timer", TIMERS, ids=str)
def test_systemd_reads_each_calendar(timer: str) -> None:
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not available on this host")
    calendar = _timer(TIMERS[timer])["OnCalendar"][-1]
    subprocess.run([analyze, "calendar", calendar], capture_output=True, check=True)


# --- on the node ---


@pytest.mark.parametrize("tracked", INSTALLED, ids=lambda p: str(p.relative_to(DEPLOY)))
def test_installed_copy_matches_tracked(tracked: Path) -> None:
    if not _on_node():
        pytest.skip(f"not {NODE}")
    installed = INSTALLED[tracked]
    try:
        text = installed.read_text()
    except FileNotFoundError:
        pytest.fail(f"{installed} is not installed on {NODE} (docs/PATCHING.md § Patching)")
    assert text == tracked.read_text()


def test_live_apt_config_is_automatic() -> None:
    if not _on_node():
        pytest.skip(f"not {NODE}")
    assert _apt_config() == {"E": "1", "R": "false", "L": "1", "U": "1"}


@pytest.mark.parametrize("timer", TIMERS, ids=str)
def test_live_timer_is_enabled_on_the_tracked_calendar(timer: str) -> None:
    if not _on_node():
        pytest.skip(f"not {NODE}")
    enabled = subprocess.run(
        ["systemctl", "is-enabled", timer], capture_output=True, text=True
    ).stdout.strip()
    assert enabled == "enabled", f"{timer} is {enabled}"
    shown = subprocess.run(
        ["systemctl", "show", timer, "-p", "TimersCalendar", "-p", "RandomizedDelayUSec"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    calendar = _timer(TIMERS[timer])["OnCalendar"][-1]
    assert shown.count("OnCalendar=") == 1, shown
    assert f"OnCalendar={calendar}:00 ;" in shown, shown
    delay = int(_timer(TIMERS[timer])["RandomizedDelaySec"][-1].removesuffix("m"))
    assert f"RandomizedDelayUSec={delay}min\n" in shown or (
        delay % 60 == 0 and f"RandomizedDelayUSec={delay // 60}h\n" in shown
    ), shown


# --- both sides of the node check, on any host (broker#79) ---


def _outcome_of(test, *args) -> BaseException | None:
    try:
        test(*args)
    except (pytest.skip.Exception, pytest.fail.Exception, AssertionError) as outcome:
        return outcome
    return None


def _pretend(monkeypatch, *, on_node: bool) -> None:
    monkeypatch.setattr(sys.modules[__name__], "_on_node", lambda: on_node)


def test_installed_copy_fails_on_the_node_when_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(INSTALLED, PERIODIC, tmp_path / "absent")
    _pretend(monkeypatch, on_node=True)
    outcome = _outcome_of(test_installed_copy_matches_tracked, PERIODIC)
    assert isinstance(outcome, pytest.fail.Exception)


def test_installed_copy_skips_off_the_node(monkeypatch) -> None:
    _pretend(monkeypatch, on_node=False)
    outcome = _outcome_of(test_installed_copy_matches_tracked, PERIODIC)
    assert isinstance(outcome, pytest.skip.Exception)


def test_overlap_reads_a_window_across_midnight() -> None:
    """The helper the window test leans on: a range wrapping past 24:00 meets the
    next day's early hours, and two ranges that only touch don't meet."""
    assert _overlaps((60, 120), (23 * 60, DAY + 90))
    assert not _overlaps((600, 660), (300, 600))
    assert _overlaps((360, 420), (300, 600))
