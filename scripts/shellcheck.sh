#!/usr/bin/env bash
# Lint this repo's own shell scripts: the tree, or with --staged what a commit is
# about to take (#81, as CannObserv/broker#112). ci.yml's lint job runs the first
# and .githooks/pre-commit the second, so both lint the same files with the same
# binary: shellcheck-py from uv.lock's lint group (docs/DEPLOYMENT.md § Shell lint).
#
# Ours is every tracked *.sh and .githooks/*. Not a symlink: the .claude/hooks/ ones
# point into skills-vendor/, upstream's code under upstream's rules, as ruff's
# extend-exclude decides. Not .skills/ either: the vendored refresh hook rewrites
# doctor.sh there, with no review. A finding in either is an issue on
# gregoryfoster/skills. tests/test_shellcheck.py pins it.
#
# --no-config: [tool.uv] find-links is read even under --only-group, and a clone
# has no ./.wheelhouse (#79's stand-in for broker's tracked .wheelhouse/.gitkeep).
set -euo pipefail

cd "$(dirname "$0")/.."

case ${1:-} in
'') list=(git ls-files -z) ;;
--staged) list=(git diff --cached --name-only --diff-filter=ACMR -z) ;;
*)
	echo "usage: $0 [--staged]" >&2
	exit 2
	;;
esac

files=()
while IFS= read -r -d '' f; do
	case $f in
	skills-vendor/* | .skills/*) continue ;;
	*.sh | .githooks/*) ;;
	*) continue ;;
	esac
	[ -L "$f" ] || files+=("$f")
done < <("${list[@]}")

[ ${#files[@]} -gt 0 ] || exit 0
exec uv run --frozen --no-config --only-group lint shellcheck -- "${files[@]}"
