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
MANIFEST=${SYNC_MANIFEST:-$DEST/sync-manifest.txt}
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

mkdir -p "$DEST" "$TMP_ROOT"
[ -r "$MANIFEST" ] || { echo "techtree sync: manifest missing: $MANIFEST" >&2; exit 1; }

index=0
while IFS= read -r relative || [ -n "$relative" ]; do
    case "$relative" in
        ''|'#'*) continue ;;
        /*|*..*) echo "techtree sync: unsafe manifest path: $relative" >&2; exit 1 ;;
    esac
    tmp="$TMP_ROOT/$index"
    if ! curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
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
