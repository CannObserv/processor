"""The ``.skills/`` knobs the vendored ``shipping-work`` reads hold what #115 chose.

``merge_mode`` and ``deploy_command`` are read by the skill's own readers, never
re-parsed here: a knob that is present but unusable is exit 2 there, never the
default. Both read the *primary* checkout, so each runs in a scratch repo holding
only the knobs (a linked worktree would read the main checkout's). The doc-check
pair gets the dead-entry check upstream lacks for single entries: one entry that
matches nothing is a hole the gate never reports, while the gate does fail a list
that matches nothing at all.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KNOBS = ROOT / ".skills"
SHIP = ROOT / "skills" / "shipping-work" / "scripts"
DOCTOR_HINT = "dangling: run `bash .skills/doctor.sh`"


def _lines(name: str) -> list[str]:
    """The knob's lines, minus blanks and whole-line ``#`` comments (KNOBS.md)."""
    raw = (KNOBS / name).read_text().splitlines()
    return [line.strip() for line in raw if line.strip() and not line.lstrip().startswith("#")]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A repo on ``main``, pushed to a bare origin, holding the knobs and deploy.sh."""
    assert (SHIP / "merge-mode.sh").is_file(), DOCTOR_HINT
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for rel in (".skills/merge_mode", ".skills/deploy_command", "scripts/deploy.sh"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, repo / rel)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "knobs")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-qu", "origin", "main")
    return repo


def _ship(script: str, repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SHIP / script), *args], cwd=repo, capture_output=True, text=True
    )


def test_merge_mode_is_pr(scratch: Path) -> None:
    """Work lands through a PR (#9's convention); the FF merge is docs/SKILLS.md's."""
    out = _ship("merge-mode.sh", scratch)
    assert (out.returncode, out.stdout.strip()) == (0, "pr"), out.stderr


def test_deploy_command_is_the_release_deploy(scratch: Path) -> None:
    """``setsid -w``: its own session, and the deploy's exit code (broker#123)."""
    out = _ship("run-deploy-command.sh", scratch, "--check")
    sha = _git(scratch, "rev-parse", "HEAD")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"setsid -w scripts/deploy.sh {sha}"


def test_deploy_command_names_an_executable_repo_script() -> None:
    """The skill checks only the first word (``setsid``); the script is checked here."""
    (line,) = _lines("deploy_command")
    script = ROOT / line.split()[-1]
    assert script.is_file() and script.stat().st_mode & 0o111, script


def _tracked() -> list[str]:
    return _git(ROOT, "ls-files").splitlines()


def test_each_doc_sensitive_path_matches_a_tracked_path() -> None:
    """Entries match whole path segments at any depth (doc-check.sh)."""
    tracked = [f"/{p}/" for p in _tracked()]
    dead = [
        e
        for e in _lines("doc-sensitive-paths")
        if not any(f"/{e.strip('/')}/" in p for p in tracked)
    ]
    assert not dead, dead


def test_each_doc_section_names_tracked_docs_and_real_headings() -> None:
    """Each line leads with ``<doc> ["Heading"], …: ``; every doc and heading exists."""
    tracked = set(_tracked())
    for line in _lines("doc-sections"):
        # The first ": " outside quotes: a heading may hold one ("Deploy a change: …").
        lead = re.match(r'((?:"[^"]*"|[^"])*?): ', line)
        assert lead, line
        lead = lead.group(1)
        for doc, heading in re.findall(r'([\w./-]+\.md)(?: "([^"]+)")?', lead):
            assert doc in tracked, (doc, line)
            if heading:
                text = (ROOT / doc).read_text()
                assert re.search(rf"^#+ {re.escape(heading)}$", text, re.M), (doc, heading)


def test_gate_stamp_inputs_cover_the_untracked_inputs() -> None:
    """The knob replaces the default ``.env``, so it names it again (test_child reads
    ``REPO/.env``); ``.wheelhouse`` decides which co-core ``uv run`` installs."""
    assert _lines("gate-stamp-inputs") == [".env", ".wheelhouse/*"]
