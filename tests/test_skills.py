"""The vendored agent-skill layout holds (docs/SKILLS.md).

Two discovery paths read one set: ``.claude/skills/<n>`` -> ``../../skills/<n>`` ->
``../skills-vendor/<owner>-<repo>/skills/<n>``, so a committed override in ``skills/``
shadows its vendor copy in both. The auto-refresh hook is two artifacts, a vendor
symlink and its ``SessionStart`` registration; the symlink alone refreshes nothing.

Every vendor link dangles until the submodules are initialised (fresh clone,
``git worktree add``): run ``bash .skills/doctor.sh``.
"""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
CLAUDE_SKILLS = ROOT / ".claude" / "skills"
VENDOR = ROOT / "skills-vendor"
HOOK = ROOT / ".claude" / "hooks" / "skills-submodule-update.sh"
INSTALL_REFRESH = SKILLS / "managing-skills" / "scripts" / "install-refresh.sh"
DOCTOR_HINT = "dangling: run `bash .skills/doctor.sh`"
# using-mayfly-chat's leak pattern (references/security.md): the 22-char ID and the
# 43-char key, so it matches a live URL and not the keyless view URL. Character
# classes cannot match their own text.
CHANNEL_URL = re.compile(r"/c/[A-Za-z0-9_-]{22}#[A-Za-z0-9_-]{43}")


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def _frontmatter(skill_md: Path) -> dict[str, str]:
    block = skill_md.read_text().split("---", 2)[1]
    pairs = (re.match(r"\s*([\w-]+):\s*(.*)", line) for line in block.splitlines())
    return {m[1]: m[2].strip().strip('"') for m in pairs if m}


def test_both_discovery_paths_list_the_same_skills() -> None:
    assert _names(SKILLS) == _names(CLAUDE_SKILLS)


def test_claude_skills_route_through_skills() -> None:
    wrong = [
        n
        for n in _names(CLAUDE_SKILLS)
        if not (CLAUDE_SKILLS / n).is_symlink()
        or (CLAUDE_SKILLS / n).readlink() != Path("../../skills") / n
    ]
    assert not wrong, wrong


def test_skills_are_vendor_links_or_declared_overrides() -> None:
    for name in _names(SKILLS):
        entry = SKILLS / name
        if entry.is_symlink():
            assert str(entry.readlink()).startswith("../skills-vendor/"), (name, entry.readlink())
            assert entry.readlink().name == name, (name, entry.readlink())
            assert (entry / "SKILL.md").is_file(), (name, DOCTOR_HINT)
            continue
        meta = _frontmatter(entry / "SKILL.md")
        assert meta.get("name") == name, (name, meta.get("name"))
        target = meta.get("overrides", "")
        assert (VENDOR / target.replace("/", "/skills/", 1) / "SKILL.md").is_file(), (name, target)
        assert re.search(r"\([0-9a-f]{7,40}\)", meta.get("synced-from", "")), (name, "synced-from")


def test_override_links_resolve() -> None:
    overrides = [e for e in SKILLS.iterdir() if not e.is_symlink()]
    for top, dirs, files in (w for e in overrides for w in os.walk(e)):
        for link in (Path(top) / n for n in dirs + files):
            if link.is_symlink():
                assert link.exists(), (str(link.relative_to(ROOT)), DOCTOR_HINT)


def test_refresh_hook_is_a_vendor_symlink_and_registered() -> None:
    assert HOOK.is_symlink(), "a copy freezes at install time; re-run install-refresh.sh"
    assert "skills-vendor/" in str(HOOK.readlink()), HOOK.readlink()
    assert HOOK.exists(), DOCTOR_HINT
    check = subprocess.run(
        ["bash", str(INSTALL_REFRESH), "--check"], cwd=ROOT, capture_output=True, text=True
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_doctor_is_a_committed_copy() -> None:
    doctor = ROOT / ".skills" / "doctor.sh"
    assert doctor.is_file() and not doctor.is_symlink()


def test_no_mayfly_channel_url_is_committable() -> None:
    """A channel URL is read, write and delete access; it never reaches a durable store."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    files = [ROOT / f for f in listed if f]
    leaks = [
        str(f.relative_to(ROOT))
        for f in files
        if f.is_file() and not f.is_symlink() and CHANNEL_URL.search(f.read_text(errors="ignore"))
    ]
    assert not leaks, leaks
