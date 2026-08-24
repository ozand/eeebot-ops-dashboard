from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import techtree_viewer as tv


def _fixture() -> dict[str, object]:
    return {
        'portfolio': {
            'current': 'proposer-quality',
            'nodes': {
                'cycle-cost': {
                    'lever_metric': 'cost.tokens_per_integration',
                    'gain_history': [],
                    'status': 'plateaued',
                    'minted_by': 'product',
                    'created_ts': '2026-08-01T00:00:00Z',
                    'cooldown_until_ts': '2026-08-19T00:00:00Z',
                },
                'stagnation-guard': {
                    'lever_metric': 'loop.repeat_failure_rate',
                    'gain_history': [0.01, 0.02],
                    'status': 'plateaued',
                    'minted_by': 'product',
                    'created_ts': '2026-08-05T00:00:00Z',
                    'cooldown_until_ts': None,
                },
                'proposer-quality': {
                    'lever_metric': 'loop.repeat_failure_rate',
                    'gain_history': [0.02, -0.01, 0.03],
                    'status': 'active',
                    'minted_by': 'hypothesis',
                    'created_ts': '2026-08-10T00:00:00Z',
                    'cooldown_until_ts': None,
                },
                'never-visited-direction': {
                    'lever_metric': 'cost.other',
                    'gain_history': [],
                    'status': 'active',
                    'minted_by': 'product',
                    'created_ts': '2026-08-11T00:00:00Z',
                    'cooldown_until_ts': None,
                },
            },
            'switches': [
                {'ts': '2026-08-17T04:04:22Z', 'from': 'cycle-cost', 'to': 'stagnation-guard', 'reason': 'plateau_switch'},
                {'ts': '2026-08-18T04:04:22Z', 'from': 'stagnation-guard', 'to': 'proposer-quality', 'reason': 'plateau_switch'},
            ],
        },
        'scorecard': {
            'computed_at_utc': '2026-08-18T08:22:34Z',
            'loop': {'integrations': 107, 'confirmed_integration_ratio': 0.08, 'repeat_failure_rate': 0.78},
            'cost': {'tokens_per_integration': 2539840.5},
            'heldout': {'checked': 4, 'passed': 4},
            'control_plane': {
                'runtime_trust_ladder': {'level': 0, 'unlocked': [], 'ladder': ['nanobot/runtime/existence_index.py']},
                'hypothesis_loop': {'active': 0, 'answered': 2, 'supported': 0, 'refuted': 0, 'inconclusive': 0},
            },
        },
        # Real git DAG: one root, forking into two children -- true
        # branching, not a flat timeline.
        'evolution_tree': {
            'current_sha': 'child1sha0000000000000000000000000000000',
            'nodes': {
                'rootsha00000000000000000000000000000000': {
                    'parent_sha': None,
                    'branch': 'selfevo/cycle-root',
                    'cycle_id': 'cycle-root',
                    'ts': '2026-08-15T00:00:00Z',
                    'fitness': {'reward': 0.85, 'integrations': 10},
                },
                'child1sha0000000000000000000000000000000': {
                    'parent_sha': 'rootsha00000000000000000000000000000000',
                    'branch': 'selfevo/cycle-a',
                    'cycle_id': 'cycle-a',
                    'ts': '2026-08-16T00:00:00Z',
                    'fitness': {'reward': 0.92, 'integrations': 12},
                },
                'child2sha0000000000000000000000000000000': {
                    'parent_sha': 'rootsha00000000000000000000000000000000',
                    # No embedded "/" in the malicious part -- the branch
                    # tail extraction (rsplit on "/") must not eat it, so
                    # this exercises html.escape on the actual tag text.
                    'branch': 'selfevo/cycle-b<script>bad',
                    'cycle_id': 'cycle-b<script>bad',
                    'ts': '2026-08-17T00:00:00Z',
                    'fitness': {'reward': 0.40, 'integrations': 3},
                },
            },
            'switches': [
                {
                    'ts': '2026-08-17T01:00:00Z',
                    'from_sha': 'rootsha00000000000000000000000000000000',
                    'to_sha': 'child2sha0000000000000000000000000000000',
                    'reason': 'plateau_switch',
                },
            ],
        },
        'hypotheses': {
            'entries': {
                'hyp-1': {
                    'title': 'Lower temperature increases smoke pass rate',
                    'status': 'answered',
                    'answered_evidence': 'cycle-a',
                    'answered_at': '2026-08-16T12:00:00Z',
                },
                'hyp-2': {
                    'title': 'Dynamic prompt injection improves dedup',
                    'status': 'active',
                    'first_seen': '2026-08-17T00:00:00Z',
                },
            },
        },
        'ledger_tail': [
            {'phase': 'started', 'cycle_id': 'cycle-a', 'ts': '2026-08-16T00:00:00Z'},
            {'phase': 'evolution_tree', 'cycle_id': 'cycle-a', 'sha': 'child1sha0000000000000000000000000000000', 'parent_sha': 'rootsha00000000000000000000000000000000', 'ts': '2026-08-16T00:01:00Z'},
            {'phase': 'outcome', 'cycle_id': 'cycle-a', 'status': 'success', 'ts': '2026-08-16T00:02:00Z'},
            {'phase': 'started', 'cycle_id': 'cycle-failed-1', 'ts': '2026-08-16T01:00:00Z'},
            {'phase': 'gate', 'cycle_id': 'cycle-failed-1', 'gate': 'smoke_test', 'status': 'fail', 'reason': 'syntax error in tests', 'ts': '2026-08-16T01:01:00Z'},
            {'phase': 'outcome', 'cycle_id': 'cycle-failed-1', 'status': 'fail', 'reason': 'smoke_failed', 'ts': '2026-08-16T01:02:00Z'},
        ],
        'demand_rotation': {
            'served': {'gap-1': '2026-08-16T00:00:00Z', 'gap-2': '2026-08-16T01:00:00Z'},
        },
        'demand_completed': {
            'entries': {
                'gap-1': {
                    'cycle_id': 'cycle-a',
                    'ts': '2026-08-16T00:02:00Z',
                    'files_changed': ['src/engine.py', 'tests/test_engine.py'],
                },
            },
        },
        'skill_reads': {
            'reads': [
                {'skill': 'test-driven-development', 'cycle_id': 'cycle-a', 'ts': '2026-08-16T00:00:30Z'},
                {'skill': 'test-driven-development', 'cycle_id': 'cycle-failed-1', 'ts': '2026-08-16T01:00:30Z'},
                {'skill': 'systematic-debugging', 'cycle_id': 'cycle-failed-1', 'ts': '2026-08-16T01:00:40Z'},
            ],
        },
        'goal_text': {
            'charter': 'Autonomously improve code quality, reduce tokens per integration, and eliminate repeat failures.',
            'version': '1.0',
        },
        'agents_md': '# Agent Operating System\n\nFollow test-driven development strictly.',
        'cycle_titles': {
            'cycle-a': 'Optimize prompt caching for proposer',
            'cycle-failed-1': 'Add speculative syntax checking',
        },
    }


def test_render_page_includes_node_cards_and_panels() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')

    assert 'Proposer Quality' in html_out
    assert 'Cycle Cost' in html_out
    assert 'Never Visited Direction' in html_out
    assert 'RESEARCHING' in html_out
    assert 'PLATEAUED' in html_out
    assert 'AVAILABLE' in html_out
    assert 'MINTED' in html_out
    assert 'Now / Active Focus' in html_out
    assert 'Cycle Feed' in html_out
    assert 'Hypotheses Lifecycle' in html_out
    assert 'Agent Configuration &amp; Fitness' in html_out or 'Agent Configuration & Fitness' in html_out
    assert 'EEEBOT EMPIRE' in html_out
    assert 'http://' not in html_out
    assert 'https://' not in html_out


def test_canvas_is_one_wide_svg_with_lane_labels() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')

    assert 'class="tech-canvas"' in html_out
    assert 'RESEARCH DIRECTIONS</text>' in html_out
    assert 'EVOLUTION LINEAGE (DGM)</text>' in html_out

    # The canvas must be a genuinely wide panorama, not a vertical list.
    import re
    match = re.search(r'<svg class="tech-canvas" width="(\d+)"', html_out)
    assert match is not None
    assert int(match.group(1)) >= 1200


def test_visited_directions_form_spine_in_switch_order_with_elbows() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')

    # switches trace cycle-cost -> stagnation-guard -> proposer-quality;
    # the spine boxes must appear left-to-right in that same order.
    pos_cost = html_out.index('Cycle Cost')
    pos_guard = html_out.index('Stagnation Guard')
    pos_quality = html_out.index('Proposer Quality')
    assert pos_cost < pos_guard < pos_quality

    # Orthogonal elbow connectors join the spine boxes.
    assert 'class="dir-elbow"' in html_out

    # The never-visited direction sits apart in the reserve column,
    # rendered dim (available, not part of the switch chronicle).
    assert 'dir-box-dim' in html_out

    # Hypothesis-minted direction gets a dashed gold edge.
    assert 'class="mint-elbow"' in html_out
    assert '&#127979;' in html_out


def test_world_history_renders_branching_boxes_with_elbows() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')

    # Real branching boxes (not bare circles), joined by orthogonal elbows.
    assert 'class="evo-box' in html_out
    assert 'class="evo-elbow"' in html_out
    # Both fork children (from the same parent) must have visible labels.
    assert 'Optimize prompt caching for proposer' in html_out
    assert 'child1s' in html_out
    # The second child's branch name is untrusted (LLM-authored) and
    # deliberately carries a script-injection attempt -- must be escaped,
    # never executed, and the sha7+tail label must still render.
    assert '<script>bad' not in html_out
    assert '&lt;script&gt;' in html_out
    assert 'child2s' in html_out
    # current-sha highlight (gold diamond ribbon) + switch marker glyph.
    assert 'evo-box-current' in html_out
    assert '&#9672;' in html_out
    assert '&#8634;' in html_out


# --- new block tests for Issue #30 ------------------------------------------

def test_now_panel_renders_active_focus_cycle_and_demand() -> None:
    fixture = _fixture()
    html_out = tv.render_page(fixture, host='eeepc', generated_at='2026-08-18 12:00:00')

    # Active research direction
    assert 'proposer-quality' in html_out
    assert 'loop.repeat_failure_rate' in html_out

    # Latest cycle title
    assert 'Optimize prompt caching for proposer' in html_out

    # Demand queue snapshot
    assert 'gap-1' in html_out
    assert 'gap-2' in html_out
    assert 'demand-chip served' in html_out
    assert 'demand-chip completed' in html_out


def test_cycle_feed_renders_outcomes_files_and_failed_cycles() -> None:
    fixture = _fixture()
    html_out = tv.render_page(fixture, host='eeepc', generated_at='2026-08-18 12:00:00')

    # Integrated cycle
    assert 'INTEGRATED' in html_out
    assert 'src/engine.py' in html_out

    # Failed cycle (with gate block reason) must be visible
    assert 'Add speculative syntax checking' in html_out
    assert 'cycle-failed-1' in html_out
    assert 'syntax error in tests' in html_out or 'smoke_failed' in html_out
    assert 'feed-outcome-failed' in html_out or 'feed-outcome-gate_blocked' in html_out


def test_hypotheses_lifecycle_groups_active_and_answered() -> None:
    fixture = _fixture()
    html_out = tv.render_page(fixture, host='eeepc', generated_at='2026-08-18 12:00:00')

    # Active group
    assert 'Active (1)' in html_out
    assert 'Dynamic prompt injection improves dedup' in html_out

    # Answered group with evidence anchor
    assert 'Answered (1)' in html_out
    assert 'Lower temperature increases smoke pass rate' in html_out
    assert '<a href="#cycle-cycle-a">cycle-a</a>' in html_out or 'href="#cycle-cycle-a"' in html_out or 'href="#cycle-a"' in html_out


def test_agent_panel_escapes_agents_md_and_aggregates_skills() -> None:
    fixture = _fixture()
    # Add script injection to AGENTS.md to verify escaping
    fixture['agents_md'] = '# Agent Guide\n<script>alert("xss")</script>'
    html_out = tv.render_page(fixture, host='eeepc', generated_at='2026-08-18 12:00:00')

    # AGENTS.md escaped
    assert '<script>alert' not in html_out
    assert '&lt;script&gt;alert' in html_out

    # Goals charter
    assert 'Autonomously improve code quality' in html_out

    # Skill reads table
    assert 'test-driven-development' in html_out
    assert '<td class="skill-reads">2</td>' in html_out
    assert 'systematic-debugging' in html_out
    assert '<td class="skill-reads">1</td>' in html_out
    assert 'not tracked' in html_out


def test_extract_git_titles_local_parsing(tmp_path: Path) -> None:
    # Initialize a dummy git repo with a merge commit
    repo = tmp_path / 'test_repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-b', 'master', str(repo)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Tester'], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.com'], check=True)

    # Base commit on master
    (repo / 'file.txt').write_text('base', encoding='utf-8')
    subprocess.run(['git', '-C', str(repo), 'add', 'file.txt'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'chore: initial'], check=True)

    # Feature branch
    subprocess.run(['git', '-C', str(repo), 'checkout', '-b', 'selfevo/cycle-cycle-123'], check=True)
    (repo / 'file.txt').write_text('feature', encoding='utf-8')
    subprocess.run(['git', '-C', str(repo), 'add', 'file.txt'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'Add fuzzy matching to proposer'], check=True)

    # Merge into master
    subprocess.run(['git', '-C', str(repo), 'checkout', 'master'], check=True)
    subprocess.run(['git', '-C', str(repo), 'merge', '--no-ff', 'selfevo/cycle-cycle-123', '-m', 'merge: integrate selfevo/cycle-cycle-123'], check=True)

    titles = tv.extract_git_titles_local(repo)
    assert 'cycle-123' in titles or 'cycle-cycle-123' in titles
    assert titles.get('cycle-123') == 'Add fuzzy matching to proposer' or titles.get('cycle-cycle-123') == 'Add fuzzy matching to proposer'


def test_extract_git_titles_local_non_repo(tmp_path: Path) -> None:
    not_repo = tmp_path / 'not_a_repo'
    not_repo.mkdir()
    titles = tv.extract_git_titles_local(not_repo)
    assert titles == {}



def test_render_page_fails_soft_on_missing_sources() -> None:
    empty = {'portfolio': None, 'scorecard': None, 'evolution_tree': None, 'hypotheses': None, 'ledger_tail': None}
    html_out = tv.render_page(empty, host='eeepc', generated_at='2026-08-18 12:00:00')

    assert 'unavailable' in html_out.lower()
    assert '<html' in html_out
    # Canvas still renders (both lanes fail-soft to an unavailable strip)
    # rather than crashing the page.
    assert 'class="tech-canvas"' in html_out


def test_empty_switches_falls_back_to_reserve_column_only() -> None:
    no_journey = {
        'portfolio': {
            'current': None,
            'nodes': {
                'solo-direction': {
                    'lever_metric': 'loop.repeat_failure_rate',
                    'gain_history': [],
                    'status': 'active',
                    'minted_by': 'product',
                    'created_ts': '2026-08-01T00:00:00Z',
                },
            },
            'switches': [],
        },
        'scorecard': None,
        'evolution_tree': None,
        'hypotheses': None,
        'ledger_tail': [],
    }
    html_out = tv.render_page(no_journey, host='eeepc', generated_at='2026-08-18 12:00:00')

    assert 'no research journey yet' in html_out
    assert 'class="dir-elbow"' not in html_out
    assert 'Solo Direction' in html_out


def test_world_history_falls_back_to_simple_list_below_two_nodes() -> None:
    single_node = {
        'portfolio': None,
        'scorecard': None,
        'evolution_tree': {
            'current_sha': 'onlyshaaaa00000000000000000000000000000',
            'nodes': {
                'onlyshaaaa00000000000000000000000000000': {
                    'parent_sha': None,
                    'branch': 'selfevo/cycle-solo',
                    'ts': '2026-08-15T00:00:00Z',
                },
            },
            'switches': [],
        },
        'hypotheses': None,
        'ledger_tail': None,
    }
    html_out = tv.render_page(single_node, host='eeepc', generated_at='2026-08-18 12:00:00')

    assert 'class="evo-box' not in html_out
    assert 'timeline-list' in html_out


# --- local state reading (issue #27, Task 1) --------------------------------

def _write_local_state_root(root: Path) -> None:
    (root / 'tech_tree').mkdir(parents=True)
    (root / 'scorecard').mkdir(parents=True)
    (root / 'evolution').mkdir(parents=True)
    (root / 'hypotheses').mkdir(parents=True)
    (root / 'ledger').mkdir(parents=True)
    (root / 'tech_tree' / 'portfolio.json').write_text(json.dumps({'current': None, 'nodes': {}}), encoding='utf-8')
    (root / 'scorecard' / 'latest.json').write_text(json.dumps({'computed_at_utc': '2026-08-18T00:00:00Z'}), encoding='utf-8')
    (root / 'evolution' / 'tree.json').write_text(json.dumps({'current_sha': 'a', 'nodes': {}}), encoding='utf-8')
    (root / 'hypotheses' / 'lifecycle.json').write_text(json.dumps({'entries': {}}), encoding='utf-8')
    (root / 'ledger' / 'cycles.jsonl').write_text(
        json.dumps({'phase': 'tech_tree', 'from': 'a', 'to': 'b'}) + '\n', encoding='utf-8',
    )


def test_read_local_state_matches_remote_shape(tmp_path: Path) -> None:
    _write_local_state_root(tmp_path)
    data = tv.read_local_state(str(tmp_path))

    assert set(data) >= {'portfolio', 'scorecard', 'evolution_tree', 'hypotheses', 'ledger_tail'}
    assert data['portfolio'] == {'current': None, 'nodes': {}}
    assert data['evolution_tree'] == {'current_sha': 'a', 'nodes': {}}
    assert data['hypotheses'] == {'entries': {}}
    assert data['ledger_tail'] == [{'phase': 'tech_tree', 'from': 'a', 'to': 'b'}]
    assert data.get('_error') is None
    # A real mtime was read, so an age must be available (not fabricated).
    assert isinstance(data['_newest_source_age_seconds'], (int, float))
    assert data['_newest_source_age_seconds'] >= 0


def test_read_local_state_missing_root_reports_error(tmp_path: Path) -> None:
    missing = tmp_path / 'does-not-exist'
    data = tv.read_local_state(str(missing))

    assert data['portfolio'] is None
    assert data['scorecard'] is None
    assert data['evolution_tree'] is None
    assert data['hypotheses'] is None
    assert data['ledger_tail'] is None
    assert data.get('_error')
    assert data['_newest_source_age_seconds'] is None


def test_read_local_state_partial_root_fails_soft_per_file(tmp_path: Path) -> None:
    # Only the state root itself exists; none of the five source files do.
    # This must behave like fetch_remote_state on a reachable-but-empty
    # state tree: no top-level `_error`, each field individually None.
    data = tv.read_local_state(str(tmp_path))

    assert data.get('_error') is None
    assert data['portfolio'] is None
    assert data['ledger_tail'] == []


# --- freshness footer (issue #27, Task 2) -----------------------------------

def test_footer_shows_utc_label_and_known_source_age() -> None:
    fixture = _fixture()
    fixture['_newest_source_age_seconds'] = 125  # 2m

    html_out = tv.render_page(fixture, host='eeepc', generated_at='2026-08-18 12:00:00')

    assert 'generated 2026-08-18 12:00:00 UTC' in html_out
    assert 'newest source 2m old' in html_out


def test_footer_says_age_unknown_when_no_mtime_available() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')

    # _fixture() carries no _newest_source_age_seconds key -- this must
    # read as an honest "unknown", never a fabricated 0 or omitted marker.
    assert 'age unknown' in html_out


# --- blocker B2: the public page must never leak a host filesystem path ----

def test_render_page_never_leaks_host_path_from_error(tmp_path: Path) -> None:
    fixture = _fixture()
    host_path = str(tmp_path / 'var' / 'lib' / 'eeepc-agent' / 'self-evolving-agent' / 'state')
    fixture['_error'] = f'state root not found or not a directory: {host_path}'

    html_out = tv.render_page(fixture, host='eeepc', generated_at='2026-08-18 12:00:00')

    assert host_path not in html_out
    assert '/var/lib' not in html_out
    # A fixed, generic notice still appears -- the reader is told *something*
    # went wrong, just not the host's internal filesystem layout.
    assert 'fetch note' in html_out


def test_read_local_state_missing_root_error_message_not_echoed_on_page() -> None:
    """End-to-end: read_local_state's own real error message (which does
    contain the state root path) must not survive into render_page's HTML.
    Checked via distinctive path segments rather than the full path string
    so this holds regardless of the platform's path-separator rendering."""
    missing_root = '/var/lib/eeepc-agent/self-evolving-agent/state'
    data = tv.read_local_state(missing_root)
    error_text = data.get('_error') or ''
    # Sanity: the raw error really does carry host-specific path detail.
    assert 'eeepc-agent' in error_text
    assert 'self-evolving-agent' in error_text

    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'eeepc-agent' not in html_out
    assert 'self-evolving-agent' not in html_out


# --- blocker B3: _gh must return non-zero, never raise -----------------

def test_gh_timeout_returns_nonzero_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd='gh', timeout=60)

    monkeypatch.setattr(subprocess, 'run', _raise_timeout)

    result = tv._gh(['api', 'repos/foo/bar'])  # must not raise

    assert result.returncode != 0


def test_gh_timeout_does_not_leak_payload_from_exc_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """N9 (issue #27 review round 3) claimed the leak path was str(exc)
    including captured output; item D (round 4) found that's wrong --
    TimeoutExpired.__str__ never includes output/stderr, so the tests
    above using a short cmd='gh' string can never exercise the real leak
    path, which is exc.cmd being the full argv list (item D/N9, round 4).
    Use a list-shaped cmd carrying a base64-sized payload, as the contents
    PUT call's argv actually would, and confirm it never reaches stderr."""
    payload = 'A' * 100
    args = ['api', '-X', 'PUT', 'repos/foo/bar/contents/index.html',
            '-f', f'content={payload}']

    def _raise_timeout(*a, **kwargs):
        raise subprocess.TimeoutExpired(cmd=['gh'] + args, timeout=60)

    monkeypatch.setattr(subprocess, 'run', _raise_timeout)

    result = tv._gh(args)  # must not raise

    assert result.returncode != 0
    assert payload not in result.stderr


def test_gh_timeout_message_identifies_the_failing_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Item G (issue #27 review round 4): args[0] is always 'api' for
    every _gh call in publish_to_pages, so a timeout message built from
    args[0] alone ("gh api timed out") can never say which of the several
    `gh api` calls in one run actually hung. The message must name the
    endpoint instead -- args[1] normally, or the slot after a leading
    '-X <verb>' pair for the POST/PUT calls."""
    def _raise_timeout(*a, **kwargs):
        raise subprocess.TimeoutExpired(cmd='gh', timeout=60)

    monkeypatch.setattr(subprocess, 'run', _raise_timeout)

    plain = tv._gh(['api', 'repos/foo/bar/branches/gh-pages'])
    assert 'repos/foo/bar/branches/gh-pages' in plain.stderr

    posted = tv._gh(['api', '-X', 'POST', 'repos/foo/bar/git/refs', '-f', 'ref=x'])
    assert 'repos/foo/bar/git/refs' in posted.stderr
    assert 'gh api -X timed out' not in posted.stderr


def test_gh_missing_binary_returns_nonzero_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError('gh: command not found')

    monkeypatch.setattr(subprocess, 'run', _raise_not_found)

    result = tv._gh(['api', 'repos/foo/bar'])  # must not raise

    assert result.returncode != 0


def test_publish_to_pages_returns_one_when_gh_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """publish_to_pages's docstring promises 'Returns 0 on success, 1 on
    any failure' -- this must hold even when the underlying `gh` subprocess
    itself raises (timeout, missing binary), not just when it exits nonzero."""
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd='gh', timeout=60)

    monkeypatch.setattr(subprocess, 'run', _raise_timeout)

    rc = tv.publish_to_pages('<html></html>')  # must not raise

    assert rc == 1
