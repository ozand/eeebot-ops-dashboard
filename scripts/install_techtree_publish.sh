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
# No STATE_DIR here: /var/lib/eeebot-techtree is created and owned by
# systemd itself via StateDirectory= in the unit (see step 3 below).
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

# 3. The publisher's own state dir (digest + last-publish timestamp) is
#    NOT created here: eeebot-techtree-publish.service declares
#    StateDirectory=eeebot-techtree, so systemd itself creates
#    /var/lib/eeebot-techtree (mode 0700, owned by the service's User=/
#    Group=) the first time the unit starts, and re-creates it if it's ever
#    removed. Pre-creating it here would only risk drifting out of sync
#    with the unit's own StateDirectoryMode=/ownership.

# 4. Service unit + bridge drop-in, then reload so systemd picks them up.
run mkdir -p "$DROPIN_DIR"
run install -o root -g root -m 0644 "$ROOT/systemd/eeebot-techtree-publish.service" "$UNIT_DIR/eeebot-techtree-publish.service"
run install -o root -g root -m 0644 \
  "$ROOT/systemd/drop-ins/eeepc-self-evolving-subagent-bridge.service.d/20-techtree-publish.conf" \
  "$DROPIN_DIR/20-techtree-publish.conf"
run systemctl daemon-reload

# 5. Verify the trigger this whole publisher depends on is actually live,
#    instead of just asserting it (issue #27 review, blocker B8): this
#    repo's other units are `systemctl --user`, so nothing here guarantees
#    that eeepc-self-evolving-subagent-bridge.service exists at all, or
#    that it is a system-scope unit our system-scope drop-in (step 4) can
#    even attach to -- an absent or --user-only bridge unit would leave the
#    drop-in permanently inert with no error anywhere.
#    Skipped entirely under --dry-run (issue #27 review round 3, note N6):
#    step 4 above (drop-in install + daemon-reload) is itself skipped on a
#    dry run, so systemd could never see the trigger as live regardless of
#    the host's real state -- running this check on a dry run would print
#    an alarming "trigger not live" WARNING on every first-time --dry-run
#    invocation, even on a host where a real (non-dry) run would succeed.
BRIDGE_UNIT=eeepc-self-evolving-subagent-bridge.service
TRIGGER_LIVE=0
if [[ "$DRY_RUN" == "0" ]] && systemctl cat "$BRIDGE_UNIT" >/dev/null 2>&1; then
  ONSUCCESS_VALUE="$(systemctl show -p OnSuccess --value "$BRIDGE_UNIT" 2>/dev/null || true)"
  case " $ONSUCCESS_VALUE " in
    *" eeebot-techtree-publish.service "*) TRIGGER_LIVE=1 ;;
  esac
fi

# 6. Deliberately NOT done here: creating, requesting, or templating the
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
EOF

if [[ "$DRY_RUN" == "1" ]]; then
  cat <<EOF

[dry-run] Trigger-live check skipped: step 4 (drop-in install +
daemon-reload) was not actually performed on a dry run, so this check
cannot say anything meaningful about it. Re-run without --dry-run, then
check with:
  systemctl cat $BRIDGE_UNIT
  systemctl show -p OnSuccess $BRIDGE_UNIT
EOF
elif [[ "$TRIGGER_LIVE" == "1" ]]; then
  cat <<EOF

Once that file exists, eeebot-techtree-publish.service will run
automatically the next time eeepc-self-evolving-subagent-bridge.service
completes a cycle -- no further action needed here.
EOF
else
  cat <<EOF

WARNING: could not confirm the publish trigger is actually live -- do NOT
assume publishing will happen automatically. Checked for a system-scope
$BRIDGE_UNIT with OnSuccess= listing eeebot-techtree-publish.service, and
did not find it. Either:
  - $BRIDGE_UNIT does not exist as a system-scope unit on this host
    (it may only exist as a \`systemctl --user\` unit, which this
    system-scope drop-in cannot attach to at all), or
  - it exists but its OnSuccess= does not list
    eeebot-techtree-publish.service (the drop-in in $DROPIN_DIR may not
    have been picked up, or the bridge unit was reinstalled after it).
Check by hand with:
  systemctl cat $BRIDGE_UNIT
  systemctl show -p OnSuccess $BRIDGE_UNIT
Publishing will not happen automatically until this is resolved.
EOF
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo
  echo "[dry-run] no changes were made."
fi
