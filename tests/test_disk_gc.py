"""The cohort disk GC and headroom sensor's config for this host (#82).

Provisioner's GC (provisioner#61) is installed by gregoryfoster/skills
``maintaining-host-disk`` under ``/usr/local/lib/cannobserv/disk-gc/``. Its tiers
and rules are Provisioner's; this repo tracks only the host's choices:

- ``disk-gc/disk-gc.conf`` -> ``/etc/cannobserv/disk-gc/disk-gc.conf``

and the repo-side knob the GC doesn't own: pytest's basetemp retention.

Pure assertions on the tracked copy run everywhere, read the way the GC reads it.
On ``co-processor``, decided by the hostname, never by the file (broker#79): the
installed copy matches, and the tracked one passes the installed GC's own
``load_config``. Both fail on the node when what they read is missing.
"""

import configparser
import importlib.machinery
import importlib.util
import json
import socket
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "deploy" / "disk-gc" / "disk-gc.conf"
INSTALLED = Path("/etc/cannobserv/disk-gc/disk-gc.conf")
GC = Path("/usr/local/lib/cannobserv/disk-gc/disk-gc")
NODE = "co-processor"
DEPLOY_CHECKOUT = Path("/home/exedev/processor")


def _on_node() -> bool:
    return socket.gethostname() == NODE


def _conf() -> configparser.ConfigParser:
    """The tracked config, parsed with the GC's own parser settings (VERSION 2)."""
    parser = configparser.ConfigParser(interpolation=None, default_section="\0none")
    parser.read_string(CONF.read_text())
    return parser


def _outcome_of(test) -> BaseException | None:
    """The skip, fail or assertion ``test()`` ends in, or ``None`` if it returns (broker#81)."""
    try:
        test()
    except (pytest.skip.Exception, pytest.fail.Exception, AssertionError) as outcome:
        return outcome
    return None


# --- the tracked config ---


def test_conf_has_only_the_gcs_sections() -> None:
    assert _conf().sections() == ["gc", "sensor"]


def test_conf_prunes_exedevs_home_only() -> None:
    # `processor` has no home; the GC walks only the homes named here.
    assert _conf()["gc"]["homes"].split() == ["/home/exedev"]


def test_conf_tiers_are_the_templates_plus_worktrees_without_docker() -> None:
    tiers = set(_conf()["gc"]["tiers"].split())
    assert tiers == {
        "vscode-servers",
        "vscode-clis",
        "claude-extensions",
        "vsix-cache",
        "plugin-cache",
        "npx",
        "npm-cache",
        "pytest-garbage",
        "worktrees",
        "uv",
    }


def test_conf_worktrees_tier_names_this_repo() -> None:
    # Either half alone does nothing: the tier and the repo (turning-on.md).
    assert _conf()["gc"]["worktree_repos"].split() == [str(DEPLOY_CHECKOUT)]


def test_conf_keeps_the_pinned_socraticode_tree() -> None:
    (spec_file,) = _conf()["gc"]["npx_spec_files"].split()
    rel = Path(spec_file).relative_to(DEPLOY_CHECKOUT)
    assert rel == Path(".claude/settings.json")
    assert json.loads((ROOT / rel).read_text())["env"]["SOCRATICODE_SPEC"].startswith(
        "socraticode@"
    )


def test_conf_never_cleans_the_uv_cache() -> None:
    assert _conf()["gc"]["uv_clean_above_mb"].strip() == "0"


def test_conf_lines_are_85_and_80_on_root() -> None:
    # patching-hosts reads alert_at_percent as a floor (gregoryfoster/skills#390).
    sensor = _conf()["sensor"]
    assert sensor["mount"].strip() == "/"
    assert int(sensor["alert_at_percent"]) == 85
    assert int(sensor["clear_at_percent"]) == 80


def test_conf_checks_in_to_status() -> None:
    sensor = _conf()["sensor"]
    assert sensor["status_url"].strip() == "http://status:9000"
    assert sensor["monitor_id"].strip().isalnum() or sensor["monitor_id"].strip() == ""


# --- the repo-side knob ---


def test_pytest_keeps_only_a_failed_runs_basetemp() -> None:
    opts = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert opts["tmp_path_retention_count"] == 1
    assert opts["tmp_path_retention_policy"] == "failed"


# --- on the node ---


def test_installed_copy_matches_tracked() -> None:
    if not _on_node():
        pytest.skip(f"not {NODE}")
    try:
        installed = INSTALLED.read_text()
    except FileNotFoundError:
        pytest.fail(f"{INSTALLED} is not installed on {NODE} (docs/DEPLOYMENT.md § Disk GC)")
    assert installed == CONF.read_text()


def test_tracked_conf_passes_the_installed_gc() -> None:
    """The GC refuses a config it can't use, and patching-hosts stops on one too."""
    if not _on_node():
        pytest.skip(f"not {NODE}")
    if not GC.is_file():
        pytest.fail(f"{GC} is not installed on {NODE} (docs/DEPLOYMENT.md § Disk GC)")
    loader = importlib.machinery.SourceFileLoader("cannobserv_disk_gc", str(GC))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    gc = importlib.util.module_from_spec(spec)
    loader.exec_module(gc)
    cfg = gc.load_config(str(CONF))
    assert cfg.homes == ("/home/exedev",)
    assert (cfg.alert_at_percent, cfg.clear_at) == (85, 80)


# --- both sides of the node check, on any host (broker#79) ---


@pytest.fixture
def bare_node(tmp_path, monkeypatch) -> None:
    """Neither the config nor the GC installed."""
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "INSTALLED", tmp_path / "disk-gc.conf")
    monkeypatch.setattr(module, "GC", tmp_path / "disk-gc")


def _pretend(monkeypatch, *, on_node: bool) -> None:
    monkeypatch.setattr(sys.modules[__name__], "_on_node", lambda: on_node)


@pytest.mark.parametrize(
    "test", [test_installed_copy_matches_tracked, test_tracked_conf_passes_the_installed_gc]
)
def test_node_checks_skip_off_the_node(bare_node, monkeypatch, test) -> None:
    _pretend(monkeypatch, on_node=False)
    assert isinstance(_outcome_of(test), pytest.skip.Exception)


@pytest.mark.parametrize(
    "test", [test_installed_copy_matches_tracked, test_tracked_conf_passes_the_installed_gc]
)
def test_node_checks_fail_on_the_node_when_absent(bare_node, monkeypatch, test) -> None:
    _pretend(monkeypatch, on_node=True)
    outcome = _outcome_of(test)
    assert isinstance(outcome, pytest.fail.Exception)
    assert "is not installed" in str(outcome)
