from __future__ import annotations

import json
from pathlib import Path

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

def test_digest_ignores_scorecard_and_ledger(tmp_path: Path) -> None:
    root = tmp_path / 'state'
    _write_state_root(root)
    digest_before = ap.compute_tree_digest(root)

    (root / 'scorecard/latest.json').write_text('{"computed_at_utc": "2026-08-19T00:00:00Z"}', encoding='utf-8')
    (root / 'ledger/cycles.jsonl').write_text('{"phase": "tech_tree"}\n', encoding='utf-8')

    digest_after = ap.compute_tree_digest(root)
    assert digest_after == digest_before


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


# --- state file persistence (acceptance tests 4, 5) -------------------------

def test_save_and_load_publish_state_roundtrip(tmp_path: Path) -> None:
    state_dir = tmp_path / 'techtree-state'
    ap.save_publish_state(state_dir, digest='abc123', published_at=12345.0)

    loaded = ap.load_publish_state(state_dir)
    assert loaded == {'digest': 'abc123', 'published_at': 12345.0}


def test_load_publish_state_missing_file_reads_as_never_published(tmp_path: Path) -> None:
    state_dir = tmp_path / 'does-not-exist-yet'
    loaded = ap.load_publish_state(state_dir)
    assert loaded == {'digest': None, 'published_at': None}


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
    assert loaded == {'digest': 'good', 'published_at': 500.0}


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
    assert ap.load_publish_state(state_dir) == {'digest': None, 'published_at': None}


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
    assert ap.load_publish_state(state_dir) == {'digest': None, 'published_at': None}


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
