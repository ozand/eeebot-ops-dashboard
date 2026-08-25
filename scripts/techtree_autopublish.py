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
#
# Keyed explicitly by the tv.read_local_state() dict key that backs each
# file (issue #27 review round 3, blocker BL1) -- NOT two parallel tuples
# paired by list index. A previous version of this file did exactly that
# via a separate _TREE_SOURCE_KEYS tuple with a comment claiming it was
# "in the same order"; pairing by position is exactly the kind of thing
# that silently breaks the moment either collection is reordered on its
# own, so it is a single dict instead.
_TREE_SOURCE_FILES: dict[str, str] = {
    'evolution_tree': 'evolution/tree.json',
    'portfolio': 'tech_tree/portfolio.json',
    'hypotheses': 'hypotheses/lifecycle.json',
}
TREE_DIGEST_SOURCES = tuple(_TREE_SOURCE_FILES.values())

# Issue #56: the cycle ledger is not a tree source, but the Cycle Feed
# renders from it -- a failed/gate-blocked/rejected cycle updates ONLY the
# ledger, and without it in the digest those cycles stay unpublished until
# the next successful integration. Hash the tail (the page renders at most
# the last 50 cycles, so the tail is sufficient and bounded).
LEDGER_DIGEST_PATH = 'ledger/cycles.jsonl'
LEDGER_DIGEST_TAIL_LINES = 50

DEFAULT_STALENESS_FLOOR_HOURS = 6.0

# A floor on the floor (issue #27 review round 4, item A). The torn/
# unreadable-source guard below (_unreadable_tree_source) is right to
# refuse publishing a blank/broken page over a good one -- but refusing
# FOREVER once a source goes bad is not the right default either. This is
# reachable, not theoretical: evolution_tree.py, tech_tree.py, and
# hypothesis_backlog.py all write their state files with plain
# Path.write_text() (truncate-then-write), so a SIGKILL between the
# truncate and the write -- plausible on a 2 GB eeepc host under memory
# pressure -- leaves a permanent 0-byte file. The guard then refuses on
# every single firing after that, forever, and the staleness floor cannot
# rescue it because the guard is applied AFTER should_publish and
# overrides it. The public page freezes at its last good version with no
# signal anywhere except ~410 refusal lines/day in the journal. The same
# is true of an EACCES source: stat() still succeeds, only the read fails.
#
# So: once a refusal streak has persisted beyond this many multiples of
# the staleness floor, stop refusing and publish the fail-soft "source
# unavailable" page instead (render_page already fails soft per source --
# this reuses that, it does not add a second rendering path). Turns a
# silent freeze into an honest, visible degraded page. 2x is a deliberately
# generous grace period: it must clearly exceed one staleness-floor cycle
# (which can itself legitimately trigger a publish attempt) before
# assuming the source is stuck rather than just mid-write.
REFUSAL_FREEZE_MULTIPLE = 2.0


def _unreadable_tree_source(data: dict[str, Any], state_root: Path) -> str | None:
    """Detects a tree source file that is PRESENT on disk but failed to
    parse into a usable shape (issue #27 review round 3, blocker BL1).

    compute_tree_digest hashes RAW BYTES of the three tree-shaped source
    files, so a source caught mid-write by this script's own loop (the
    trigger fires the instant the loop finishes writing, so this is a live
    race, not a corner case) still changes the digest -- but
    tv.read_local_state's json.load on that same half-written file fails
    and the corresponding key comes back None. Publishing in that state
    would replace a good public page with an all-panels-unavailable one,
    and (absent this check) record the bad digest, wedging the page broken
    until the tree next changes for real or the staleness floor fires.

    Deliberately does NOT refuse when a source file is simply ABSENT.
    tv.read_local_state's read_json returns None indistinguishably for
    "file does not exist", "file exists but failed to parse", EACCES, and
    "path is a directory" -- but only the parse-failure/unreadable cases
    are what this guard exists for. hypotheses/lifecycle.json is not
    created until the first hypothesis candidate exists, and
    evolution/tree.json is not created until the first node is recorded,
    so an absent file is a normal, permanent state on a fresh host, a
    rebuilt state tree, or a pruned state dir -- refusing on it would mean
    should_publish's "no prior successful publish recorded" keeps
    returning True forever, this guard fires on every single bridge cycle
    (~410/day), and the page never publishes at all, which is worse than
    the bug this guard fixes. compute_tree_digest already treats a missing
    file as a distinct-but-valid sentinel for exactly this reason, and
    render_page is designed to fail soft with a "source unavailable" panel
    per source.

    Also refuses when a present file parses to something other than a
    dict (e.g. `[]`, `"oops"`, `5`, or literal `null`) -- render_page does
    not crash on those, but they are not a usable tree/portfolio/hypotheses
    shape either (issue #27 review round 3, note N4).

    Returns a human-readable reason, or None if every source either parsed
    to a dict or is legitimately absent.
    """
    if data.get('_error'):
        return f"state read error: {data['_error']}"
    for key, relpath in _TREE_SOURCE_FILES.items():
        if isinstance(data.get(key), dict):
            continue
        try:
            exists = (state_root / relpath).exists()
        except OSError:
            # issue #29: PermissionError/OSError when probing paths where a parent
            # directory denies search (+x) or read (+r) to the unprivileged service user.
            # Treat as present but unreadable to report problem and refuse degraded publish.
            return f'{key} could not be parsed (present but unreadable or wrong-shape: {relpath})'
        if not exists:
            continue  # legitimately absent -- not this guard's concern
        return f'{key} could not be parsed (present but unreadable or wrong-shape: {relpath})'
    return None


def compute_tree_digest(state_root: Path) -> str:
    """SHA-256 over the raw bytes of TREE_DIGEST_SOURCES only, in a fixed
    order, each length-delimited by a NUL so an absent file cannot be
    confused with a present-but-empty one. A missing file hashes as a
    distinct sentinel rather than being skipped, so "the file appeared" and
    "the file disappeared" both count as a change.

    Issue #56: additionally hashes the ledger tail (last
    LEDGER_DIGEST_TAIL_LINES lines) so any new cycle activity -- including
    failed/gate-blocked/rejected cycles that never touch the tree files --
    triggers a republish. The ledger component is fail-soft: an unreadable
    ledger hashes as a distinct sentinel (same pattern as missing tree
    sources) and never raises."""
    hasher = hashlib.sha256()
    for relpath in TREE_DIGEST_SOURCES:
        path = state_root / relpath
        try:
            hasher.update(path.read_bytes())
        except OSError:
            hasher.update(b'<missing>')
        hasher.update(b'\x00')
    ledger_path = state_root / LEDGER_DIGEST_PATH
    try:
        with open(ledger_path, 'rb') as fh:
            tail = fh.readlines()[-LEDGER_DIGEST_TAIL_LINES:]
        hasher.update(b''.join(tail))
    except OSError:
        hasher.update(b'<ledger-missing>')
    hasher.update(b'\x00')
    return hasher.hexdigest()


def load_publish_state(state_dir: Path) -> dict[str, Any]:
    """Read back {"digest", "published_at", "refusing_since"} from the last
    successful publish. Any read/parse problem (including a half-written
    file left by an interrupted run, before the atomic-replace in
    save_publish_state existed to prevent that) is treated as "never
    published" rather than raised -- one extra publish is cheap; a wedged
    publisher is not.

    "refusing_since" (issue #27 review round 4, item A) is the wall-clock
    time.time() at which the CURRENT source-refusal streak started, or
    None when there is no ongoing refusal. It is stored here rather than
    in a second state file so there is exactly one place tracking this
    publisher's state, and so a state_dir wipe/rebuild clears both
    consistently. A state file written before this field existed simply
    lacks the key -- setdefault below treats that the same as "no ongoing
    refusal", which is correct: there cannot have been one recorded by
    code that didn't know about this field yet."""
    path = state_dir / STATE_FILENAME
    try:
        with path.open('r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict) and 'digest' in data and 'published_at' in data:
            data.setdefault('refusing_since', None)
            return data
    except Exception:  # noqa: BLE001
        pass
    return {'digest': None, 'published_at': None, 'refusing_since': None}


def save_publish_state(
    state_dir: Path, digest: str | None, published_at: float | None,
    refusing_since: float | None = None,
) -> None:
    """Record the digest + publish time atomically: write to a temp file in
    the same directory, then os.replace (issue #27). os.replace is atomic
    on both POSIX and Windows, so a process killed mid-write can never
    leave a corrupt/partial state file that would wedge publishing forever
    -- worst case the temp file is orphaned and the next run just re-reads
    the last good state (or "never published", see load_publish_state).

    `refusing_since` defaults to None (issue #27 review round 4, item A):
    every call site on the successful-publish path omits it, which is what
    clears a previously-recorded refusal streak the moment a publish (fail
    -soft or otherwise) actually goes through. The one call site that is
    mid-refusal passes the streak's start time explicitly, and passes
    through the PRIOR digest/published_at unchanged so a still-refusing
    cycle never overwrites the last known-good publish record.

    The whole thing is wrapped in try/except OSError: a missing or
    unexpectedly read-only state_dir (e.g. StateDirectory= not applied, or
    applied to the wrong path) must fail loudly to the journal rather than
    silently (issue #27 review, blocker B4)."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = state_dir / f'.{STATE_FILENAME}.tmp{os.getpid()}'
        payload = {
            'digest': digest, 'published_at': published_at,
            'refusing_since': refusing_since,
        }
        with tmp_path.open('w', encoding='utf-8') as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, state_dir / STATE_FILENAME)
    except OSError as exc:
        print(
            f'techtree-autopublish: FAILED to save publish state to {state_dir} '
            f'({exc.__class__.__name__}: {exc}) -- the page just published '
            'successfully, but every future cycle will republish unnecessarily '
            'until this is fixed',
            file=sys.stderr,
        )


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
    while the tree itself sits quiet.

    The floor itself is computed from wall-clock time.time() (unlike the
    digest-change trigger above, which is event-driven off the loop and so
    never depends on any clock). A backward clock jump (NTP correction,
    manual reset) would otherwise make `age` negative and permanently
    smaller than the floor, disabling the floor forever (issue #27 review,
    blocker B7) -- so a negative age is treated the same as "stale"."""
    prev_digest = state.get('digest')
    prev_published_at = state.get('published_at')

    if prev_digest != current_digest:
        return True, 'tree digest changed'
    if not isinstance(prev_published_at, (int, float)):
        return True, 'no prior successful publish recorded'
    age = now - prev_published_at
    if age < 0:
        return True, f'clock moved backward since last publish ({age:.0f}s); treating as stale'
    if age >= staleness_floor_seconds:
        return True, f'staleness floor exceeded ({age:.0f}s >= {staleness_floor_seconds:.0f}s)'
    return False, 'digest unchanged and within staleness floor'


def _first_state_directory(value: str) -> str:
    """$STATE_DIRECTORY is colon-separated when a unit's StateDirectory=
    lists more than one name (issue #27 review round 4, item I; confirmed
    against man/systemd.exec.xml at v252: "these options take a
    whitespace-separated list of directory names ... if multiple
    directories are set, then in the environment variable the paths are
    concatenated with colon"). This unit's StateDirectory= sets exactly
    one name today, so os.environ.get('STATE_DIRECTORY', ...) currently
    always yields a single absolute path -- but if a second directory is
    ever added to the unit, the same code would silently start pointing
    Path() at "/var/lib/a:/var/lib/b", which is not a path that exists,
    rather than at either real directory. Take only the first
    colon-separated segment so that failure mode can't reoccur silently;
    a single-directory STATE_DIRECTORY (today's only case) is unaffected,
    since split(':', 1)[0] on a string with no colon is the whole string.

    Falls back to the caller's default when the first segment is empty (an
    unset variable, or a leading colon): '' would become Path('.') and write
    state into the working directory, which is worse than not honouring the
    variable at all. systemd cannot produce that -- StateDirectory= names
    cannot be empty -- but a hand-run shell can."""
    return value.split(':', 1)[0] or DEFAULT_STATE_DIR


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--state-root', default=tv.STATE_ROOT,
        help=f'authority-host state root to read (default: {tv.STATE_ROOT})',
    )
    parser.add_argument(
        # Default to $STATE_DIRECTORY when systemd has set it (it does so
        # automatically for any unit with StateDirectory=, expanded to the
        # absolute path under /var/lib) rather than hardcoding
        # DEFAULT_STATE_DIR -- otherwise the unit's StateDirectory= and this
        # default could silently diverge if either is ever changed without
        # the other (issue #27 review round 3, note N5). DEFAULT_STATE_DIR
        # remains the fallback for direct/manual invocation outside systemd.
        # _first_state_directory guards against a future second
        # StateDirectory= name making this colon-separated (item I).
        '--state-dir', default=_first_state_directory(
            os.environ.get('STATE_DIRECTORY', DEFAULT_STATE_DIR),
        ),
        help=f'directory holding this publisher\'s own digest/timestamp state (default: $STATE_DIRECTORY if set, else {DEFAULT_STATE_DIR})',
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


def _refusal_freeze_status(
    state: dict[str, Any], staleness_floor_seconds: float, now: float,
) -> tuple[float, float, float, bool]:
    """Given the persisted state and the current source-refusal streak
    (issue #27 review round 4, item A), return (refusing_since,
    refusal_age, freeze_limit, past_limit): the streak's start time, how
    long it has run, the bounded limit before this publisher gives up on
    refusing, and whether it has already exceeded that limit. If state has
    no recorded start for the streak yet (this is the first refusing
    cycle, or the state predates this field), treats it as having just
    started now (age 0) rather than inventing a past time -- run() is
    responsible for persisting this start time once it has decided this is
    in fact a fresh streak."""
    refusing_since = state.get('refusing_since')
    if not isinstance(refusing_since, (int, float)):
        refusing_since = now
    refusal_age = now - refusing_since
    freeze_limit = REFUSAL_FREEZE_MULTIPLE * staleness_floor_seconds
    # freeze_limit > 0 guard (issue #27 review round 4, finding 2): with
    # --staleness-floor-hours 0 the limit is 0 and `refusal_age >= 0` is true
    # on the FIRST refusing cycle, so the escape fires immediately and the
    # torn-source guard it is meant to bound is defeated outright -- a
    # degraded page published over a good one on the very first torn read.
    # Nothing shipped passes that flag, but a manual force-publish run would.
    # A non-positive limit means "no escape", which is the safe reading.
    past_limit = freeze_limit > 0 and refusal_age >= freeze_limit
    return refusing_since, refusal_age, freeze_limit, past_limit


def run(args: argparse.Namespace) -> int:
    state_root = Path(args.state_root)
    state_dir = Path(args.state_dir)
    staleness_floor_seconds = args.staleness_floor_hours * 3600.0

    data = tv.read_local_state(str(state_root))
    digest = compute_tree_digest(state_root)
    state = load_publish_state(state_dir)
    now = time.time()
    publish, reason = should_publish(digest, state, staleness_floor_seconds, now)

    source_problem = _unreadable_tree_source(data, state_root)

    if args.dry_run:
        html_out = tv.render_page(data, args.host_label)
        if publish and source_problem:
            _, refusal_age, freeze_limit, past_limit = _refusal_freeze_status(
                state, staleness_floor_seconds, now,
            )
            if past_limit:
                verdict = 'would PUBLISH the fail-soft "source unavailable" page'
                shown_reason = (
                    f'{source_problem} (refused for {refusal_age:.0f}s >= '
                    f'{freeze_limit:.0f}s freeze limit -- giving up on refusing)'
                )
            else:
                verdict = 'would REFUSE to publish'
                shown_reason = (
                    f'{source_problem} (refusing for {refusal_age:.0f}s of a '
                    f'{freeze_limit:.0f}s limit before falling back to the '
                    'fail-soft page)'
                )
        else:
            verdict = 'WOULD PUBLISH' if publish else 'would NOT publish'
            shown_reason = reason
        print(f'[dry-run] {verdict}: {shown_reason}')
        print(f'[dry-run] digest={digest} rendered_bytes={len(html_out)}')
        if data.get('_error'):
            # Detailed message (may include a host filesystem path) goes to
            # stderr only, never stdout/the page (issue #27 review, blocker
            # B2) -- this is an operator-facing dry-run log line, not the
            # published page, but keep the convention consistent regardless.
            print(f'[dry-run] state read note: {data["_error"]}', file=sys.stderr)
        return 0

    if not publish:
        # Deliberately silent (issue #27): this runs after every bridge
        # cycle, roughly every 3.5 minutes including idle ones, so logging
        # on the no-op happy path would bury the journal in noise.
        return 0

    if source_problem:
        # Blocker B1: a torn/unreadable tree source must never publish a
        # blank page over the good one, and must never have its bad digest
        # recorded -- next cycle re-reads the same (still torn, or by then
        # fixed) source and tries again.
        #
        # But not forever (issue #27 review round 4, item A): a source
        # that never recovers (permanent 0-byte file from a SIGKILL mid
        # write, a persistent EACCES) must not freeze the public page at
        # its last good version indefinitely with nothing but journal
        # noise as a signal. Once this refusal streak has run longer than
        # REFUSAL_FREEZE_MULTIPLE x the staleness floor, give up on
        # refusing and fall through to publish the fail-soft "source
        # unavailable" page below instead -- render_page already renders
        # each affected panel as unavailable from this same `data`, so
        # this is not a second rendering path, just choosing to use the
        # normal one instead of refusing.
        refusing_since, refusal_age, freeze_limit, past_limit = _refusal_freeze_status(
            state, staleness_floor_seconds, now,
        )

        if not past_limit:
            if not isinstance(state.get('refusing_since'), (int, float)):
                # First refusing cycle of a new streak: record when it
                # started, without touching the last known-good
                # digest/published_at (state.get(...) here is exactly
                # that last-good pair, or None/None if never published).
                save_publish_state(
                    state_dir, state.get('digest'), state.get('published_at'),
                    refusing_since=refusing_since,
                )
            print(
                f'techtree-autopublish: refusing to publish -- {source_problem}; '
                f'previous page and state left untouched (refusing for '
                f'{refusal_age:.0f}s of a {freeze_limit:.0f}s limit before '
                'falling back to a fail-soft publish)',
                file=sys.stderr,
            )
            return 1

        print(
            f'techtree-autopublish: source has been unreadable for '
            f'{refusal_age:.0f}s (>= {freeze_limit:.0f}s, '
            f'{REFUSAL_FREEZE_MULTIPLE:g}x the staleness floor) -- giving up '
            f'on refusing and publishing the fail-soft "source unavailable" '
            f'page instead of freezing forever ({source_problem})',
            file=sys.stderr,
        )
        # Fall through: publish below using the same (still-broken) data;
        # render_page fails soft per source already.

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
