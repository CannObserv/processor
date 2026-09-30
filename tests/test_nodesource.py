"""The NodeSource apt setup is pinned and self-consistent (docs/DEPLOYMENT.md, #6).

Node.js is agent tooling only (using-mayfly-chat, SocratiCode); the service never runs
it. Moving the major line is a deliberate act: edit ``EXPECTED_MAJOR`` here in the same
commit as ``deploy/apt/nodesource.sources``. Nothing here touches the host; the host is
checked by ``bash deploy/nodesource.sh check``.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APT = ROOT / "deploy" / "apt"
SCRIPT = ROOT / "deploy" / "nodesource.sh"
EXPECTED_MAJOR = 24
# NodeSource's repo signing key, as watcher#296 verified it.
FINGERPRINT = "6F71F525282841EEDAF851B42F59B5F99B1BE0B4"


def _deb822(path: Path) -> dict[str, str]:
    lines = [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("#")]
    return dict(ln.split(": ", 1) for ln in lines)


def _script_value(name: str) -> str:
    match = re.search(rf'^{name}="([^"]*)"', SCRIPT.read_text(), re.MULTILINE)
    assert match, f"{name}= not found in {SCRIPT.name}"
    return match[1]


def test_source_is_the_pinned_major_line_signed_by_its_own_keyring() -> None:
    source = _deb822(APT / "nodesource.sources")
    assert source["Types"] == "deb"
    assert source["URIs"] == f"https://deb.nodesource.com/node_{EXPECTED_MAJOR}.x"
    assert source["Suites"] == "nodistro"
    assert source["Components"] == "main"
    assert source["Signed-By"] == _script_value("KEYRING")


def test_preference_pins_nodejs_to_nodesource_by_host() -> None:
    pref = _deb822(APT / "nodesource.pref")
    assert pref["Package"] == "nodejs"
    # By host: the Release file's Origin is aptly's generic ". nodistro".
    assert pref["Pin"] == "origin deb.nodesource.com"
    # Above universe's 500 (its nodejs 18), below 1000 (never a downgrade).
    assert 500 < int(pref["Pin-Priority"]) < 1000


def test_script_installs_what_the_repo_versions() -> None:
    assert _script_value("SOURCES").endswith("/nodesource.sources")
    assert _script_value("PREF").endswith("/nodesource.pref")
    assert _script_value("FINGERPRINT") == FINGERPRINT


def test_deployment_doc_names_the_same_fingerprint() -> None:
    doc = re.sub(r"\s", "", (ROOT / "docs" / "DEPLOYMENT.md").read_text())
    assert FINGERPRINT in doc


def test_script_parses() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
