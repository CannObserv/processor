"""needrestart lists restarts and never performs them (#77, as CannObserv/broker#65).

apt's ``DPkg::Post-Invoke`` hook (``/etc/apt/apt.conf.d/99needrestart``) runs
``needrestart -m u`` after every dpkg run. Ubuntu's patch turns ``-m u`` into
**automatic** restarts when ``$nrconf{restart}`` is unset, the stock state, so a
``libssl3t64`` update would restart ``processor`` (its ``python3.12`` maps libssl)
outside any window. ``tailscaled`` is static Go: only its own postinst restarts it.
``NEEDRESTART_MODE=l`` (``deploy/nodesource.sh``) covers only the runs that remember
it; this drop-in makes the answer not depend on it.

Tracked in ``deploy/``, installed as:

- ``needrestart.conf.d/processor.conf`` -> ``/etc/needrestart/conf.d/``

Pure assertions on the tracked copy run everywhere. Installed-parity and live
assertions skip off ``co-processor``, decided by the hostname, never by the file:
keyed on the drop-in, deleting it from the node would skip the one test that
guards it (broker#79). On the node, a missing drop-in fails both.
"""

import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DROPIN = ROOT / "deploy" / "needrestart.conf.d" / "processor.conf"
INSTALLED = Path("/etc/needrestart/conf.d/processor.conf")
MAIN_CONF = Path("/etc/needrestart/needrestart.conf")
NODE = "co-processor"

# needrestart's config is Perl, eval'd into `%nrconf`. Evaluating it the same way is
# the only honest parse: a syntax error makes needrestart die, and the apt hook
# swallows that with `|| true`.
_EVAL = (
    "our %nrconf; our $LOGPREF = q(); "
    "eval do { local(@ARGV, $/) = $ARGV[0]; <> }; die $@ if $@; "
    "print defined $nrconf{restart} ? $nrconf{restart} : q(undef);"
)


def _on_node() -> bool:
    return socket.gethostname() == NODE


def _restart_mode(conf: Path) -> str:
    perl = shutil.which("perl")
    if perl is None:
        pytest.skip("perl not available on this host")
    return subprocess.run(
        [perl, "-e", _EVAL, str(conf)], capture_output=True, text=True, check=True
    ).stdout


def _outcome_of(test) -> BaseException | None:
    """The skip, fail or assertion ``test()`` ends in, or ``None`` if it returns.

    ``pytest.raises(pytest.fail.Exception)`` would let a skip escape and skip the
    pin, so "fails on the node" regressing to "skips on the node" would read as one
    more skip (broker#81).
    """
    try:
        test()
    except (pytest.skip.Exception, pytest.fail.Exception, AssertionError) as outcome:
        return outcome
    return None


def test_dropin_sets_list_only_restart_mode() -> None:
    assert _restart_mode(DROPIN) == "l"


def test_dropin_sets_nothing_else() -> None:
    """One key, so the drop-in cannot quietly change needrestart's other behaviour,
    and ``$nrconf{ui}`` stays unset: setting it would force interactive mode."""
    lines = [
        ln.strip()
        for ln in DROPIN.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert lines == ["$nrconf{restart} = 'l';"]


def test_installed_copy_matches_tracked() -> None:
    if not _on_node():
        pytest.skip(f"not {NODE}")
    try:
        installed = INSTALLED.read_text()
    except FileNotFoundError:
        pytest.fail(f"{INSTALLED} is not installed on {NODE} (docs/DEPLOYMENT.md § Patching)")
    assert installed == DROPIN.read_text()


def test_live_config_chain_resolves_to_list_only() -> None:
    """The main config globs ``conf.d/*.conf`` in sort order, so a later file could
    override this one. Evaluate the chain needrestart itself reads."""
    if not _on_node():
        pytest.skip(f"not {NODE}")
    assert MAIN_CONF.exists(), f"{MAIN_CONF} absent: needrestart not installed on {NODE}"
    assert _restart_mode(MAIN_CONF) == "l"


# --- both sides of the node check, on any host (broker#79) ---


@pytest.fixture
def stock_needrestart(tmp_path, monkeypatch) -> Path:
    """A needrestart main config with no drop-in behind it: the stock ``undef``."""
    main = tmp_path / "needrestart.conf"
    main.write_text("$nrconf{verbosity} = 1;\n")
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "MAIN_CONF", main)
    monkeypatch.setattr(module, "INSTALLED", tmp_path / "conf.d" / "processor.conf")
    return main


def _pretend(monkeypatch, *, on_node: bool) -> None:
    monkeypatch.setattr(sys.modules[__name__], "_on_node", lambda: on_node)


def test_live_chain_skips_off_the_node(stock_needrestart, monkeypatch) -> None:
    _pretend(monkeypatch, on_node=False)
    outcome = _outcome_of(test_live_config_chain_resolves_to_list_only)
    assert isinstance(outcome, pytest.skip.Exception)


def test_live_chain_fails_on_the_node_without_the_dropin(stock_needrestart, monkeypatch) -> None:
    if shutil.which("perl") is None:
        pytest.skip("perl not available on this host")
    _pretend(monkeypatch, on_node=True)
    outcome = _outcome_of(test_live_config_chain_resolves_to_list_only)
    assert isinstance(outcome, AssertionError)
    assert "undef" in str(outcome)


def test_installed_copy_fails_on_the_node_when_absent(stock_needrestart, monkeypatch) -> None:
    _pretend(monkeypatch, on_node=True)
    outcome = _outcome_of(test_installed_copy_matches_tracked)
    assert isinstance(outcome, pytest.fail.Exception)


def test_installed_copy_skips_off_the_node(stock_needrestart, monkeypatch) -> None:
    _pretend(monkeypatch, on_node=False)
    outcome = _outcome_of(test_installed_copy_matches_tracked)
    assert isinstance(outcome, pytest.skip.Exception)
