#!/bin/sh
# Sync the reviewed generator pair from repository master before publishing.
# Install the drop-in's ExecStartPre with a leading '-' (and '+' for root) so
# sync failure is logged but does not block publish. Both files are downloaded
# and compiled before either existing /opt copy is replaced.
set -eu

DEST=/opt/eeebot-techtree
RAW_BASE=https://raw.githubusercontent.com/ozand/eeebot-ops-dashboard/master/scripts
RUN_SUFFIX=".sync.$$"
VIEWER_TMP="$DEST/.techtree_viewer.py$RUN_SUFFIX"
AUTOPUBLISH_TMP="$DEST/.techtree_autopublish.py$RUN_SUFFIX"
VIEWER_BACKUP=
AUTOPUBLISH_BACKUP=
VIEWER_MOVED=0
AUTOPUBLISH_MOVED=0

cleanup() {
    rm -f "$VIEWER_TMP" "$AUTOPUBLISH_TMP"
}
rollback() {
    if [ "$AUTOPUBLISH_MOVED" -eq 1 ]; then
        if [ -n "$AUTOPUBLISH_BACKUP" ]; then
            mv -f "$AUTOPUBLISH_BACKUP" "$DEST/techtree_autopublish.py"
        else
            rm -f "$DEST/techtree_autopublish.py"
        fi
    fi
    if [ "$VIEWER_MOVED" -eq 1 ]; then
        if [ -n "$VIEWER_BACKUP" ]; then
            mv -f "$VIEWER_BACKUP" "$DEST/techtree_viewer.py"
        else
            rm -f "$DEST/techtree_viewer.py"
        fi
    fi
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

mkdir -p "$DEST"

fetch_one() {
    name=$1
    tmp=$2
    if ! curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
        "$RAW_BASE/$name" -o "$tmp"; then
        echo "techtree sync: download failed: $name" >&2
        return 1
    fi
    if ! python3 -m py_compile "$tmp"; then
        echo "techtree sync: compile failed: $name" >&2
        return 1
    fi
}

# Preflight both downloads and compiles before touching either installed file.
fetch_one techtree_viewer.py "$VIEWER_TMP"
fetch_one techtree_autopublish.py "$AUTOPUBLISH_TMP"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
if [ -e "$DEST/techtree_viewer.py" ]; then
    VIEWER_BACKUP="$DEST/techtree_viewer.py.bak.$stamp"
    cp -p "$DEST/techtree_viewer.py" "$VIEWER_BACKUP"
fi
if [ -e "$DEST/techtree_autopublish.py" ]; then
    AUTOPUBLISH_BACKUP="$DEST/techtree_autopublish.py.bak.$stamp"
    cp -p "$DEST/techtree_autopublish.py" "$AUTOPUBLISH_BACKUP"
fi

# Replace both atomically; if the second move fails, restore the first file.
if ! mv -f "$VIEWER_TMP" "$DEST/techtree_viewer.py"; then
    echo "techtree sync: replace failed: techtree_viewer.py" >&2
    exit 1
fi
VIEWER_MOVED=1
if ! mv -f "$AUTOPUBLISH_TMP" "$DEST/techtree_autopublish.py"; then
    echo "techtree sync: replace failed: techtree_autopublish.py" >&2
    exit 1
fi
AUTOPUBLISH_MOVED=1
rm -f "$DEST/__pycache__/techtree_viewer.cpython-*.pyc" \
      "$DEST/__pycache__/techtree_autopublish.cpython-*.pyc"
echo "techtree sync: installed techtree_viewer.py and techtree_autopublish.py"
