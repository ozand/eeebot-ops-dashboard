#!/usr/bin/env python3
"""Autopublisher for the tech-tree page, run on the `eeepc` authority host
itself (issue #27).

Triggered by the `eeepc-self-evolving-subagent-bridge.service` OnSuccess=
drop-in (see systemd/drop-ins/), i.e. once per completed bridge iteration --
roughly every 3.5 minutes, including idle no-demand cycles. Publishing on
every one of those would mean ~410 published commits/day, so this gates on
a digest over the tree-shaped sources only (see compute_tree_digest below)
plus a staleness floor, and only calls `gh` when something is actually
worth publishing.

Imports and reuses techtree_viewer (read_local_state / render_page /
publish_to_pages) rather than reimplementing any of it -- this file is only
the gate + state-file bookkeeping around that.

Usage:
    python scripts/techtree_autopublish.py [--state-root PATH] [--state-dir PATH]
        [--staleness-floor-hours N] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# techtree_viewer.py is installed as a sibling file on the host (see
# scripts/install_techtree_publish.sh), not necessarily inside a `scripts`
# package -- so import it via this file's own directory rather than
# assuming a package layout. This works identically in the repo (both
# files live in scripts/) and on the host (both installed flat under
# /opt/eeebot-techtree/).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import techtree_viewer as tv  # noqa: E402

DEFAULT_STATE_DIR = '/var/lib/eeebot-techtree'
STATE_FILENAME = 'publish_state.json'

# The tree-shaped sources only (issue #27 design comment). Deliberately
# EXCLUDES scorecard/latest.json and the ledger tail: both change on nearly
# every bridge cycle (loop metrics move constantly), so folding them into
# the publish gate would mean a fresh gh-pages commit on almost every one
# of the ~410 cycles/day, for numbers that have nothing to do with whether
# the tech tree itself actually changed.
TREE_DIGEST_SOURCES = (
    'evolution/tree.json',
    'tech_tree/portfolio.json',
    'hypotheses/lifecycle.json',
)

DEFAULT_STALENESS_FLOOR_HOURS = 6.0


def compute_tree_digest(state_root: Path) -> str:
    """SHA-256 over the raw bytes of TREE_DIGEST_SOURCES only, in a fixed
    order, each length-delimited by a NUL so an absent file cannot be
    confused with a present-but-empty one. A missing file hashes as a
    distinct sentinel rather than being skipped, so "the file appeared" and
    "the file disappeared" both count as a change."""
    hasher = hashlib.sha256()
    for relpath in TREE_DIGEST_SOURCES:
        path = state_root / relpath
        try:
            hasher.update(path.read_bytes())
        except OSError:
            hasher.update(b'<missing>')
        hasher.update(b'\x00')
    return hasher.hexdigest()


def load_publish_state(state_dir: Path) -> dict[str, Any]:
    """Read back {"digest", "published_at"} from the last successful
    publish. Any read/parse problem (including a half-written file left by
    an interrupted run, before the atomic-replace in save_publish_state
    existed to prevent that) is treated as "never published" rather than
    raised -- one extra publish is cheap; a wedged publisher is not."""
    path = state_dir / STATE_FILENAME
    try:
        with path.open('r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict) and 'digest' in data and 'published_at' in data:
            return data
    except Exception:  # noqa: BLE001
        pass
    return {'digest': None, 'published_at': None}


def save_publish_state(state_dir: Path, digest: str, published_at: float) -> None:
    """Record the digest + publish time atomically: write to a temp file in
    the same directory, then os.replace (issue #27). os.replace is atomic
    on both POSIX and Windows, so a process killed mid-write can never
    leave a corrupt/partial state file that would wedge publishing forever
    -- worst case the temp file is orphaned and the next run just re-reads
    the last good state (or "never published", see load_publish_state)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = state_dir / f'.{STATE_FILENAME}.tmp{os.getpid()}'
    payload = {'digest': digest, 'published_at': published_at}
    with tmp_path.open('w', encoding='utf-8') as fh:
        json.dump(payload, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, state_dir / STATE_FILENAME)


def should_publish(
    current_digest: str,
    state: dict[str, Any],
    staleness_floor_seconds: float,
    now: float,
) -> tuple[bool, str]:
    """The publish gate (issue #27): publish if EITHER the tree digest
    changed, OR the last successful publish is older than the staleness
    floor -- the floor exists so metrics deliberately excluded from the
    digest (scorecard, ledger) cannot drift on the published page forever
    while the tree itself sits quiet."""
    prev_digest = state.get('digest')
    prev_published_at = state.get('published_at')

    if prev_digest != current_digest:
        return True, 'tree digest changed'
    if not isinstance(prev_published_at, (int, float)):
        return True, 'no prior successful publish recorded'
    age = now - prev_published_at
    if age >= staleness_floor_seconds:
        return True, f'staleness floor exceeded ({age:.0f}s >= {staleness_floor_seconds:.0f}s)'
    return False, 'digest unchanged and within staleness floor'


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--state-root', default=tv.STATE_ROOT,
        help=f'authority-host state root to read (default: {tv.STATE_ROOT})',
    )
    parser.add_argument(
        '--state-dir', default=DEFAULT_STATE_DIR,
        help=f'directory holding this publisher\'s own digest/timestamp state (default: {DEFAULT_STATE_DIR})',
    )
    parser.add_argument(
        '--staleness-floor-hours', type=float, default=DEFAULT_STALENESS_FLOOR_HOURS,
        help=f'publish even on an unchanged digest once the last publish is older than this many hours (default: {DEFAULT_STALENESS_FLOOR_HOURS})',
    )
    parser.add_argument(
        '--host-label', default='eeepc',
        help='host label shown in the rendered page footer (default: eeepc)',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='compute the digest and render the page, print the publish decision, and make no gh API call',
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    state_root = Path(args.state_root)
    state_dir = Path(args.state_dir)
    staleness_floor_seconds = args.staleness_floor_hours * 3600.0

    data = tv.read_local_state(str(state_root))
    digest = compute_tree_digest(state_root)
    state = load_publish_state(state_dir)
    now = time.time()
    publish, reason = should_publish(digest, state, staleness_floor_seconds, now)

    if args.dry_run:
        html_out = tv.render_page(data, args.host_label)
        verdict = 'WOULD PUBLISH' if publish else 'would NOT publish'
        print(f'[dry-run] {verdict}: {reason}')
        print(f'[dry-run] digest={digest} rendered_bytes={len(html_out)}')
        if data.get('_error'):
            print(f'[dry-run] state read note: {data["_error"]}')
        return 0

    if not publish:
        # Deliberately silent (issue #27): this runs after every bridge
        # cycle, roughly every 3.5 minutes including idle ones, so logging
        # on the no-op happy path would bury the journal in noise.
        return 0

    html_out = tv.render_page(data, args.host_label)

    if not os.environ.get('GH_TOKEN'):
        print(
            'techtree-autopublish: GH_TOKEN is not set -- create '
            '/etc/eeepc-agent/techtree-publish.env (root-owned, 0600) with a '
            'GH_TOKEN=<token> line',
            file=sys.stderr,
        )
        return 1

    # publish_to_pages already returns 1 (rather than raising) on any API
    # failure and never writes anything on that path -- so a failed publish
    # here simply skips save_publish_state below and leaves gh-pages as it
    # was, ready to retry next cycle.
    rc = tv.publish_to_pages(html_out)
    if rc != 0:
        print(f'techtree-autopublish: publish failed ({reason}); previous page left untouched', file=sys.stderr)
        return 1

    print(f'techtree-autopublish: published ({reason})')
    save_publish_state(state_dir, digest, now)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
