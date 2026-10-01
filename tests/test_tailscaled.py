"""tailscaled answers every lookup on co-processor, and is ranked to survive the OOM killer (#8).

Since #8 this VM runs Tailscale with ``--accept-dns=true``, the cohort's setting
(observo#631, notifier#43 D8). ``/etc/resolv.conf`` points at MagicDNS
(``100.100.100.100``), so every lookup on the host goes through tailscaled: ``broker``
(the bus), GCS, GitHub and the agent sessions' API. tailscaled already carries the
only path to the broker, so an OOM kill would take both. At the packaged
``OOMScoreAdjust=0`` it read ``oom_score`` 671 on 2026-10-01, ranked by size with
every other unprotected process.

The bus URL names the broker, never its address: broker's ``docs/RECOVERY.md``
rebuilds the node under the same name, and a new address.

Tracked in ``deploy/``, installed as:

- ``tailscaled.service.d/90-processor-oom.conf`` -> ``/etc/systemd/system/tailscaled.service.d/``

Pure assertions on the tracked copies run everywhere. Installed-parity and live
assertions skip where the node is not this one.
"""

import ipaddress
import json
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DROPIN = ROOT / "deploy" / "tailscaled.service.d" / "90-processor-oom.conf"
INSTALLED = Path("/etc/systemd/system/tailscaled.service.d") / DROPIN.name
SERVICE = ROOT / "deploy" / "processor.service"
DEPLOYMENT = ROOT / "docs" / "DEPLOYMENT.md"
RESOLV_BACKUP = "/etc/resolv.pre-tailscale-backup.conf"
NODE_TAG = "tag:processor"
TAILNET = ipaddress.ip_network("100.64.0.0/10")

# The kernel's floor. -1000 exempts a process outright: under true exhaustion
# something must still be killable.
OOM_EXEMPT = -1000

# A shell write onto /etc/resolv.conf: a copy, move, link, removal, tee or dd onto
# it, an in-place sed, or a redirect into it.
RESOLV_WRITE = re.compile(r"\b(cp|mv|ln|rm|tee|install|dd)\b|\bsed\b.*\s-i|>>?\s*/etc/resolv\.conf")

# The broker's tailnet address, which a rebuild changes. Escaped, so this file
# does not match itself.
BROKER_ADDRESS = re.compile(r"\b100\.97\.91\.19\b")


def _directives(path: Path) -> list[str]:
    """Non-blank, non-comment lines of a systemd unit fragment."""
    return [
        ln.strip()
        for ln in path.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith(("#", ";"))
    ]


def _oom_score_adjust(path: Path) -> int:
    """``OOMScoreAdjust=`` of a unit fragment, 0 when unset (systemd's default).

    ``int()`` is strict on purpose: systemd has no inline comments, so
    ``OOMScoreAdjust=-950  # why`` voids the directive and the unit runs at 0.
    """
    values = [ln.split("=", 1)[1] for ln in _directives(path) if ln.startswith("OOMScoreAdjust=")]
    assert len(values) <= 1, f"{path.name} sets OOMScoreAdjust more than once: {values}"
    return int(values[0]) if values else 0


def _require_this_node() -> None:
    """Skip unless this is the running ``tag:processor`` node."""
    exe = shutil.which("tailscale")
    if exe is None:
        pytest.skip("tailscale not installed on this host")
    status = subprocess.run([exe, "status", "--json"], capture_output=True, text=True)
    if status.returncode != 0:
        pytest.skip(f"tailscale status failed (rc {status.returncode}); not a running node")
    if NODE_TAG not in (json.loads(status.stdout).get("Self", {}).get("Tags") or []):
        pytest.skip(f"not the {NODE_TAG} node")


def test_dropin_is_one_service_directive() -> None:
    """One key, so the drop-in cannot quietly change tailscaled's other behaviour.

    No ``MemoryLow=``: without a matching ``system.slice`` grant the kernel bounds
    it to 0 (watcher#309), so it would read as protection it is not.
    """
    lines = _directives(DROPIN)
    assert lines[0] == "[Service]", f"directives must sit under [Service]: {lines}"
    assert len(lines) == 2 and lines[1].startswith("OOMScoreAdjust="), lines


def test_tailscaled_ranks_below_the_service_it_carries() -> None:
    adj = _oom_score_adjust(DROPIN)
    assert adj > OOM_EXEMPT, "-1000 exempts tailscaled outright; rank it, don't exempt it"
    service = _oom_score_adjust(SERVICE)
    assert adj < service, (
        f"tailscaled ({adj}) must be killed after processor.service ({service}): the "
        "service reaches the broker, and resolves every name, through it (#8)"
    )


def test_deployment_installs_the_dropin() -> None:
    doc = DEPLOYMENT.read_text()
    assert f"deploy/tailscaled.service.d/{DROPIN.name}" in doc
    assert str(INSTALLED) in doc


def _writes_resolv_conf(line: str) -> bool:
    return "/etc/resolv.conf" in line and RESOLV_WRITE.search(line) is not None


@pytest.mark.parametrize(
    "line",
    [
        "sudo cp infra/resolv.conf /etc/resolv.conf",
        "echo nameserver 1.1.1.1 | sudo tee /etc/resolv.conf",
        "sudo sh -c 'echo nameserver 1.1.1.1 > /etc/resolv.conf'",
        "echo options rotate >> /etc/resolv.conf",
        "sudo sed -i 's/^nameserver .*/nameserver 1.1.1.1/' /etc/resolv.conf",
        "sudo ln -sf /run/resolv.conf /etc/resolv.conf",
        "sudo rm /etc/resolv.conf",
    ],
)
def test_the_write_guard_sees_every_shell_write(line: str) -> None:
    assert _writes_resolv_conf(line)


def test_the_write_guard_passes_reads() -> None:
    for line in (
        "grep nameserver /etc/resolv.conf",
        "cat /etc/resolv.conf",
        "head -1 /etc/resolv.conf",
    ):
        assert not _writes_resolv_conf(line), line


def test_deployment_never_writes_a_file_over_resolv_conf() -> None:
    """tailscaled owns ``/etc/resolv.conf``; the one sanctioned write restores its backup."""
    writes = [ln.strip() for ln in DEPLOYMENT.read_text().splitlines() if _writes_resolv_conf(ln)]
    for ln in writes:
        assert RESOLV_BACKUP in ln, f"docs/DEPLOYMENT.md overwrites /etc/resolv.conf: {ln}"


def test_no_committable_file_names_the_broker_by_address() -> None:
    """Every committable file, not a list: a new doc, unit or script is covered too."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    files = [ROOT / f for f in listed if f]
    hits = [
        str(f.relative_to(ROOT))
        for f in files
        if f.is_file()
        and not f.is_symlink()
        and BROKER_ADDRESS.search(f.read_text(errors="ignore"))
    ]
    assert hits == [], f"these name the broker by address; use `broker` (#8): {hits}"


def test_every_bus_url_in_the_runbook_names_the_broker() -> None:
    urls = re.findall(r"redis://processor:[^@\s]*@([^:/\s]+):6379", DEPLOYMENT.read_text())
    assert urls and set(urls) == {"broker"}, urls


def test_installed_copy_matches_tracked() -> None:
    try:
        installed = INSTALLED.read_text()
    except FileNotFoundError:
        pytest.skip(f"{INSTALLED} not installed on this host")
    assert installed == DROPIN.read_text()


def test_live_tailscaled_runs_at_the_tracked_adj() -> None:
    """``OOMScoreAdjust=`` applies at exec: installed but not restarted reads 0."""
    if not INSTALLED.exists():
        pytest.skip(f"{INSTALLED} not installed on this host")
    pid = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", "tailscaled"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert pid not in ("", "0"), "tailscaled is not running"
    live = int(Path(f"/proc/{pid}/oom_score_adj").read_text())
    assert live == _oom_score_adjust(DROPIN), (
        f"tailscaled runs at {live}; restart it so the drop-in takes effect"
    )


def test_live_node_accepts_tailnet_dns() -> None:
    """The cohort's rule: ``CorpDNS`` must be true.

    It can be false while ``tailscale status`` looks healthy: the node is up, the
    peers are listed, and every tailnet name still fails (archiver#193).
    """
    _require_this_node()
    prefs = subprocess.run(
        ["tailscale", "debug", "prefs"], capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(prefs)["CorpDNS"] is True, (
        "co-processor runs with --accept-dns=false; restore it with "
        "`sudo tailscale set --accept-dns=true` (#8)"
    )


def test_live_broker_resolves_to_its_tailnet_address() -> None:
    """``CorpDNS`` can be true while ``/etc/resolv.conf`` has been written over."""
    _require_this_node()
    addresses = {info[4][0] for info in socket.getaddrinfo("broker", 6379, socket.AF_INET)}
    assert addresses and all(ipaddress.ip_address(a) in TAILNET for a in addresses), addresses
