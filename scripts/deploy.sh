#!/usr/bin/env bash
# Deploy processor: build a release from a pushed commit, switch, verify (#2).
#
#   scripts/deploy.sh [<ref>]     <ref> on origin/main; default origin/main
#   scripts/deploy.sh --help
#
# A rollback is a deploy of the previous build: scripts/deploy.sh <old build>.
#
# The cohort's release standard (broker#22), as CannObserv/status#9 shipped it
# (its spec's R1-R13; docs/DEPLOYMENT.md says where processor differs). The unit
# runs /srv/processor/live, a symlink into releases/<build>: a read-only
# `git archive` of one commit on origin/main, with its own venv. Nothing done in
# a checkout reaches the unit until this script puts it there.
#
# In this order:
#   1. build releases/<build> (or reuse it): archive, wheelhouse, uv sync,
#      REVISION last, read-only
#   2. switch the link (rename(2), atomic), then install the unit from the
#      release when it differs from the installed copy
#   3. restart processor (it lets the in-flight command finish: up to
#      TimeoutStopSec=240)
#   4. verify: the new process logs `starting` with this build and
#      child_containment required, then `consuming`; then the smoke run, as the
#      service user with the unit's environment, credential and sandboxing
#      (scripts/smoke_scratch_bus.py: the scratch bus, the production bucket)
#   5. on failure, switch back, unit too, restart, and prove the old build the
#      same way
#
# Runs as exedev; sudo for systemctl, systemd-run and the unit file only. Exits
# 0 when verified, 4 when processor is left on a build that did not answer (no
# rollback possible, or the old build failed too), 1 otherwise.
set -euo pipefail

ROOT="${PROCESSOR_DEPLOY_ROOT:-/srv/processor}"
ETC="${PROCESSOR_DEPLOY_ETC:-/etc}"
KEEP="${PROCESSOR_DEPLOY_KEEP:-5}"
# Past the unit's TimeoutStopSec=240: a restart waits for the in-flight command.
VERIFY_SECONDS="${PROCESSOR_DEPLOY_VERIFY_SECONDS:-300}"
# The system interpreter: the service user has no home and cannot read
# /home/exedev, so a uv-managed Python under ~/.local/share/uv would not start.
PYTHON="${PROCESSOR_DEPLOY_PYTHON:-/usr/bin/python3.12}"
UNIT=processor
UNIT_DIR="$ETC/systemd/system"
SMOKE_UNIT=processor-smoke
# deploy/<file>=<path under /etc>, compared after a deploy, never installed.
# tests/test_units.py holds every file under deploy/ to being the unit, one of
# these, or a script.
HOST_CONFIGS=(
  "tailscaled.service.d/90-processor-oom.conf=systemd/system/tailscaled.service.d/90-processor-oom.conf"
  "apt/nodesource.sources=apt/sources.list.d/nodesource.sources"
  "apt/nodesource.pref=apt/preferences.d/nodesource.pref"
)
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

note() { echo "deploy: $*" >&2; }
die() {
  note "$*"
  exit 1
}
dead() {
  note "$*"
  exit 4
}

usage() { sed -n '4,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

ref=""
while (($#)); do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    -*) die "unknown flag $1 (see --help)" ;;
    *)
      [[ -z "$ref" ]] || die "one ref at a time"
      ref="$1"
      ;;
  esac
  shift
done
ref="${ref:-origin/main}"

# Releases belong to exedev and are read-only; the unit's user reads them and
# can never make them writable again (#2).
[[ "$(id -u)" -eq 0 ]] && die "run as exedev, not root; the script sudoes for systemctl itself"

# A release has a copy of this script and no .git to build from.
git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1 ||
  die "run from a checkout (~/processor/scripts/deploy.sh); $SRC is not one"

[[ -d "$ROOT" ]] ||
  die "$ROOT does not exist. Once: sudo install -d -o exedev -g exedev -m 755 $ROOT"
# co-core is private: uv resolves it from ./.wheelhouse (AGENTS.md).
[[ -d "$SRC/.wheelhouse" ]] || die "$SRC/.wheelhouse is missing; populate it first (AGENTS.md)"

exec 9>"$ROOT/.deploy.lock"
flock -n 9 || die "another deploy is running (it holds $ROOT/.deploy.lock)"

# The installed unit this deploy replaces, kept for a switch back.
backup="$(mktemp -d "${TMPDIR:-/tmp}/processor-deploy.XXXXXX")"
trap 'rm -rf "$backup" || :' EXIT

# --- which commit ----------------------------------------------------------

git -C "$SRC" fetch --quiet --prune origin
sha="$(git -C "$SRC" rev-parse --verify --quiet "${ref}^{commit}")" || die "cannot resolve $ref"
git -C "$SRC" merge-base --is-ancestor "$sha" origin/main ||
  die "$ref ($sha) is not on origin/main; only a pushed main commit is deployed"
build="$(git -C "$SRC" rev-parse --short=12 "$sha")"
release="$ROOT/releases/$build"

# --- the release -----------------------------------------------------------

make_writable() { chmod -R u+w "$1"; }

build_release() {
  if [[ -e "$release" ]]; then
    make_writable "$release"
    rm -rf "$release"
  fi
  note "building $build"
  mkdir -p "$release"
  git -C "$SRC" archive "$sha" | tar -x -C "$release"
  cp -rL "$SRC/.wheelhouse" "$release/.wheelhouse"
  # Built where it will run: a uv venv embeds its absolute path in its scripts.
  # Non-editable: sys.path, and so the child's Landlock allowlist, then holds the
  # stdlib and site-packages alone (#2).
  (cd "$release" && UV_PYTHON_DOWNLOADS=never uv sync --locked --no-dev --no-editable \
    --compile-bytecode --python "$PYTHON" --quiet) ||
    die "uv sync failed for $build; nothing switched"
  local home
  home="$(sed -n 's/^home *= *//p' "$release/.venv/pyvenv.cfg")"
  [[ -n "$home" && "$home" != /home/* ]] ||
    die "$build's venv runs ${home:-an unknown interpreter}, which the service user cannot; nothing switched"
  # REVISION last: a release without one is an interrupted build.
  echo "$build" >"$release/REVISION"
  chmod -R a-w,go+rX "$release"
}

# The build a link names, by its last component: a link made by hand during a
# recovery may be absolute.
release_of() {
  local link
  link="$(readlink "$ROOT/live" 2>/dev/null)" || return 0
  basename "$link"
}

# Why this release cannot be reused as it stands; nothing when it can.
unusable() {
  local out
  if [[ ! -e "$release" ]]; then
    echo "not built"
  elif [[ ! -f "$release/REVISION" ]]; then
    echo "an interrupted build"
  elif [[ -w "$release" ]]; then
    echo "writable, so never finished"
  elif ! out="$("$release/.venv/bin/python" -c 'import processor.__main__, co_core, lxml' 2>&1)"; then
    echo "a venv that no longer runs (${out:-no output})"
  fi
}

# A release live runs is never rebuilt in place: that pulls the code out from
# under the service, and a failed sync leaves it with no release at all.
why="$(unusable)"
if [[ -z "$why" ]]; then
  note "reusing release $build"
else
  [[ "$why" == "not built" || "$(release_of)" != "$build" ]] ||
    die "release $build is $why, and live runs it. Deploy another build first; this one is then rebuilt."
  [[ "$why" == "not built" ]] || note "release $build is $why; rebuilding"
  build_release
fi
touch "$release" # prune by last deploy, not first build

# The unit's user must exist before anything switches (DEPLOYMENT.md § Install).
user="$(sed -n 's/^User=//p' "$release/deploy/$UNIT.service")"
getent passwd "$user" >/dev/null ||
  die "the unit runs as $user, who does not exist here; nothing switched (DEPLOYMENT.md § Install)"

# --- switch, verify, or switch back ----------------------------------------

# What the tree a link names reports as its build: its REVISION, else "dev",
# the rule src/processor/build.py applies.
served_build() {
  local dir rev
  dir="$(cd "$ROOT" && cd -P "$1" 2>/dev/null && pwd)" || {
    echo dev
    return 0
  }
  rev="$(cat "$dir/REVISION" 2>/dev/null)" || rev=""
  echo "${rev:-dev}"
}

swap() { # <link> <target>: rename(2) over the old link, so there is never no link
  ln -sfn "$2" "$1.new"
  mv -Tf "$1.new" "$1"
}

# Installs the release's unit when it differs from the installed copy, after
# keeping that copy aside for restore_unit. Called as `install_unit || ...`.
install_unit() {
  local name="$UNIT.service" unit="$UNIT_DIR/$UNIT.service"
  cmp -s "$release/deploy/$name" "$unit" && return 0
  if [[ -e "$unit" ]]; then
    cp "$unit" "$backup/$name" || {
      note "cannot keep $unit aside; not installing it"
      return 1
    }
  else
    : >"$backup/$name.added"
  fi
  sudo install -m 644 "$release/deploy/$name" "$unit" || {
    note "installing $name in $UNIT_DIR failed"
    return 1
  }
  sudo systemctl daemon-reload || {
    note "systemctl daemon-reload failed"
    return 1
  }
  note "unit installed from $build: $name"
  logger -t processor-deploy "unit from $build: $name" || true
}

# Switch back, for the unit. A failure is a note: the rollback still proves the
# old build, and says whether it answers.
restore_unit() {
  local name="$UNIT.service"
  if [[ -f "$backup/$name" ]]; then
    sudo install -m 644 "$backup/$name" "$UNIT_DIR/$name" || note "restoring $name failed"
  elif [[ -f "$backup/$name.added" ]]; then
    sudo rm -f "$UNIT_DIR/$name" || note "removing $UNIT_DIR/$name failed"
  else
    return 0
  fi
  sudo systemctl daemon-reload || note "systemctl daemon-reload failed"
  logger -t processor-deploy "unit restored" || true
}

# The new process's own start records: `starting` naming <build> under required
# containment, then `consuming`. By MainPID, so the old process's records, which
# name the old build, never count.
started_on() { # <build>
  local want="$1" pid records deadline=$((SECONDS + VERIFY_SECONDS))
  while ((SECONDS < deadline)); do
    pid="$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null)" || pid=0
    if [[ -n "$pid" && "$pid" != 0 ]] &&
      records="$(journalctl -u "$UNIT" "_PID=$pid" -o cat --no-pager 2>/dev/null)" &&
      [[ "$(jq -rR 'fromjson? | select(.message == "starting")
          | "\(.build) \(.child_containment)"' <<<"$records" | tail -n 1)" == "$want required" ]] &&
      jq -eR 'fromjson? | select(.message == "consuming")' <<<"$records" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  note "processor did not log starting on $want (child_containment required), then consuming," \
    "within ${VERIFY_SECONDS}s: journalctl -u $UNIT -n 50"
  return 1
}

# The smoke run, as the unit runs: its user, environment file, credential and
# sandboxing, read from the release's own unit. systemd-run does not expand %d,
# so the credentials directory is spelled out.
smoke() { # <build>
  local dir="$ROOT/releases/$1" key value out props=()
  while IFS='=' read -r key value; do
    case "$key" in
      User | Group | EnvironmentFile | LoadCredential | Environment | NoNewPrivileges | PrivateTmp | \
        ProtectSystem | ProtectHome)
        props+=(-p "$key=${value//%d//run/credentials/$SMOKE_UNIT.service}")
        ;;
    esac
  done < <(grep -E '^[A-Za-z]+=' "$dir/deploy/$UNIT.service")
  if out="$(sudo systemd-run --quiet --pipe --wait --collect --unit="$SMOKE_UNIT" \
    "${props[@]}" -p "WorkingDirectory=$dir" \
    "$dir/.venv/bin/python" scripts/smoke_scratch_bus.py 2>&1)" &&
    [[ "$(jq -rR 'fromjson? | select(.result == "pass") | "\(.build) \(.child_containment)"' \
      <<<"$out" | tail -n 1)" == "$1 required" ]]; then
    return 0
  fi
  note "the smoke run failed on $1: ${out:-no output}"
  return 1
}

restart_and_verify() { # <build>
  # The deploy that fixes a crash loop is the one that finds the unit past its
  # StartLimitBurst, and systemd refuses manual starts there too.
  sudo systemctl reset-failed "$UNIT" || true
  sudo systemctl restart "$UNIT" || {
    note "systemctl restart $UNIT failed"
    return 1
  }
  started_on "$1" && smoke "$1"
}

compare_host_configs() {
  local entry rel dest
  for entry in "${HOST_CONFIGS[@]}"; do
    rel="${entry%%=*}" dest="$ETC/${entry#*=}"
    [[ -f "$release/deploy/$rel" ]] || continue
    if [[ ! -e "$dest" ]]; then
      note "host config deploy/$rel is not installed at $dest (docs/DEPLOYMENT.md)"
    elif ! cmp -s "$release/deploy/$rel" "$dest"; then
      note "host config deploy/$rel differs from $dest; install it by hand (docs/DEPLOYMENT.md)"
    fi
  done
  return 0
}

link="$ROOT/live"
previous_link="$(readlink "$link" 2>/dev/null || true)"
previous="$(release_of)"

swap "$link" "releases/$build"
logger -t processor-deploy "live -> $build (was ${previous_link:-nothing})" || true
units_ok=1
install_unit || units_ok=0

if ((units_ok)) && restart_and_verify "$build"; then
  note "live is on $build"
else
  if [[ -z "$previous" ]]; then
    # A first deploy: nothing to switch back to. The unit it replaced goes back,
    # but the install's other steps (the user, /etc/processor) are the
    # operator's to undo: DEPLOYMENT.md § Rollback.
    restore_unit
    sudo systemctl reset-failed "$UNIT" || true
    logger -t processor-deploy "live failed on $build; no previous release" || true
    dead "live failed on $build, and there is no previous release to return to;" \
      "the unit it replaced is back. docs/DEPLOYMENT.md § Rollback"
  fi
  if [[ "$previous" == "$build" ]]; then
    [[ -f "$backup/$UNIT.service" || -f "$backup/$UNIT.service.added" ]] ||
      dead "live failed on $build, which it was already running; there is nothing to switch back to"
    old="$build" back="put back the unit it replaced, on $build"
  else
    old="$(served_build "$previous_link")" back="switched back to $old"
    swap "$link" "$previous_link"
  fi
  restore_unit
  if restart_and_verify "$old"; then
    logger -t processor-deploy "live rolled back to $old after $build failed ($back)" || true
    die "live failed on $build; $back, which is answering"
  fi
  logger -t processor-deploy "live failed on $build; $back, which is NOT answering" || true
  dead "live failed on $build; $back, which is NOT answering: journalctl -u $UNIT -n 50"
fi
compare_host_configs

# --- prune -----------------------------------------------------------------

live_build="$(release_of)"
# shellcheck disable=SC2012 # names are 12 hex characters
ls -1t "$ROOT/releases" | tail -n +$((KEEP + 1)) | while read -r old; do
  [[ "$old" == "$live_build" ]] && continue
  note "pruning release $old"
  # REVISION first: a prune cut short leaves an interrupted build, never a
  # "complete" one with half its files. A failed prune is a note.
  { make_writable "$ROOT/releases/$old" && rm -f "$ROOT/releases/$old/REVISION" &&
    rm -rf "${ROOT:?}/releases/$old"; } ||
    note "prune failed for $old; remove it by hand (chmod -R u+w first)"
done
