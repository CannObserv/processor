#!/usr/bin/env bash
# Deploy processor: build a release from a pushed commit, switch, verify (#2).
#
#   scripts/deploy.sh [<ref>]            <ref> on origin/main, CI green; default origin/main
#   scripts/deploy.sh --skip-ci [<ref>]  without asking CI: an emergency, logged
#   scripts/deploy.sh --help
#
# A rollback is a deploy of the previous build: scripts/deploy.sh <old build>.
# Gated the same way (#34).
#
# Only once the commit's CI passed: its push run on main and every job in it
# green, lint and test among them, asked of GitHub before anything is built
# (#34, as status#11; docs/DEPLOYMENT.md § The CI gate).
#
# The cohort's release standard (broker#22), as CannObserv/status#9 shipped it
# (its spec's R1-R13; docs/DEPLOYMENT.md says where processor differs). The unit
# runs /srv/processor/live, a symlink into releases/<build>: a read-only
# `git archive` of one commit on origin/main, with its own venv. Nothing done in
# a checkout reaches the unit until this script puts it there.
#
# In this order:
#   0. ask GitHub whether the commit's CI passed (unless --skip-ci)
#   1. build releases/<build> (or reuse it): archive, wheelhouse, uv sync,
#      REVISION last, read-only
#   2. switch the link (rename(2), atomic), then install each unit under deploy/
#      from the release when it differs from the installed copy (#35)
#   3. restart processor (it lets the in-flight command finish: up to
#      TimeoutStopSec=240)
#   4. verify: the new process logs `starting` with this build and
#      child_containment required, then `consuming`; then the smoke run, as the
#      service user with the unit's environment, credential and sandboxing
#      (scripts/smoke_scratch_bus.py: the scratch bus, the production bucket)
#   5. on failure, switch back, units too, restart, and prove the old build the
#      same way; on success, enable a timer this deploy installed new
#
# Runs as exedev; sudo for systemctl, systemd-run and the unit files only. Exits
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
# CI takes about 3 minutes; a run still going after this is wedged or queued behind one.
CI_WAIT_SECONDS="${PROCESSOR_DEPLOY_CI_WAIT_SECONDS:-600}"
CI_POLL_SECONDS="${PROCESSOR_DEPLOY_CI_POLL_SECONDS:-30}"
# The jobs a run must have, which tests/test_deploy.py holds ci.yml to; every
# job a run lists must pass too, named here or not (status#11, CR 6).
CI_JOBS=(lint test)
GITHUB_API="https://api.github.com/repos/CannObserv/processor"
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

usage() { sed -n '4,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

skip_ci=0
ref=""
while (($#)); do
  case "$1" in
    --skip-ci) skip_ci=1 ;;
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

# The installed units this deploy replaces, kept for a switch back.
backup="$(mktemp -d "${TMPDIR:-/tmp}/processor-deploy.XXXXXX")"
trap 'rm -rf "$backup" || :' EXIT

# --- which commit ----------------------------------------------------------

git -C "$SRC" fetch --quiet --prune origin
sha="$(git -C "$SRC" rev-parse --verify --quiet "${ref}^{commit}")" || die "cannot resolve $ref"
git -C "$SRC" merge-base --is-ancestor "$sha" origin/main ||
  die "$ref ($sha) is not on origin/main; only a pushed main commit is deployed"
build="$(git -C "$SRC" rev-parse --short=12 "$sha")"
release="$ROOT/releases/$build"

# --- CI (#34) --------------------------------------------------------------
#
# CannObserv/status's gate (status#11), ported: its CR numbers are status#11's.
# Asked before anything is built, so a refusal changes nothing. Rollbacks too:
# a kept release proves it was built, not that its CI passed; --skip-ci is the
# escape when GitHub cannot answer.

# Unauthenticated: the repo is public, and 60 requests an hour per address
# covers a deploy's 2 (22 waiting the full 600 s). No token, so none to store.
# A refusal (rate limit, outage, no answer) never passes.
github() { # <path>: GitHub's JSON answer, or a refusal naming GitHub's message
  local out
  if out="$(curl -sS --fail-with-body --max-time 10 \
    -H 'Accept: application/vnd.github+json' "$GITHUB_API/$1")"; then
    # A JSON object, or refused: jq reads an empty body as no input at all,
    # prints nothing and exits 0, so an empty jobs answer listed no failed job
    # and passed (#34 CR 5).
    jq -e 'type == "object"' <<<"$out" >/dev/null 2>&1 ||
      die "GitHub's answer about $build's CI is not the JSON expected; nothing was built." \
        "Deploy again later, or pass --skip-ci."
    printf '%s\n' "$out"
    return
  fi
  out="$(jq -r '.message // empty' <<<"$out" 2>/dev/null)" || out=""
  die "GitHub did not answer about $build's CI.${out:+ GitHub says: $out}" \
    "Nothing was built; deploy again later, or pass --skip-ci."
}

# The run that decides: the newest push run of ci.yml on main for exactly this
# commit. Every FF merge also has a pull_request run on the same SHA, and a
# dispatch adds a workflow_dispatch one; check runs cannot tell them apart. A
# re-run counts, since a run reports its latest attempt.
push_run() { # the run as JSON, or nothing
  jq -c --arg sha "$sha" '[.workflow_runs[]
    | select(.head_sha == $sha and .event == "push" and .head_branch == "main")]
    | max_by(.created_at) // empty' ||
    die "GitHub's answer about $build's CI runs is not the JSON expected; nothing was built"
}

# Waits for the run, bounded. A commit behind origin/main's tip with no run is
# refused at once: GitHub runs CI on the newest commit of each push only, so it
# will not get one, unless it was pushed seconds ago with another push after it.
# An FF merge pushes a PR's every commit, and only its tip has a run.
finished_run() {
  local deadline=$((SECONDS + CI_WAIT_SECONDS)) tip run state url left
  tip="$(git -C "$SRC" rev-parse origin/main)"
  while :; do
    run="$(github "actions/workflows/ci.yml/runs?head_sha=$sha&event=push&branch=main&per_page=100" | push_run)" ||
      exit 1
    left=$((deadline - SECONDS))
    if [[ -z "$run" && "$sha" != "$tip" ]]; then
      die "no CI run for $build as a push to main. GitHub runs CI on the newest commit of each push" \
        "only: deploy that one, or pass --skip-ci. Pushed in the last minute, with another push" \
        "after it? Its run may not be listed yet: deploy again shortly. Nothing was built."
    elif [[ -z "$run" ]]; then
      state="not queued yet" url=""
      ((left > 0)) ||
        die "no CI run for $build after ${CI_WAIT_SECONDS}s ([skip ci]?). Pass --skip-ci to deploy it" \
          "anyway. Nothing was built."
    else
      state="$(jq -r .status <<<"$run")"
      url="$(jq -r .html_url <<<"$run")"
      [[ "$state" == completed ]] && {
        printf '%s\n' "$run"
        return
      }
      ((left > 0)) ||
        die "CI for $build is still $state after ${CI_WAIT_SECONDS}s. Nothing was built; deploy again" \
          "when it finishes. Run: $url"
    fi
    note "waiting for CI on $build ($state)${url:+: $url}"
    sleep $((left < CI_POLL_SECONDS ? left : CI_POLL_SECONDS))
  done
}

# Success only, from the run itself, every job it lists, and at least CI_JOBS.
# A skipped job leaves the run "success", so the jobs are read too. CI_JOBS is
# only the floor: it comes from this checkout, which may be older than ci.yml,
# and a job it does not name still counts (CR 6).
ci_gate() {
  local run url conclusion jobs problems
  run="$(finished_run)" || exit 1
  url="$(jq -r .html_url <<<"$run")"
  conclusion="$(jq -r '.conclusion // "nothing"' <<<"$run")"
  jobs="$(github "actions/runs/$(jq -r .id <<<"$run")/jobs?per_page=100")" || exit 1
  problems="$(jq -r --arg required "${CI_JOBS[*]}" '[
      (.jobs[] | select(.conclusion != "success") | "\(.name) (\(.conclusion // .status))"),
      (($required | split(" "))[] as $name | select(any(.jobs[]; .name == $name) | not)
        | "\($name) (not in the run)")
    ] | join(", ")' <<<"$jobs")" ||
    die "GitHub's answer about $build's CI jobs is not the JSON expected; nothing was built"
  [[ "$conclusion" == success ]] || problems="run concluded $conclusion${problems:+; $problems}"
  # Cancelled is not a verdict, and has nothing to fix (CR 12). ci.yml's
  # concurrency group keeps one pending run on main: a newer push or a dispatch
  # cancels this one while it waits behind a run in progress (#34 CR 1). A run
  # that timed out waiting for a runner ends cancelled too (#23's PR).
  local remedy="fix it on main"
  [[ "$conclusion" == cancelled ]] && remedy="re-run it from its page (a re-run counts), or deploy a newer commit"
  [[ -z "$problems" ]] || die "CI did not pass for $build: $problems. Nothing was built; $remedy. Run: $url"
  note "CI passed for $build: $url"
  logger -t processor-deploy "CI passed for $build: $url" || true
}

if ((skip_ci)); then
  note "not asking CI about $build (--skip-ci)"
  logger -t processor-deploy "CI not checked for $build (--skip-ci)" || true
else
  ci_gate
fi

# --- the release -----------------------------------------------------------

make_writable() { chmod -R u+w "$1"; }

build_release() {
  if [[ -e "$release" ]]; then
    make_writable "$release"
    rm -rf "$release"
  fi
  note "building $build"
  # The parent explicitly, repaired if an earlier deploy left it 700: `mkdir -p`
  # takes the operator's umask (700 under 077), and the service user could then
  # reach no release at all (CR 14).
  install -d -m 755 "$ROOT/releases"
  mkdir -p "$release"
  # Each says why it stopped: under set -e alone the deploy would exit silently. The
  # release, left without REVISION, is rebuilt next time (CR 13).
  git -C "$SRC" archive "$sha" | tar -x -C "$release" ||
    die "git archive failed for $build; nothing switched"
  cp -rL "$SRC/.wheelhouse" "$release/.wheelhouse" ||
    die "copying the wheelhouse failed for $build; nothing switched"
  # Built where it will run: a uv venv embeds its absolute path in its scripts.
  # Non-editable: sys.path, and so the child's Landlock allowlist, then holds the
  # stdlib and site-packages alone (#2). Copied, never hardlinked from ~/.cache/uv:
  # a hardlink is the same inode as the cache's and every dev venv's, so the chmod
  # below would reach them, and an edit to any of them would reach production
  # (CR 11).
  (cd "$release" && UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never uv sync --locked --no-dev --no-editable \
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

# --- units (#35; status#18) --------------------------------------------------
#
# Every deploy/*.service and deploy/*.timer in the release is installed: before
# #35 only processor.service was, and a new unit would never have reached
# $UNIT_DIR (status#18 had the same bug). tests/test_units.py accounts for each
# file under deploy/.

units_of() { # the release's units, by name
  local path
  for path in "$release"/deploy/*.service "$release"/deploy/*.timer; do
    [[ -f "$path" ]] && basename "$path"
  done
  return 0
}

# Installs the release's units that differ from their installed copies, after
# keeping each copy aside for restore_units. One daemon-reload, and a changed
# timer is try-restarted so it re-arms on its new schedule. A new timer is
# enabled later, by enable_new_timers, once the deploy verifies. Units the
# release lacks stay as they are.
# Called as `install_units || ...`, so set -e is off in here: every step that
# can fail says so itself.
install_units() {
  local name unit changed=() added=() timers=()
  { mkdir -p "$backup/units" && : >"$backup/units.added"; } ||
    { note "cannot keep the installed units aside in $backup"; return 1; }
  while read -r name; do
    unit="$UNIT_DIR/$name"
    cmp -s "$release/deploy/$name" "$unit" && continue
    if [[ -e "$unit" ]]; then
      cp "$unit" "$backup/units/$name" ||
        { note "cannot keep $unit aside; not installing it"; return 1; }
      changed+=("$name")
      [[ "$name" != *.timer ]] || timers+=("$name")
    else
      echo "$name" >>"$backup/units.added"
      added+=("$name")
    fi
    sudo install -m 644 "$release/deploy/$name" "$unit" ||
      { note "installing $name in $UNIT_DIR failed"; return 1; }
  done < <(units_of)
  ((${#changed[@]} + ${#added[@]})) || return 0
  sudo systemctl daemon-reload || { note "systemctl daemon-reload failed"; return 1; }
  # A timer only watches: one that will not re-arm is a note, never a reason to
  # switch processor back (#35 trap 4).
  for name in "${timers[@]}"; do
    sudo systemctl try-restart "$name" || note "systemctl try-restart $name failed: systemctl status $name"
  done
  local list="${changed[*]}"
  for name in "${added[@]}"; do list+=" $name (new)"; done
  note "units installed from $build: ${list# }"
  logger -t processor-deploy "units from $build: ${list# }" || true
}

# Whether install_units replaced or added any unit.
units_replaced() {
  compgen -G "$backup/units/*" >/dev/null || [[ -s "$backup/units.added" ]]
}

# Switch back, for units: the copies install_units replaced go back, and what it
# added is removed. A failure is a note: the rollback still proves the old build,
# and says whether it answers.
restore_units() {
  local path name timers=() any=0
  for path in "$backup/units"/*; do
    [[ -f "$path" ]] || continue
    name="$(basename "$path")"
    sudo install -m 644 "$path" "$UNIT_DIR/$name" || note "restoring $name failed: $path"
    [[ "$name" != *.timer ]] || timers+=("$name")
    any=1
  done
  # Missing when install_units could not even start; set -e is on here, and a
  # failed redirect would end the rollback before it proves the old build (status CR 4).
  if [[ -f "$backup/units.added" ]]; then
    while read -r name; do
      sudo rm -f "$UNIT_DIR/$name" || note "removing $UNIT_DIR/$name failed"
      any=1
    done <"$backup/units.added"
  fi
  ((any)) || return 0
  sudo systemctl daemon-reload || note "systemctl daemon-reload failed"
  for name in "${timers[@]}"; do
    sudo systemctl try-restart "$name" || note "systemctl try-restart $name failed"
  done
  logger -t processor-deploy "units restored" || true
}

# A timer this deploy installed new is enabled once the deploy verifies, so a
# deploy switched back never ran it. One installed before is never enabled
# again: an operator's `systemctl disable --now` sticks (#35). Status installs
# without enabling; here the drift timer is the decision #35 made. A failure is
# a note: the timer watches the deploy, it never decides one.
enable_new_timers() {
  local name
  [[ -f "$backup/units.added" ]] || return 0
  while read -r name; do
    [[ "$name" == *.timer ]] || continue
    if sudo systemctl enable --now "$name"; then
      note "$name enabled"
      logger -t processor-deploy "$name enabled" || true
    else
      note "systemctl enable --now $name failed; enable it by hand (docs/DEPLOYMENT.md § The drift check)"
    fi
  done <"$backup/units.added"
}

# The new process's own start records: `starting` naming <build> under required
# containment, then `consuming`. By MainPID, this boot, since the restart: an
# older process that had the same PID never counts (CR 5).
started_on() { # <build> <restart time, epoch seconds>
  local want="$1" since="$2" pid records deadline=$((SECONDS + VERIFY_SECONDS))
  while ((SECONDS < deadline)); do
    pid="$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null)" || pid=0
    if [[ -n "$pid" && "$pid" != 0 ]] &&
      records="$(journalctl -b --since "@$since" "_SYSTEMD_UNIT=$UNIT.service" "_PID=$pid" \
        -o cat --no-pager 2>/dev/null)" &&
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
  local since
  since="$(date +%s)"
  # The deploy that fixes a crash loop is the one that finds the unit past its
  # StartLimitBurst, and systemd refuses manual starts there too.
  sudo systemctl reset-failed "$UNIT" || true
  sudo systemctl restart "$UNIT" || {
    note "systemctl restart $UNIT failed"
    return 1
  }
  started_on "$1" "$since" && smoke "$1"
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
install_units || units_ok=0

if ((units_ok)) && restart_and_verify "$build"; then
  note "live is on $build"
  enable_new_timers
else
  if [[ -z "$previous" ]]; then
    # A first deploy: nothing to switch back to. The unit it replaced goes back,
    # and the service restarts on it: left alone, the process that failed the
    # verify would keep running the new release, consuming, under a unit file
    # that no longer describes it (CR 3). The install's other steps (the user,
    # /etc/processor) are the operator's to undo: DEPLOYMENT.md § Rollback.
    restore_units
    sudo systemctl reset-failed "$UNIT" || true
    sudo systemctl restart "$UNIT" || note "systemctl restart $UNIT failed on the restored unit"
    logger -t processor-deploy "live failed on $build; no previous release; restarted on the unit it replaced" || true
    dead "live failed on $build, and there is no previous release to return to;" \
      "restarted on the unit it replaced. docs/DEPLOYMENT.md § Rollback"
  fi
  if [[ "$previous" == "$build" ]]; then
    units_replaced ||
      dead "live failed on $build, which it was already running; there is nothing to switch back to"
    old="$build" back="put back the units it replaced, on $build"
  else
    old="$(served_build "$previous_link")" back="switched back to $old"
    swap "$link" "$previous_link"
  fi
  restore_units
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
