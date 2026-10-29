"""``.github/dependabot.yml`` moves the action pins nothing else moves (#80).

A port of CannObserv/broker#90's ``tests/deploy/test_dependabot.py``, cut to what
this repo runs. ``ci.yml`` and ``audit.yml`` pin every action to a commit SHA, and
setup-uv publishes no floating major tag since v8: without an updater, a security
release goes unannounced. A mistyped Dependabot config stops producing PRs without
erroring anywhere a person looks, so each property is pinned here.

* **``github-actions`` only, never ``uv`` or ``pip``.** Processor owns the exact
  co-core pin, the extraction stack is held with it (AGENTS.md, #71), and co-core
  resolves from the private wheelhouse, which Dependabot's updater cannot sync.
  Dependabot *security updates* would move ``uv.lock`` whatever this file says:
  they are off in Settings → Code security (operator, 2026-10-29), which no test
  can read (docs/DEPLOYMENT.md § CI pins and Dependabot).
* **Every workflow pins each action as ``@<sha> # vX.Y.Z``.** Dependabot proposes
  in the pin's own form, so a SHA and its comment move together.
* **The open-PR limit outlasts a full batch**, derived from the workflows: past
  it Dependabot opens nothing and says so nowhere.
* **The prefix is one of AGENTS.md's commit types**, because a Dependabot PR lands
  by fast-forwarding its commit onto ``main``.

No ``exclude-paths``: unlike broker, no workflow here is generated.
"""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / ".github" / "dependabot.yml"
WORKFLOWS = REPO / ".github" / "workflows"
AGENTS = REPO / "AGENTS.md"

# YAML drops comments, so the pin's tag comment is read from the text.
USES = re.compile(r"^[ \t]+(?:- )?uses: (\S+)(.*)$", re.MULTILINE)
SHA_REF = re.compile(r"^[\w.-]+/[\w.-]+(?:/[^@\s]*)?@[0-9a-f]{40}$")
TAG_COMMENT = re.compile(r"^ # v\d+\.\d+\.\d+$")


def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text()) if CONFIG.is_file() else {}


def _updates() -> list[dict]:
    return _config().get("updates") or []


def _workflows() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.y*ml"))


def _uses(text: str) -> list[tuple[str, str]]:
    return [(ref, rest) for ref, rest in USES.findall(text) if not ref.startswith("./")]


def test_config_exists() -> None:
    assert CONFIG.is_file(), "no .github/dependabot.yml: nothing moves the SHA pins (#80)"


def test_config_is_version_2() -> None:
    """A version 1 config is ignored without an error."""
    assert _config().get("version") == 2


def test_only_github_actions_is_configured() -> None:
    ecosystems = [u.get("package-ecosystem") for u in _updates()]
    assert ecosystems == ["github-actions"], (
        f"{ecosystems}: never uv or pip. Processor owns the co-core pin and holds the "
        "extraction stack (AGENTS.md, #71), and the wheelhouse is private"
    )


def test_it_scans_the_workflows_directory() -> None:
    """``/`` is the spelling that reads ``.github/workflows/``; any other is silence."""
    (update,) = _updates()
    assert update.get("directory") == "/"
    assert "directories" not in update


def test_it_runs_weekly() -> None:
    (update,) = _updates()
    assert update["schedule"]["interval"] == "weekly"


def test_every_workflow_pins_each_action_to_a_commit_with_its_tag() -> None:
    workflows = _workflows()
    assert {"ci.yml", "audit.yml"} <= {p.name for p in workflows}, workflows
    bad = [
        f"{path.name}: {ref}{rest}"
        for path in workflows
        for ref, rest in _uses(path.read_text())
        if not (SHA_REF.match(ref) and TAG_COMMENT.match(rest))
    ]
    assert not bad, "pin as `owner/action@<40-hex sha> # vX.Y.Z`:\n  " + "\n  ".join(bad)


def test_the_pin_check_refuses_a_tag_and_a_missing_comment() -> None:
    """Guard the guard: the two forms this replaces both fail it."""
    text = (
        "    steps:\n      - uses: actions/checkout@v7\n"
        f"      - uses: astral-sh/setup-uv@{'a' * 40}\n"
        f"        uses: google-github-actions/auth@{'b' * 40} # v3.0.0\n"
        "      - uses: ./local-action\n"
    )
    found = _uses(text)
    assert [ref.split("@")[0] for ref, _ in found] == [
        "actions/checkout",
        "astral-sh/setup-uv",
        "google-github-actions/auth",
    ]
    ok = [bool(SHA_REF.match(ref) and TAG_COMMENT.match(rest)) for ref, rest in found]
    assert ok == [False, False, True]


def test_open_pr_limit_outlasts_a_full_batch() -> None:
    """One PR per action. The default is 5."""
    actions = {ref.split("@")[0] for p in _workflows() for ref, _ in _uses(p.read_text())}
    assert actions, "found no `uses:`: the derivation broke"
    (update,) = _updates()
    limit = update.get("open-pull-requests-limit", 5)
    assert limit > len(actions), f"limit {limit}, actions {sorted(actions)}"


def test_the_commit_prefix_is_one_of_agents_md_types() -> None:
    # Mid-paragraph in AGENTS.md's Conventions, unlike broker's own line.
    types = re.search(r"Types: ([a-z, ]+)\.", AGENTS.read_text())
    assert types, "AGENTS.md lost its `Types:` list"
    (update,) = _updates()
    assert update["commit-message"]["prefix"] in types.group(1).split(", ")
