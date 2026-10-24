#!/usr/bin/env bash
# Audit uv.lock against OSV and report what it finds. Never fail (#73, ported
# from CannObserv/broker#86).
#
# Run by .github/workflows/audit.yml (push, PR, weekly, dispatch) and by hand.
# Report-only on purpose: a gate that failed on a finding would turn main red on
# someone else's disclosure date. It is its own workflow, so scripts/deploy.sh's
# CI gate (ci.yml's run alone) never reads it. Every exit is 0:
#   uv exit 1 (findings)        -> warning "uv.lock advisories", plus where it goes
#   any other non-zero exit     -> warning "uv audit did not run", never clean
#   checkout not entered        -> warning "uv audit did not run", uv never called
# The audit text goes to the job summary. In Actions the stdout copy sits between
# stop-commands and a random resume token: it is OSV's text, and a line of it
# starting `::` would otherwise be read as a workflow command.
#
# --frozen reads uv.lock as committed. A relock would need the private wheelhouse
# (AGENTS.md, Environment & Tooling), which the job has no credential to fetch.
# `uv audit` is a preview command, hence the flag. uv before 0.11.19
# (astral-sh/uv#19645) has the command but not the name `audit-command`, and
# rejects it with exit 2 and "Unknown feature flag", so that one rejection retries
# without the flag (broker#103). From 0.11.20 an unknown name only warns.
# tests/test_lock_audit.py pins all of it.
set -u

# The lock is the one beside this script. A checkout it cannot enter is not
# audited: the caller's directory may hold some other lock (SC2164).
if ! cd "$(dirname "$0")/.." 2>/dev/null; then
	echo "::warning title=uv audit did not run::cannot enter the checkout at $(dirname "$0")/..; the lock was not audited."
	exit 0
fi

out=$(uv audit --frozen --preview-features audit-command 2>&1)
rc=$?
if [ "$rc" -eq 2 ] && [[ $out == *"Unknown feature flag"* ]]; then
	out=$(uv audit --frozen 2>&1)
	rc=$?
fi

# Where a finding goes (#71): Processor's rule, not a generic upgrade.
section="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-CannObserv/processor}/blob/main/docs/DEPLOYMENT.md#extraction-dependencies"
routing="Where a finding goes ($section):
- every finding: \`uv lock --upgrade-package NAME\`, plus a \`[tool.uv] constraint-dependencies\` floor checked in tests/test_lock_floors.py, so a later re-lock cannot undo it;
- in the extraction stack too (pypdf, lxml, bs4, html5lib, openpyxl, ...; \`EXTRACTION_STACK\` in tests/test_pin.py): bump \`LOCAL_GENERATION\`, with its notice to Watcher."

if [ -n "${GITHUB_ACTIONS:-}" ]; then
	token=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
	echo "::stop-commands::$token"
	printf '%s\n' "$out"
	echo "::$token::"
	fence='```'
	{
		printf '### uv audit --frozen (exit %s)\n\n%s\n%s\n%s\n' "$rc" "$fence" "$out" "$fence"
		[ "$rc" -eq 1 ] && printf '\n%s\n' "$routing"
	} >>"${GITHUB_STEP_SUMMARY:-/dev/null}"
else
	printf '%s\n' "$out"
	[ "$rc" -eq 1 ] && printf '\n%s\n' "$routing"
fi

case $rc in
0) ;;
1) echo "::warning title=uv.lock advisories::uv audit found advisories in uv.lock; the job summary lists them. Re-lock with a constraint-dependencies floor; an extraction-stack package also bumps LOCAL_GENERATION (docs/DEPLOYMENT.md § Extraction dependencies)." ;;
*) echo "::warning title=uv audit did not run::uv audit exited $rc; the lock was not audited. The job summary has its output." ;;
esac
exit 0
