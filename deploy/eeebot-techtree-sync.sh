#!/bin/sh
# Sync the generator and vendored browser assets from repository master.
# Install the drop-in's ExecStartPre with a leading '-' (and '+' for root) so
# sync failure is logged but does not block publish. All downloads and checks
# finish before any installed file is replaced.
set -eu

DEST=/opt/eeebot-techtree
RAW_BASE=https://raw.githubusercontent.com/ozand/eeebot-ops-dashboard/master
# Permanent backups kept per installed file. The publish unit fires every few
# minutes and each run replaced every manifest file, so an unbounded keep-all
# policy reached 86 files / 11 MB in one directory (issue #155).
BACKUP_KEEP=3
# #210: the host's own copy of this file is not itself a manifest entry, so
# it can never update itself. Any master change that deletes or renames an
# entry (as #209 did for the d3 assets) permanently deadlocks every future
# sync: the stale local copy names a file master no longer has, and the
# all-or-nothing download rule aborts before the very update that would fix
# the list. LOCAL_MANIFEST is now only the fallback used when master is
# unreachable or returns something that is not a manifest; the manifest on
# master is fetched and preferred first, then written back to
# LOCAL_MANIFEST once a full sync using it has actually succeeded, so the
# fallback keeps healing itself instead of freezing at whatever it was
# initialized with.
LOCAL_MANIFEST=${SYNC_MANIFEST:-$DEST/sync-manifest.txt}
TMP_ROOT="$DEST/.sync-$$"
FILES="$TMP_ROOT/files"
MOVED_LIST="$TMP_ROOT/moved"

cleanup() {
    rm -rf "$TMP_ROOT"
}
rollback() {
    if [ -f "$MOVED_LIST" ]; then
        while IFS='|' read -r relative destination backup permanent_backup; do
            [ -n "$destination" ] || continue
            rm -f "$permanent_backup"
            if [ -n "$backup" ] && [ -e "$backup" ]; then
                mv -f "$backup" "$destination"
            else
                rm -f "$destination"
            fi
        done < "$MOVED_LIST"
    fi
}
prune_backups() {
    # Runs only after every install succeeded, so rollback has already had its
    # chance to use this run's backups. Stamps are UTC and zero-padded, so a
    # lexicographic sort is chronological.
    while IFS='|' read -r _relative destination _backup _permanent_backup; do
        [ -n "$destination" ] || continue
        total=0
        for candidate in "$destination".bak.*; do
            [ -e "$candidate" ] || continue
            total=$((total + 1))
        done
        [ "$total" -gt "$BACKUP_KEEP" ] || continue
        drop=$((total - BACKUP_KEEP))
        for candidate in $(printf '%s\n' "$destination".bak.* | sort); do
            [ "$drop" -gt 0 ] || break
            [ -e "$candidate" ] || continue
            rm -f "$candidate"
            drop=$((drop - 1))
        done
    done < "$MOVED_LIST"
}
on_exit() {
    rc=$?
    if [ "$rc" -ne 0 ]; then
        rollback
    fi
    cleanup
    exit "$rc"
}
trap on_exit 0
trap 'exit 1' HUP INT TERM

# A fetched manifest is only trusted if every non-blank, non-comment line
# is a path this script's own install loop below already knows how to
# handle (.py or .js). This is not a new, stricter rule invented for the
# fetch path: it is the exact same vocabulary the loop enforces on any
# manifest already, local or remote -- so a garbage response (an HTML error
# page, a JSON error body, an empty body) is rejected here instead of
# either aborting the whole sync or being fed unvalidated into the loop.
# A manifest edited on Windows, or served with CRLF, would otherwise turn every
# entry into "name.py<CR>" -- rejected here, and a hard 404 in the loop below.
cr=$(printf '\r')
manifest_looks_valid() {
    manifest_file="$1"
    [ -s "$manifest_file" ] || return 1
    saw_entry=0
    line=
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%"$cr"}
        case "$line" in
            ''|'#'*) continue ;;
            /*|*..*) return 1 ;;
            *.py|*.js) saw_entry=1 ;;
            *) return 1 ;;
        esac
    done < "$manifest_file"
    [ "$saw_entry" = 1 ]
}

mkdir -p "$DEST" "$TMP_ROOT"

# #210: prefer the manifest on master; fall back to the local copy only if
# the fetch fails or the fetched content is not a recognisable manifest.
# Either branch prints a distinct, visible journal line -- a silent
# fallback to a stale local copy would just be the original bug in a new
# wrapper, and the operator explicitly ruled that out.
# Shared curl options for both fetches. Bounded network waits: an
# unreachable GitHub must degrade to the local copy, not hang this
# ExecStartPre until the unit's TimeoutStartSec (300 s) kills the whole
# publish. --proto-redir keeps a redirect from downgrading to http; what is
# fetched here is installed as root and, for the manifest, kept as the
# offline fallback.
CURL_OPTS="--connect-timeout 10 --max-time 60 --proto-redir =https"
MANIFEST="$LOCAL_MANIFEST"
manifest_source=local
# shellcheck disable=SC2086  # CURL_OPTS is deliberately word-split
if curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 $CURL_OPTS \
    "$RAW_BASE/deploy/sync-manifest.txt" -o "$TMP_ROOT/manifest.remote"; then
    if manifest_looks_valid "$TMP_ROOT/manifest.remote"; then
        MANIFEST="$TMP_ROOT/manifest.remote"
        manifest_source=master
    else
        echo "techtree sync: manifest fetched from master is empty or unrecognisable, falling back to local copy: $LOCAL_MANIFEST (a stale local copy can still abort this run on a deleted entry -- #210)" >&2
    fi
else
    echo "techtree sync: manifest fetch from master failed, falling back to local copy: $LOCAL_MANIFEST (a stale local copy can still abort this run on a deleted entry -- #210)" >&2
fi
[ -r "$MANIFEST" ] || { echo "techtree sync: manifest missing: $MANIFEST" >&2; exit 1; }
echo "techtree sync: using $manifest_source manifest ($MANIFEST)"

# Trade recorded (#210 review): with master's manifest in use, a 404 on one
# of ITS entries (raw CDN still catching up on a file pushed in the same
# commit) fails this run with no retry against the local copy. That is a
# transient of one publish interval, and the local copy could not have
# helped -- it does not know the new file either. The deadlock this PR
# removes was permanent; this is not.
index=0
relative=
while IFS= read -r relative || [ -n "$relative" ]; do
    relative=${relative%"$cr"}
    case "$relative" in
        ''|'#'*) continue ;;
        /*|*..*) echo "techtree sync: unsafe manifest path: $relative" >&2; exit 1 ;;
    esac
    tmp="$TMP_ROOT/$index"
    # shellcheck disable=SC2086
    if ! curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 $CURL_OPTS \
        "$RAW_BASE/$relative" -o "$tmp"; then
        echo "techtree sync: download failed: $relative" >&2
        exit 1
    fi
    case "$relative" in
        *.py)
            if ! python3 -m py_compile "$tmp"; then
                echo "techtree sync: compile failed: $relative" >&2
                exit 1
            fi
            ;;
        *.js)
            [ -s "$tmp" ] || { echo "techtree sync: empty asset: $relative" >&2; exit 1; }
            ;;
        *) echo "techtree sync: unsupported manifest path: $relative" >&2; exit 1 ;;
    esac
    printf '%s|%s\n' "$relative" "$tmp" >> "$FILES"
    index=$((index + 1))
done < "$MANIFEST"

[ "$index" -gt 0 ] || { echo "techtree sync: empty manifest" >&2; exit 1; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
installed=0
while IFS='|' read -r relative tmp; do
    destination="$DEST/$relative"
    backup=
    permanent_backup=
    mkdir -p "$(dirname "$destination")"
    if [ -e "$destination" ]; then
        backup="$TMP_ROOT/backup-$installed"
        permanent_backup="$destination.bak.$stamp"
        cp -p "$destination" "$backup"
    fi
    if ! mv -f "$tmp" "$destination"; then
        echo "techtree sync: replace failed: $relative" >&2
        exit 1
    fi
    printf '%s|%s|%s|%s\n' "$relative" "$destination" "$backup" "$permanent_backup" >> "$MOVED_LIST"
    if [ -n "$backup" ]; then
        cp -p "$backup" "$permanent_backup"
    fi
    installed=$((installed + 1))
done < "$FILES"

if [ -f "$MOVED_LIST" ]; then
    prune_backups
fi

echo "techtree sync: installed $installed manifest file(s)"

# Self-heal: only after every named file actually installed successfully,
# and only when master's manifest is what got used, write it back over the
# local fallback copy. This is the step that stops a deleted/renamed entry
# from deadlocking every future run forever -- the fallback now reflects
# the last manifest that was proven to work, not whatever it was
# initialized with. Not fatal on its own: the files are already installed
# by this point, so a failure to update the fallback copy is logged, not
# rolled back.
# Atomic (tmp in the same directory tree, then mv) so a signal or ENOSPC
# mid-write cannot leave a truncated fallback -- the one file needed when
# GitHub is next unreachable. An explicit SYNC_MANIFEST override is an
# operator's pinned choice and is never overwritten.
if [ "$manifest_source" = "master" ]; then
    if [ -n "${SYNC_MANIFEST:-}" ]; then
        echo "techtree sync: SYNC_MANIFEST is set explicitly; not updating it from master ($LOCAL_MANIFEST)"
    elif cp "$MANIFEST" "$TMP_ROOT/manifest.new" && mv -f "$TMP_ROOT/manifest.new" "$LOCAL_MANIFEST"; then
        echo "techtree sync: local manifest copy updated from master ($LOCAL_MANIFEST)"
    else
        echo "techtree sync: WARNING: could not update local manifest copy at $LOCAL_MANIFEST" >&2
    fi
fi
