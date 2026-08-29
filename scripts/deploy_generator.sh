#!/usr/bin/env bash
# scripts/deploy_generator.sh — deploy the host generator copy at /opt/eeebot-techtree
#
# The /opt/eeebot-techtree/ copy run by eeebot-techtree-publish.service is a
# manual snapshot that does NOT update automatically when repo master changes.
# This script closes that gap (issue #101):
#
#   1. Captures the short git sha of the copy being deployed.
#   2. Backs up the prior /opt copy with a UTC timestamp.
#   3. Copies scripts/techtree_viewer.py and scripts/techtree_autopublish.py to
#      /opt/eeebot-techtree/ via scp (or direct install when running on the host).
#   3b. Patches the _BAKED_GENERATOR_SHA sentinel in the deployed viewer so the
#      published page footer shows the real git SHA instead of 'unknown' (issue
#      #101).  The patch uses `sed -i` on the target file before py_compile.
#   4. Runs python3 -m py_compile on both target files.
#   5. Triggers one autopublish run (systemctl start, non-blocking) so the new
#      generator is exercised immediately after deploy.
#
# Idempotent: safe to re-run after any repo update.
#
# Usage (from the operator's workstation):
#   scripts/deploy_generator.sh [--host eeepc-lan] [--dry-run]
#
# Usage (running directly on the eeepc host):
#   scripts/deploy_generator.sh [--dry-run]
#   (omit --host; the script detects it is already on the target and uses
#   local install instead of scp)
#
# Prerequisites:
#   - SSH access to the host (key-based; tested with --host before use)
#   - The deploy user must be able to sudo or have write access to /opt/eeebot-techtree
#   - eeebot-techtree-publish.service must already be installed and enabled
#     (see scripts/install_techtree_publish.sh)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPT_DIR=/opt/eeebot-techtree
VIEWER=techtree_viewer.py
AUTOPUBLISH=techtree_autopublish.py
PUBLISH_SERVICE=eeebot-techtree-publish.service

HOST=""
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --host=*) HOST="${arg#--host=}" ;;
    --host)   ;;  # handled by next iteration below
    --dry-run) DRY_RUN=1 ;;
  esac
done
# Re-parse for "--host VALUE" (two-token form)
_prev=""
for arg in "$@"; do
  if [[ "$_prev" == "--host" ]]; then
    HOST="$arg"
  fi
  _prev="$arg"
done

# --- git sha ----------------------------------------------------------------
# Capture the short sha of HEAD in this repo.  This is the version being
# deployed.  The sha is baked into the deployed techtree_viewer.py via
# `sed -i` (step 3b below) so the published page footer shows the real SHA
# instead of 'unknown' (issue #101).
GIT_SHA="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

echo "deploy_generator.sh: deploying generator sha=$GIT_SHA"
echo "  source: $ROOT/scripts/{$VIEWER,$AUTOPUBLISH}"
echo "  target: ${HOST:+(on $HOST) }$OPT_DIR/"

# --- helpers ----------------------------------------------------------------
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

# Run a command on the target host (or locally when HOST is unset/empty).
remote() {
  if [[ -n "$HOST" ]]; then
    ssh "$HOST" "$@"
  else
    bash -c "$*"
  fi
}

# Copy a local file to the target host.
remote_cp() {
  local src="$1"
  local dst="$2"
  if [[ -n "$HOST" ]]; then
    scp "$src" "${HOST}:${dst}"
  else
    install -o root -g root -m 0644 "$src" "$dst"
  fi
}

# --- preflight: check OPT_DIR exists on target -----------------------------
if [[ "$DRY_RUN" == "0" ]]; then
  if ! remote "test -d $OPT_DIR"; then
    echo "ERROR: $OPT_DIR does not exist on the target." >&2
    echo "       Run scripts/install_techtree_publish.sh first to create it." >&2
    exit 1
  fi
fi

# --- step 1: backup ---------------------------------------------------------
# Timestamped backup of whatever is currently at /opt.  Fail-soft: if the
# backup fails (the existing copy is already absent, or permissions deny it)
# we warn but do not abort -- the real guard is py_compile in step 3.
BACKUP_TS="$(date -u +%Y%m%dT%H%M%SZ)"
for f in "$VIEWER" "$AUTOPUBLISH"; do
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] backup $OPT_DIR/$f -> $OPT_DIR/${f%.py}.bak.$BACKUP_TS.py"
  else
    if remote "test -f $OPT_DIR/$f" 2>/dev/null; then
      if ! remote "cp $OPT_DIR/$f $OPT_DIR/${f%.py}.bak.$BACKUP_TS.py" 2>/dev/null; then
        echo "WARNING: could not back up $OPT_DIR/$f (non-fatal)" >&2
      fi
    fi
  fi
done

# --- step 2: copy -----------------------------------------------------------
for f in "$VIEWER" "$AUTOPUBLISH"; do
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] copy $ROOT/scripts/$f -> $OPT_DIR/$f"
  else
    remote_cp "$ROOT/scripts/$f" "$OPT_DIR/$f"
    # Keep the installed copy readable by the publish user (same mode as
    # install_techtree_publish.sh).
    remote "chmod 0644 $OPT_DIR/$f" 2>/dev/null || true
  fi
done

# --- step 2b: bake generator sha into the deployed viewer (issue #101) ------
# techtree_viewer.py contains a sentinel line:
#   _BAKED_GENERATOR_SHA: str = ''
# We replace the empty string literal with the real short SHA so that
# _generator_sha() returns the actual deployed commit instead of falling back
# to `git rev-parse` (which returns 'unknown' in /opt because there is no git
# repo there).  The sed pattern is anchored to the sentinel variable name so
# it can never clobber unrelated string literals.
if [[ "$GIT_SHA" != "unknown" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] sed -i bake _BAKED_GENERATOR_SHA='$GIT_SHA' in $OPT_DIR/$VIEWER"
  else
    if ! remote "sed -i \"s/^_BAKED_GENERATOR_SHA: str = ''$/_BAKED_GENERATOR_SHA: str = '$GIT_SHA'/\" $OPT_DIR/$VIEWER"; then
      echo "WARNING: could not bake generator sha into $OPT_DIR/$VIEWER (non-fatal; footer will show 'unknown')" >&2
    else
      echo "deploy_generator.sh: baked generator sha=$GIT_SHA into $OPT_DIR/$VIEWER"
    fi
  fi
else
  echo "WARNING: could not determine git sha; footer will show 'unknown'" >&2
fi

# --- step 3: py_compile -----------------------------------------------------
# Syntax-check both files on the target Python before pronouncing success.
# A failed py_compile leaves the backup in place as the last known-good copy.
if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] python3 -m py_compile $OPT_DIR/$VIEWER $OPT_DIR/$AUTOPUBLISH"
else
  if ! remote "python3 -m py_compile $OPT_DIR/$VIEWER $OPT_DIR/$AUTOPUBLISH"; then
    echo "ERROR: py_compile failed on the target -- deploy aborted." >&2
    echo "       Prior backup: $OPT_DIR/<name>.bak.$BACKUP_TS.py" >&2
    exit 1
  fi
  echo "deploy_generator.sh: py_compile OK"
fi

# --- step 4: trigger one publish run ----------------------------------------
# systemctl start on a oneshot service is non-blocking when the service is not
# currently running.  Idempotent: a service that is already running (from a
# prior bridge OnSuccess= trigger) is a no-op start.
if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] systemctl start $PUBLISH_SERVICE  (on ${HOST:-localhost})"
else
  if remote "systemctl is-active --quiet $PUBLISH_SERVICE" 2>/dev/null; then
    echo "deploy_generator.sh: $PUBLISH_SERVICE already active; will publish on next bridge cycle"
  else
    if remote "systemctl start $PUBLISH_SERVICE" 2>/dev/null; then
      echo "deploy_generator.sh: triggered $PUBLISH_SERVICE"
    else
      echo "WARNING: could not start $PUBLISH_SERVICE (non-fatal; it will run on the next bridge OnSuccess= trigger)" >&2
    fi
  fi
fi

# --- summary ----------------------------------------------------------------
echo
echo "deploy_generator.sh: done"
echo "  generator sha: $GIT_SHA"
echo "  the published footer will show generator sha=$GIT_SHA after the next publish run."
if [[ "$DRY_RUN" == "1" ]]; then
  echo
  echo "[dry-run] no changes were made."
fi
