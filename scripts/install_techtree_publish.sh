#!/usr/bin/env bash
# System-scope installer for the tech-tree autopublisher (issue #27).
#
# scripts/install_user_units.sh installs the existing eeebot-ops-dashboard-*
# services as `systemctl --user` units under $HOME/.config/systemd/user --
# that model has no way to create a dedicated system user or install a
# root-owned credential file the service user can never read. This
# publisher needs exactly that (a real system user, root-owned 0600
# EnvironmentFile, /opt + /etc/systemd/system), which requires root and
# system systemd. Rather than bend install_user_units.sh into something it
# was never built for, this is a separate script.
#
# Idempotent: safe to re-run after an update. Does NOT create, request, or
# template the credential file -- see the printed instructions at the end.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPT_DIR=/opt/eeebot-techtree
STATE_DIR=/var/lib/eeebot-techtree
UNIT_DIR=/etc/systemd/system
DROPIN_DIR="$UNIT_DIR/eeepc-self-evolving-subagent-bridge.service.d"
PUBLISH_USER=eeebot-publish
ENV_FILE=/etc/eeepc-agent/techtree-publish.env

DRY_RUN=0
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then
    DRY_RUN=1
  fi
done

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

if [[ "$DRY_RUN" == "0" && "$(id -u)" != "0" ]]; then
  echo "install_techtree_publish.sh must run as root (it creates a system user and writes to /opt and /etc/systemd/system)." >&2
  exit 1
fi

# 1. Dedicated, unprivileged system user -- no home, no login shell.
#    Idempotent: skip if it already exists.
if id -u "$PUBLISH_USER" >/dev/null 2>&1; then
  echo "user $PUBLISH_USER already exists, skipping useradd"
else
  run useradd --system --no-create-home --shell /usr/sbin/nologin "$PUBLISH_USER"
fi

# 2. Root-owned, publisher-read-only install of the two scripts under /opt.
#    0644: readable by eeebot-publish (and everyone else), writable only by root.
run mkdir -p "$OPT_DIR"
run install -o root -g root -m 0644 "$ROOT/scripts/techtree_viewer.py" "$OPT_DIR/techtree_viewer.py"
run install -o root -g root -m 0644 "$ROOT/scripts/techtree_autopublish.py" "$OPT_DIR/techtree_autopublish.py"

# 3. The publisher's own state dir (digest + last-publish timestamp).
#    Owned by the publisher; nothing else needs to touch it.
run mkdir -p "$STATE_DIR"
run chown "$PUBLISH_USER:$PUBLISH_USER" "$STATE_DIR"
run chmod 0700 "$STATE_DIR"

# 4. Service unit + bridge drop-in, then reload so systemd picks them up.
run mkdir -p "$DROPIN_DIR"
run install -o root -g root -m 0644 "$ROOT/systemd/eeebot-techtree-publish.service" "$UNIT_DIR/eeebot-techtree-publish.service"
run install -o root -g root -m 0644 \
  "$ROOT/systemd/drop-ins/eeepc-self-evolving-subagent-bridge.service.d/20-techtree-publish.conf" \
  "$DROPIN_DIR/20-techtree-publish.conf"
run systemctl daemon-reload

# 5. Deliberately NOT done here: creating, requesting, or templating the
# credential. It is created by hand by the operator, outside any script,
# and this installer never touches its value.
cat <<EOF

Next step (manual, by the operator): create $ENV_FILE
  - root-owned, mode 0600
  - containing exactly one line: GH_TOKEN=<your fine-grained token>
  - the token must be a fine-grained PAT scoped to THIS repository ONLY
    (ozand/eeebot-ops-dashboard) with "Contents: write" and nothing else
  - GitHub Pages is already enabled on this repo's gh-pages branch, so the
    token needs NO Pages permission

Example (run as root, fill in the real token yourself -- never paste it
into a script or commit it anywhere):
  install -o root -g root -m 0600 /dev/null $ENV_FILE
  \$EDITOR $ENV_FILE   # add: GH_TOKEN=...

Once that file exists, eeebot-techtree-publish.service will run
automatically the next time eeepc-self-evolving-subagent-bridge.service
completes a cycle -- no further action needed here.
EOF

if [[ "$DRY_RUN" == "1" ]]; then
  echo
  echo "[dry-run] no changes were made."
fi
