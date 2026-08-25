from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest

from scripts import techtree_autopublish as ap


def _write_state_root(root: Path, **overrides: object) -> None:
    """Populate a minimal, valid state root with the five source files.
    `overrides` maps relpath -> raw text to write instead of the default."""
    defaults = {
        'evolution/tree.json': '{"current_sha": "a", "nodes": {}}',
        'tech_tree/portfolio.json': '{"current": null, "nodes": {}}',
        'hypotheses/lifecycle.json': '{"entries": {}}',
        'scorecard/latest.json': '{"computed_at_utc": "2026-08-18T00:00:00Z"}',
        'ledger/cycles.jsonl': '',
    }
    defaults.update(overrides)
    for relpath, content in defaults.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')


# --- digest scope (acceptance test 2) --------------------------------------

def test_digest_ignores_scorecard_but_not_ledger(tmp_path: Path) -> None:
    # Issue #56: scorecard stays outside the digest; the ledger tail is now
    # INSIDE it (failed cycles must republish).
    root = tmp_path / 'state'
    _write_state_root(root)
    digest_before = ap.compute_tree_digest(root)

    (root / 'scorecard/latest.json').write_text('{"computed_at_utc": "2026-08-19T00:00:00Z"}', encoding='utf-8')
    digest_scorecard = ap.compute_tree_digest(root)
    assert digest_scorecard == digest_before

    (root / 'ledger/cycles.jsonl').write_text('{"phase": "tech_tree"}\n', encoding='utf-8')
    digest_ledger = ap.compute_tree_digest(root)
    assert digest_ledger != digest_before


def test_digest_changes_when_evolution_tree_changes(tmp_path: Path) -> None:
    root = tmp_path / 'state'
    _write_state_root(root)
    digest_before = ap.compute_tree_digest(root)

    (root / 'evolution/tree.json').write_text('{"current_sha": "b", "nodes": {}}', encoding='utf-8')

    digest_after = ap.compute_tree_digest(root)
    assert digest_after != digest_before


def test_digest_changes_when_portfolio_or_hypotheses_change(tmp_path: Path) -> None:
    root = tmp_path / 'state'
    _write_state_root(root)
    base = ap.compute_tree_digest(root)

    (root / 'tech_tree/portfolio.json').write_text('{"current": "x", "nodes": {}}', encoding='utf-8')
    assert ap.compute_tree_digest(root) != base

    _write_state_root(root)  # reset portfolio back
    (root / 'hypotheses/lifecycle.json').write_text('{"entries": {"h1": {}}}', encoding='utf-8')
    assert ap.compute_tree_digest(root) != base


# --- should_publish gate (acceptance tests 2, 3) ----------------------------

def test_should_publish_on_digest_change() -> None:
    state = {'digest': 'old', 'published_at': 1000.0}
    publish, reason = ap.should_publish('new', state, staleness_floor_seconds=3600, now=1100.0)
    assert publish is True
    assert 'digest' in reason


def test_should_not_publish_when_digest_unchanged_and_fresh() -> None:
    state = {'digest': 'same', 'published_at': 1000.0}
    publish, reason = ap.should_publish('same', state, staleness_floor_seconds=3600, now=1100.0)
    assert publish is False


def test_staleness_floor_triggers_publish_on_unchanged_digest() -> None:
    state = {'digest': 'same', 'published_at': 1000.0}
    floor = 3600.0
    publish, reason = ap.should_publish('same', state, staleness_floor_seconds=floor, now=1000.0 + floor + 1)
    assert publish is True
    assert 'staleness' in reason.lower()


def test_should_publish_when_never_published_before() -> None:
    state = {'digest': None, 'published_at': None}
    publish, reason = ap.should_publish('anything', state, staleness_floor_seconds=3600, now=1000.0)
    assert publish is True


# --- item I (issue #27 review round 4): $STATE_DIRECTORY may become -------
# --- colon-separated if a second directory name is ever added -------------

def test_first_state_directory_takes_single_path_unchanged() -> None:
    """Today's only real case: one StateDirectory= name, no colon."""
    assert ap._first_state_directory('/var/lib/eeebot-techtree') == '/var/lib/eeebot-techtree'


def test_first_state_directory_takes_first_segment_of_colon_joined_value() -> None:
    """man/systemd.exec.xml (v252): if a unit's StateDirectory= ever lists
    more than one name, $STATE_DIRECTORY becomes colon-separated. Must not
    silently become Path("/var/lib/a:/var/lib/b") -- take the first."""
    joined = '/var/lib/eeebot-techtree:/var/lib/something-else'
    assert ap._first_state_directory(joined) == '/var/lib/eeebot-techtree'


def test_parse_args_state_dir_defaults_to_first_segment_of_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('STATE_DIRECTORY', '/var/lib/eeebot-techtree:/var/lib/other')
    args = ap.parse_args([])
    assert args.state_dir == '/var/lib/eeebot-techtree'


# --- state file persistence (acceptance tests 4, 5) -------------------------

def test_save_and_load_publish_state_roundtrip(tmp_path: Path) -> None:
    state_dir = tmp_path / 'techtree-state'
    ap.save_publish_state(state_dir, digest='abc123', published_at=12345.0)

    loaded = ap.load_publish_state(state_dir)
    assert loaded == {'digest': 'abc123', 'published_at': 12345.0, 'refusing_since': None}


def test_load_publish_state_missing_file_reads_as_never_published(tmp_path: Path) -> None:
    state_dir = tmp_path / 'does-not-exist-yet'
    loaded = ap.load_publish_state(state_dir)
    assert loaded == {'digest': None, 'published_at': None, 'refusing_since': None}


def test_interrupted_write_does_not_corrupt_state_file(tmp_path: Path) -> None:
    """Simulates a process killed mid-write: a stray temp file (as
    save_publish_state would leave behind if os.replace never ran) must not
    corrupt reads of the real state file."""
    state_dir = tmp_path / 'techtree-state'
    ap.save_publish_state(state_dir, digest='good', published_at=500.0)

    # Simulate a second, interrupted write: a temp file appears but the
    # rename to the real filename never happens.
    stray = state_dir / f'.{ap.STATE_FILENAME}.tmp99999'
    stray.write_text('{"digest": "half-written', encoding='utf-8')  # deliberately truncated/invalid JSON

    loaded = ap.load_publish_state(state_dir)
    assert loaded == {'digest': 'good', 'published_at': 500.0, 'refusing_since': None}


def test_a_failed_publish_does_not_update_stored_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: 1)  # simulate API failure

    args = ap.parse_args([
        '--state-root', str(root),
        '--state-dir', str(state_dir),
    ])
    rc = ap.run(args)

    assert rc != 0
    assert ap.load_publish_state(state_dir) == {'digest': None, 'published_at': None, 'refusing_since': None}


def test_a_successful_publish_updates_stored_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    published: list[str] = []
    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: published.append(html_out) or 0)

    args = ap.parse_args([
        '--state-root', str(root),
        '--state-dir', str(state_dir),
    ])
    rc = ap.run(args)

    assert rc == 0
    assert len(published) == 1
    state = ap.load_publish_state(state_dir)
    assert state['digest'] == ap.compute_tree_digest(root)
    assert state['published_at'] is not None


def test_missing_credential_exits_nonzero_and_does_not_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    monkeypatch.delenv('GH_TOKEN', raising=False)
    called = []
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: called.append(html_out) or 0)

    args = ap.parse_args(['--state-root', str(root), '--state-dir', str(state_dir)])
    rc = ap.run(args)

    assert rc != 0
    assert called == []
    assert ap.load_publish_state(state_dir) == {'digest': None, 'published_at': None, 'refusing_since': None}


def test_no_change_no_stale_publishes_nothing_and_is_quiet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    digest = ap.compute_tree_digest(root)
    ap.save_publish_state(state_dir, digest=digest, published_at=__import__('time').time())

    called = []
    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: called.append(html_out) or 0)

    args = ap.parse_args(['--state-root', str(root), '--state-dir', str(state_dir), '--staleness-floor-hours', '6'])
    rc = ap.run(args)

    assert rc == 0
    assert called == []
    out = capsys.readouterr()
    assert out.out == ''
    assert out.err == ''


def test_dry_run_makes_no_publish_call_even_without_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    monkeypatch.delenv('GH_TOKEN', raising=False)
    called = []
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: called.append(html_out) or 0)

    args = ap.parse_args(['--state-root', str(root), '--state-dir', str(state_dir), '--dry-run'])
    rc = ap.run(args)

    assert rc == 0
    assert called == []
    out = capsys.readouterr()
    assert 'dry-run' in out.out
    assert 'WOULD PUBLISH' in out.out


# --- blocker B1: torn/unreadable source must never publish a blank page ----

def test_torn_evolution_tree_refuses_to_publish_and_does_not_save_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source caught mid-write (issue #27 review, B1): its raw bytes
    differ from before (so the digest changes and a naive gate would
    publish), but json.load on it fails, so read_local_state reports
    evolution_tree=None. The autopublisher must refuse -- return non-zero,
    never call publish_to_pages, and never record the bad digest.

    It IS allowed (issue #27 review round 4, item A) to record that a
    refusal streak has started -- refusing_since -- since that is what the
    freeze-on-a-persistent-refusal fix needs to eventually stop refusing.
    The invariant this test actually cares about is narrower than the
    original docstring/title suggest: the last known-good digest and
    published_at must survive untouched across a refusal, not that the
    state file is never written to at all."""
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    # Simulate: this digest/state pair was already published successfully.
    good_digest = ap.compute_tree_digest(root)
    ap.save_publish_state(state_dir, digest=good_digest, published_at=1000.0)

    # Now the loop's next write is caught half-done: different raw bytes
    # (so the digest changes) but not valid JSON (so it fails to parse).
    (root / 'evolution/tree.json').write_text('{"current_sha": "b", "nod', encoding='utf-8')

    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    called = []
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: called.append(html_out) or 0)

    args = ap.parse_args(['--state-root', str(root), '--state-dir', str(state_dir)])
    rc = ap.run(args)

    assert rc != 0
    assert called == []  # publish_to_pages must never be called
    state = ap.load_publish_state(state_dir)
    assert state['digest'] == good_digest
    assert state['published_at'] == 1000.0
    assert state['refusing_since'] is not None  # streak start recorded


def test_missing_tree_source_file_still_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ABSENT tree source file must NOT be treated the same as a torn
    one (issue #27 review round 3, blocker BL1). tv.read_local_state's
    read_json returns None indistinguishably for "file does not exist" and
    "file exists but failed to parse", but only the latter is what the
    torn-source guard is for. hypotheses/lifecycle.json and
    evolution/tree.json are not created until the first hypothesis
    candidate / node exists respectively, so an absent file is a normal,
    permanent state on a fresh host, a rebuilt state tree, or a pruned
    state dir. Refusing to publish here would wedge the publisher forever:
    should_publish keeps returning True ("no prior successful publish
    recorded") on every single bridge cycle, the guard would keep firing,
    and the page would never publish at all -- worse than the bug the
    guard exists to fix."""
    root = tmp_path / 'state'
    _write_state_root(root)
    (root / 'tech_tree/portfolio.json').unlink()
    state_dir = tmp_path / 'techtree-state'

    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    published: list[str] = []
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: published.append(html_out) or 0)

    args = ap.parse_args(['--state-root', str(root), '--state-dir', str(state_dir)])
    rc = ap.run(args)

    assert rc == 0
    assert len(published) == 1
    state = ap.load_publish_state(state_dir)
    assert state['digest'] == ap.compute_tree_digest(root)
    assert state['published_at'] is not None


def test_present_but_truncated_hypotheses_file_still_refuses_to_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The flip side of the missing-file case above (issue #27 review
    round 3, blocker BL1): a source file that IS present on disk but fails
    to parse must still refuse to publish, exactly like the existing torn
    evolution_tree case, but exercised against a different source file.

    A truncated hypotheses/lifecycle.json alone does not distinguish
    per-file keying from a cruder "some source is None" check, since that
    cruder check would also refuse here (issue #27 review round 4, item
    E) -- a single-file case like this cannot support a "keyed correctly
    per file" claim by itself. So this also makes portfolio.json ABSENT
    (not truncated) in the same run and asserts on the printed reason
    naming hypotheses specifically: that combination only passes when the
    guard (a) does not refuse merely because portfolio.json is missing,
    and (b) correctly attributes the refusal to hypotheses rather than to
    whichever source happens to come first."""
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    good_digest = ap.compute_tree_digest(root)
    ap.save_publish_state(state_dir, digest=good_digest, published_at=1000.0)

    # Present on disk, different raw bytes (digest changes), but not valid
    # JSON (parse fails) -- simulates a source caught mid-write.
    (root / 'hypotheses/lifecycle.json').write_text('{"entries": {"h1"', encoding='utf-8')
    # Legitimately absent, paired in the same run -- must NOT itself cause
    # a refusal, and must not be misattributed as the failing source.
    (root / 'tech_tree/portfolio.json').unlink()

    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    called = []
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: called.append(html_out) or 0)

    args = ap.parse_args(['--state-root', str(root), '--state-dir', str(state_dir)])
    rc = ap.run(args)

    assert rc != 0
    assert called == []
    state = ap.load_publish_state(state_dir)
    assert state['digest'] == good_digest
    assert state['published_at'] == 1000.0
    stderr = capsys.readouterr().err
    assert 'hypotheses' in stderr
    assert 'lifecycle.json' in stderr
    assert 'portfolio' not in stderr


# --- note N4: a present source that parses to a non-dict must also refuse --

def test_present_source_parsing_to_non_dict_refuses_to_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source file that IS present and IS valid JSON but parses to
    something other than a dict (here, a bare list) is not None, so the
    old `data[key] is None` check would have let it through even though
    render_page has no usable tree/portfolio/hypotheses shape to work with
    (issue #27 review round 3, note N4). Must refuse, same as a torn file."""
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    good_digest = ap.compute_tree_digest(root)
    ap.save_publish_state(state_dir, digest=good_digest, published_at=1000.0)

    (root / 'evolution/tree.json').write_text('[]', encoding='utf-8')

    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    called = []
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: called.append(html_out) or 0)

    args = ap.parse_args(['--state-root', str(root), '--state-dir', str(state_dir)])
    rc = ap.run(args)

    assert rc != 0
    assert called == []
    state = ap.load_publish_state(state_dir)
    assert state['digest'] == good_digest
    assert state['published_at'] == 1000.0


# --- item A (issue #27 review round 4): a persistent refusal must not -----
# --- freeze the page forever -----------------------------------------------

def test_refusal_then_recovery_clears_refusing_since(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-trips an actual refuse-then-recover cycle through run() twice.
    First cycle: the source is torn, so run() must refuse and record when
    the refusal streak started (without touching the last known-good
    digest/published_at). Second cycle: the source has recovered (parses
    again), so run() must publish normally and clear refusing_since --
    the guard must not keep treating a since-recovered source as if it
    were still broken."""
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    good_digest = ap.compute_tree_digest(root)
    ap.save_publish_state(state_dir, digest=good_digest, published_at=1000.0)

    (root / 'evolution/tree.json').write_text('{"current_sha": "b", "nod', encoding='utf-8')  # torn

    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    called: list[str] = []
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: called.append(html_out) or 0)

    args = ap.parse_args(['--state-root', str(root), '--state-dir', str(state_dir)])

    rc = ap.run(args)  # cycle 1: still torn -- must refuse
    assert rc != 0
    assert called == []
    state = ap.load_publish_state(state_dir)
    assert state['digest'] == good_digest  # last known-good digest untouched
    assert state['published_at'] == 1000.0
    assert state['refusing_since'] is not None  # streak start recorded

    (root / 'evolution/tree.json').write_text('{"current_sha": "b", "nodes": {}}', encoding='utf-8')  # recovered

    rc = ap.run(args)  # cycle 2: source parses again -- must publish
    assert rc == 0
    assert len(called) == 1
    state = ap.load_publish_state(state_dir)
    assert state['digest'] == ap.compute_tree_digest(root)
    assert state['published_at'] is not None
    assert state['refusing_since'] is None  # streak cleared by the recovery


def test_refusal_past_freeze_limit_publishes_fail_soft_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Once a refusal streak has persisted beyond REFUSAL_FREEZE_MULTIPLE x
    the staleness floor, run() must give up on refusing -- even though the
    source is STILL torn -- and publish the fail-soft "source unavailable"
    page instead of freezing the public page forever. Uses a tiny staleness
    floor and a refusing_since far in the past so the limit is already
    exceeded without any real waiting."""
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    good_digest = ap.compute_tree_digest(root)
    long_ago = time.time() - 999_999.0
    ap.save_publish_state(
        state_dir, digest=good_digest, published_at=1000.0, refusing_since=long_ago,
    )

    (root / 'evolution/tree.json').write_text('{"current_sha": "b", "nod', encoding='utf-8')  # still torn

    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    published: list[str] = []
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: published.append(html_out) or 0)

    args = ap.parse_args([
        '--state-root', str(root), '--state-dir', str(state_dir),
        '--staleness-floor-hours', '0.0001',  # ~0.36s floor -> ~0.72s freeze limit
    ])
    rc = ap.run(args)

    assert rc == 0  # gives up refusing and actually publishes
    assert len(published) == 1
    state = ap.load_publish_state(state_dir)
    assert state['digest'] == ap.compute_tree_digest(root)  # the still-broken current digest
    assert state['published_at'] is not None
    assert state['refusing_since'] is None  # streak cleared once it publishes
    stderr = capsys.readouterr().err
    assert 'unavailable' in stderr.lower() or 'fail-soft' in stderr.lower()


def test_refusal_within_freeze_limit_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterpart to the above: a refusal streak that has NOT yet
    exceeded REFUSAL_FREEZE_MULTIPLE x the staleness floor must still
    refuse as before -- the freeze-on-persistent-refusal fallback is a
    bounded grace period, not an immediate override."""
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    good_digest = ap.compute_tree_digest(root)
    just_started = time.time()
    ap.save_publish_state(
        state_dir, digest=good_digest, published_at=1000.0, refusing_since=just_started,
    )

    (root / 'evolution/tree.json').write_text('{"current_sha": "b", "nod', encoding='utf-8')  # still torn

    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    called: list[str] = []
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: called.append(html_out) or 0)

    args = ap.parse_args([
        '--state-root', str(root), '--state-dir', str(state_dir),
        '--staleness-floor-hours', '6',  # generous floor -> streak nowhere near the limit
    ])
    rc = ap.run(args)

    assert rc != 0
    assert called == []
    state = ap.load_publish_state(state_dir)
    assert state['digest'] == good_digest
    assert state['published_at'] == 1000.0
    assert state['refusing_since'] == just_started  # streak start left unchanged


def test_zero_staleness_floor_does_not_defeat_the_torn_source_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #27 review round 4, finding 2: freeze_limit is
    REFUSAL_FREEZE_MULTIPLE x the staleness floor, so `--staleness-floor-hours
    0` made it 0, and `refusal_age >= 0` was true on the FIRST refusing cycle
    -- the escape fired immediately and published a degraded page over a good
    one on the very first torn read, defeating the guard it exists to bound.
    A non-positive limit must mean "no escape"."""
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    ap.save_publish_state(
        state_dir, digest=ap.compute_tree_digest(root), published_at=1000.0,
    )
    (root / 'evolution/tree.json').write_text(
        '{"current_sha": "b", "nod', encoding='utf-8',
    )  # torn

    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    called: list[str] = []
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: called.append(html_out) or 0)

    args = ap.parse_args([
        '--state-root', str(root), '--state-dir', str(state_dir),
        '--staleness-floor-hours', '0',
    ])
    assert ap.run(args) != 0
    assert called == []


def test_first_state_directory_falls_back_when_the_segment_is_empty() -> None:
    """An unset variable or a leading colon yields '', which as a Path is
    the working directory -- state written next to wherever the publisher
    happened to be invoked from. systemd cannot produce that (a
    StateDirectory= name cannot be empty) but a hand-run shell can."""
    assert ap._first_state_directory('') == ap.DEFAULT_STATE_DIR
    assert ap._first_state_directory(':/var/lib/b') == ap.DEFAULT_STATE_DIR
    assert ap._first_state_directory('/var/lib/a:/var/lib/b') == '/var/lib/a'
    assert ap._first_state_directory('/var/lib/a:') == '/var/lib/a'
    assert ap._first_state_directory('/var/lib/only') == '/var/lib/only'


# --- blocker B4: a state-write failure must be loud, not silent -----------

def test_save_publish_state_failure_is_logged_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """If state_dir can't be created/written (e.g. a StateDirectory=
    misconfiguration leaves it missing or read-only), save_publish_state
    must not swallow the OSError silently -- that silent swallow is exactly
    what previously caused every subsequent cycle to republish needlessly."""
    # A regular file where a directory is expected: state_dir.mkdir() will
    # raise (FileExistsError / NotADirectoryError, both OSError subclasses).
    blocked_path = tmp_path / 'not-a-directory'
    blocked_path.write_text('i am a file, not a directory', encoding='utf-8')
    state_dir = blocked_path / 'techtree-state'

    ap.save_publish_state(state_dir, digest='abc', published_at=1.0)

    out = capsys.readouterr()
    assert out.out == ''
    assert 'FAILED to save publish state' in out.err
    assert str(state_dir) in out.err


def test_a_failed_state_save_does_not_raise(tmp_path: Path) -> None:
    """save_publish_state must degrade to a loud log, never an uncaught
    exception, so a state-directory misconfiguration can't crash a
    publish that otherwise succeeded."""
    blocked_path = tmp_path / 'not-a-directory'
    blocked_path.write_text('x', encoding='utf-8')
    state_dir = blocked_path / 'techtree-state'

    ap.save_publish_state(state_dir, digest='abc', published_at=1.0)  # must not raise


# --- blocker B7: a backward clock jump must not disable the floor forever --

def test_negative_age_from_backward_clock_jump_publishes() -> None:
    """A clock set backward after the last publish would otherwise make
    `age` negative and permanently below the staleness floor, disabling it
    forever. A negative age must be treated as stale instead."""
    state = {'digest': 'same', 'published_at': 10_000.0}
    publish, reason = ap.should_publish('same', state, staleness_floor_seconds=3600, now=1000.0)  # clock moved back

    assert publish is True
    assert 'backward' in reason.lower() or 'clock' in reason.lower()


def test_run_publishes_on_backward_clock_jump_even_with_unchanged_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    digest = ap.compute_tree_digest(root)
    # published_at is in the "future" relative to now(), simulating a
    # backward wall-clock jump since the last publish.
    ap.save_publish_state(state_dir, digest=digest, published_at=time.time() + 10_000.0)

    published: list[str] = []
    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: published.append(html_out) or 0)

    args = ap.parse_args(['--state-root', str(root), '--state-dir', str(state_dir)])
    rc = ap.run(args)

    assert rc == 0
    assert len(published) == 1


# --- issue #29: unreadable tree source file raising PermissionError/OSError -

def test_unreadable_tree_source_permission_denied_returns_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a parent directory lacks execute/read permissions (issue #29),
    Path.exists() raises PermissionError. _unreadable_tree_source must catch
    PermissionError/OSError and report the source as unreadable instead of
    crashing."""
    root = tmp_path / 'state'
    data = {'evolution_tree': None, 'portfolio': {}, 'hypotheses': {}}

    def _mock_exists(self: Path) -> bool:
        if 'evolution' in str(self):
            raise PermissionError(13, 'Permission denied', str(self))
        return False

    monkeypatch.setattr(Path, 'exists', _mock_exists)
    problem = ap._unreadable_tree_source(data, root)
    assert problem is not None
    assert 'evolution_tree' in problem
    assert 'unreadable' in problem or 'could not be parsed' in problem


def test_permission_denied_tree_source_refuses_to_publish_and_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Full run() flow when a tree source file raises PermissionError on read/exists
    probes: run() must refuse to publish (exit 1), not call publish_to_pages,
    and log the unreadable source problem to stderr without an uncaught exception."""
    root = tmp_path / 'state'
    _write_state_root(root)
    state_dir = tmp_path / 'techtree-state'

    good_digest = ap.compute_tree_digest(root)
    ap.save_publish_state(state_dir, digest=good_digest, published_at=1000.0)

    real_open = Path.open
    real_exists = Path.exists

    def _mock_open(self: Path, *args: object, **kwargs: object) -> object:
        if 'evolution' in str(self):
            raise PermissionError(13, 'Permission denied', str(self))
        return real_open(self, *args, **kwargs)

    def _mock_exists(self: Path) -> bool:
        if 'evolution' in str(self):
            raise PermissionError(13, 'Permission denied', str(self))
        return real_exists(self)

    monkeypatch.setattr(Path, 'open', _mock_open)
    monkeypatch.setattr(Path, 'exists', _mock_exists)
    monkeypatch.setenv('GH_TOKEN', 'placeholder-not-a-real-token')
    called: list[str] = []
    monkeypatch.setattr(ap.tv, 'publish_to_pages', lambda html_out: called.append(html_out) or 0)

    args = ap.parse_args(['--state-root', str(root), '--state-dir', str(state_dir)])
    rc = ap.run(args)

    assert rc != 0
    assert called == []
    state = ap.load_publish_state(state_dir)
    assert state['digest'] == good_digest
    assert state['published_at'] == 1000.0
    stderr = capsys.readouterr().err
    assert 'evolution_tree' in stderr


# ---------------------------------------------------------------------------
# Issue #56: ledger tail participates in the publish digest
# ---------------------------------------------------------------------------


def test_issue56_digest_changes_when_ledger_tail_grows(tmp_path: Path) -> None:
    ledger = tmp_path / 'ledger' / 'cycles.jsonl'
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"phase": "started", "cycle_id": "cycle-a"}\n', encoding='utf-8')
    d1 = ap.compute_tree_digest(tmp_path)
    with open(ledger, 'a', encoding='utf-8') as fh:
        fh.write('{"phase": "outcome", "cycle_id": "cycle-a", "status": "fail"}\n')
    d2 = ap.compute_tree_digest(tmp_path)
    assert d1 != d2
    # stable when nothing changes
    assert ap.compute_tree_digest(tmp_path) == d2


def test_issue56_digest_ledger_tail_is_bounded(tmp_path: Path) -> None:
    ledger = tmp_path / 'ledger' / 'cycles.jsonl'
    ledger.parent.mkdir(parents=True)
    old_line = '{"phase": "started", "cycle_id": "cycle-old"}\n'
    lines = [old_line] + [f'{{"phase": "idle", "cycle_id": "cycle-i{i}"}}\n' for i in range(60)]
    ledger.write_text(''.join(lines), encoding='utf-8')
    d1 = ap.compute_tree_digest(tmp_path)
    # rotating an old line beyond the tail window must NOT change the digest
    lines[0] = '{"phase": "started", "cycle_id": "cycle-rotated"}\n'
    ledger.write_text(''.join(lines), encoding='utf-8')
    d2 = ap.compute_tree_digest(tmp_path)
    assert d1 == d2


def test_issue56_missing_ledger_does_not_crash(tmp_path: Path) -> None:
    d1 = ap.compute_tree_digest(tmp_path)  # no ledger at all
    ledger = tmp_path / 'ledger' / 'cycles.jsonl'
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"phase": "started"}\n', encoding='utf-8')
    d2 = ap.compute_tree_digest(tmp_path)
    assert d1 != d2  # ledger appearing counts as a change


def test_issue56_unreadable_ledger_fail_soft(tmp_path: Path) -> None:
    ledger = tmp_path / 'ledger' / 'cycles.jsonl'
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"phase": "started"}\n', encoding='utf-8')
    d1 = ap.compute_tree_digest(tmp_path)
    # simulate PermissionError on read (issue #29 pattern: report-and-skip)
    with mock.patch('builtins.open', side_effect=PermissionError(13, 'denied')):
        d2 = ap.compute_tree_digest(tmp_path)  # must not raise
    assert d1 != d2  # sentinel differs from readable hash

# ---------------------------------------------------------------------------
# Issue #73: lessons.yaml tail participates in the publish digest
# ---------------------------------------------------------------------------


def test_issue73_lessons_change_alters_digest(tmp_path, monkeypatch) -> None:
    state_root = tmp_path / 'state'
    state_root.mkdir()
    repo = tmp_path / 'eeebot-self-evolving'
    lessons_dir = repo / 'lessons'
    lessons_dir.mkdir(parents=True)
    base = 'lessons:\n  - id: "LESS-20260825-aaaa"\n    date: "2026-08-25"\n    result: "r1"\n'
    (lessons_dir / 'lessons.yaml').write_text(base, encoding='utf-8')
    d1 = ap.compute_tree_digest(state_root)
    (lessons_dir / 'lessons.yaml').write_text(base + '  - id: "LESS-20260825-bbbb"\n    date: "2026-08-25"\n    result: "r2"\n', encoding='utf-8')
    d2 = ap.compute_tree_digest(state_root)
    assert d1 != d2
