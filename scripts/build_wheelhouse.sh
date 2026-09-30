#!/usr/bin/env bash
# Build co-core, co-core-aio and co-core-sync wheels from a cannobserv release tag
# into ./.wheelhouse — the stand-in for scripts/sync_wheelhouse.py until this VM
# holds a co-pypi-reader key. Same filenames as the mirrored index, and find-links
# locks by filename, so uv.lock is satisfied by either source.
#
# Usage: GH_TOKEN_CANNOBSERV=... scripts/build_wheelhouse.sh [tag]   (default: the pin)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
tag="${1:-v0.19.7}"
: "${GH_TOKEN_CANNOBSERV:?set GH_TOKEN_CANNOBSERV (read access to CannObserv/cannobserv)}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The token rides an http.extraHeader from the environment, never argv or the
# clone URL (which git would persist in the clone's config).
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$GH_TOKEN_CANNOBSERV" | base64 -w0)" \
  git -c advice.detachedHead=false clone -q --depth 1 --branch "$tag" \
  https://github.com/CannObserv/cannobserv.git "$work/cannobserv"

mkdir -p "$root/.wheelhouse"
for pkg in co-core co-core-aio co-core-sync; do
  (cd "$work/cannobserv" && uv build -q --package "$pkg" --wheel --out-dir "$root/.wheelhouse")
done
ls -1 "$root/.wheelhouse"/*.whl
