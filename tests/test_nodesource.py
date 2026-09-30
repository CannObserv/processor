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


def _stanzas(path: Path) -> list[dict[str, str]]:
    """deb822 stanzas, blank-line separated; ``#`` comment lines dropped."""
    blocks = path.read_text().split("\n\n")
    lines = ([ln for ln in b.splitlines() if ln and not ln.startswith("#")] for b in blocks)
    return [dict(ln.split(": ", 1) for ln in block) for block in lines if block]


def _deb822(path: Path) -> dict[str, str]:
    (only,) = _stanzas(path)
    return only


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
    nodejs, rest = _stanzas(APT / "nodesource.pref")
    assert nodejs["Package"] == "nodejs"
    # By host: the Release file's Origin is aptly's generic ". nodistro".
    assert nodejs["Pin"] == "origin deb.nodesource.com"
    # Above universe's 500 (its nodejs 18), below 1000 (never a downgrade).
    assert 500 < int(nodejs["Pin-Priority"]) < 1000
    # Everything else the host serves stays below Ubuntu's 500: the maintenance lane's
    # site=deb.nodesource.com selects the whole site, so the pin is what scopes it to Node.
    assert rest["Package"] == "*"
    assert rest["Pin"] == nodejs["Pin"]
    assert 0 < int(rest["Pin-Priority"]) < 500


def test_script_installs_what_the_repo_versions() -> None:
    assert _script_value("SOURCES").endswith("/nodesource.sources")
    assert _script_value("PREF").endswith("/nodesource.pref")
    assert _script_value("FINGERPRINT") == FINGERPRINT


def test_deployment_doc_names_the_same_fingerprint() -> None:
    doc = re.sub(r"\s", "", (ROOT / "docs" / "DEPLOYMENT.md").read_text())
    assert FINGERPRINT in doc


def test_docs_follow_the_major_line_and_the_installed_paths() -> None:
    """A bump or a rename that leaves the docs behind fails here, not in an operator's shell."""
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
    assert f"Node {EXPECTED_MAJOR} LTS" in deployment
    assert f"node_{EXPECTED_MAJOR}.x" in deployment
    for name in ("KEYRING", "SOURCES", "PREF"):
        # The table and the removal block both name each path.
        assert deployment.count(_script_value(name)) >= 2, name
    assert f"{EXPECTED_MAJOR} LTS from NodeSource" in (ROOT / "AGENTS.md").read_text()


def test_script_parses() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
