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
    assert '# eeebot / tech-tree' in html_out
    assert 'http://' not in html_out
    assert 'https://' not in html_out


def test_canvas_is_one_wide_svg_with_lane_labels() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')

    assert 'class="tech-canvas"' in html_out
    assert 'RESEARCH DIRECTIONS</text>' in html_out
    assert 'EVOLUTION LINEAGE (DGM)</text>' in html_out

    # The canvas must be a genuinely wide panorama, not a vertical list.
    import re
    match = re.search(r'<svg class="tech-canvas" role="img" [^>]*width="(\d+)"', html_out)
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
    (repo / 'feature_file.txt').write_text('feature', encoding='utf-8')
    subprocess.run(['git', '-C', str(repo), 'add', 'feature_file.txt'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'Add fuzzy matching to proposer'], check=True)

    # Merge into master
    subprocess.run(['git', '-C', str(repo), 'checkout', 'master'], check=True)
    subprocess.run(['git', '-C', str(repo), 'merge', '--no-ff', 'selfevo/cycle-cycle-123', '-m', 'merge: integrate selfevo/cycle-cycle-123'], check=True)

    titles, cycle_files, err = tv.extract_git_titles_local(repo)
    assert err is None
    assert 'cycle-123' in titles or 'cycle-cycle-123' in titles
    assert titles.get('cycle-123') == 'Add fuzzy matching to proposer' or titles.get('cycle-cycle-123') == 'Add fuzzy matching to proposer'
    assert 'feature_file.txt' in cycle_files.get('cycle-cycle-123', []) or 'feature_file.txt' in cycle_files.get('cycle-123', [])


def test_extract_git_titles_local_non_repo(tmp_path: Path) -> None:
    not_repo = tmp_path / 'not_a_repo'
    not_repo.mkdir()
    titles, cycle_files, err = tv.extract_git_titles_local(not_repo)
    assert titles == {}
    assert cycle_files == {}
    # Non-repo fails git log command with exit 128 / fatal message
    assert err is not None
    assert 'not a git repository' in err


def test_extract_git_titles_local_dubious_ownership_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()
    mock_res = subprocess.CompletedProcess(
        args=['git'],
        returncode=128,
        stdout='',
        stderr='fatal: detected dubious ownership in repository at \'/var/lib/eeepc-agent/...\'',
    )
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: mock_res)
    titles, cycle_files, err = tv.extract_git_titles_local(repo)
    assert titles == {}
    assert cycle_files == {}
    assert err is not None
    assert 'detected dubious ownership' in err
    assert 'exit 128' in err


def test_extract_git_titles_local_mocked_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()
    
    def fake_run(cmd, *args, **kwargs):
        if 'log' in cmd and '--first-parent' in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='msha123 merge: integrate selfevo/cycle-456\n',
                stderr='',
            )
        elif 'log' in cmd and 'msha123^2' in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='Add awesome feature\n',
                stderr='',
            )
        elif 'diff' in cmd and 'msha123^1' in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='src/awesome.py\ntests/test_awesome.py\n',
                stderr='',
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    titles, cycle_files, err = tv.extract_git_titles_local(repo)
    assert err is None
    assert titles.get('cycle-456') == 'Add awesome feature'
    assert cycle_files.get('cycle-456') == ['src/awesome.py', 'tests/test_awesome.py']


def test_render_page_footer_shows_cycle_titles_error() -> None:
    data = _fixture()
    data['cycle_titles_error'] = 'git log failed (exit 128): fatal: detected dubious ownership & <foo>'
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert '&#9888; task titles unavailable (git log failed (exit 128): fatal: detected dubious ownership &amp; &lt;foo&gt;)' in html_out


def test_evo_box_html_labels_and_none_handling() -> None:
    node_none = {
        'parent_sha': None,
        'branch': 'selfevo/cycle-1',
        'fitness': {'reward': None, 'integrations': None},
    }
    html_none = tv._evo_box_html('sha1', node_none, is_current=False, is_abandoned=False, switch_marked=False, x=10.0, y=20.0, portfolio={}, task_titles={})
    assert 'evo-fitness' not in html_none
    assert 'r:None' not in html_none
    assert 'r:—' not in html_none
    assert 'integrations' not in html_none
    assert 'int:' not in html_none

    node_values = {
        'parent_sha': None,
        'branch': 'selfevo/cycle-2',
        'fitness': {'reward': 0.85, 'integrations': 3},
    }
    html_values = tv._evo_box_html('sha2', node_values, is_current=False, is_abandoned=False, switch_marked=False, x=10.0, y=20.0, portfolio={}, task_titles={})
    assert 'evo-fitness' in html_values
    assert 'r:0.85' in html_values
    assert 'integrations' not in html_values
    assert 'int:3' not in html_values


def test_build_cycle_feed_merges_demand_files_and_cycle_files() -> None:
    ledger_tail = [
        {'phase': 'started', 'cycle_id': 'cycle-merged', 'ts': '2026-08-16T00:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-merged', 'status': 'success', 'ts': '2026-08-16T00:02:00Z'},
    ]
    demand_completed = {
        'entries': {
            'gap-1': {
                'cycle_id': 'cycle-merged',
                'files_changed': ['file1.py', 'file2.py'],
            },
        },
    }
    cycle_files = {
        'cycle-merged': ['file2.py', 'file3.py', 'file4.py', 'file5.py'],
    }
    html_out = tv.build_cycle_feed(
        ledger_tail=ledger_tail,
        demand_completed=demand_completed,
        task_titles={'cycle-merged': 'Merged Task'},
        evolution_tree=None,
        cycle_files=cycle_files,
    )
    # Merged files: file1.py, file2.py, file3.py, file4.py, file5.py -> 5 files
    # First 3: file1.py, file2.py, file3.py +2 more
    assert 'file1.py' in html_out
    assert 'file2.py' in html_out
    assert 'file3.py' in html_out
    assert '+2 more' in html_out
    assert 'file4.py' in html_out  # in the title attribute
    assert 'file5.py' in html_out


def test_remote_reader_script_compiles() -> None:
    code = tv.REMOTE_READER_SCRIPT
    compiled = compile(code, '<remote_reader_script>', 'exec')
    assert compiled is not None



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


# --- Tests for Batch 1 fixes (issues #35, #36, #37) -------------------------

def test_task1_issue35_cycle_feed_partial_and_reasons() -> None:
    # Cycles without titles (non-integrated / partial / gate-blocked / proposer_reject / idle)
    ledger_tail = [
        {'phase': 'started', 'cycle_id': 'cycle-idle-1', 'ts': '2026-08-16T00:00:00Z'},
        {'phase': 'idle', 'cycle_id': 'cycle-idle-1', 'reason': 'no demand', 'ts': '2026-08-16T00:00:10Z'},

        {'phase': 'started', 'cycle_id': 'cycle-reject-1', 'ts': '2026-08-16T01:00:00Z'},
        {'phase': 'proposer_reject', 'cycle_id': 'cycle-reject-1', 'reason': 'self_dedup', 'ts': '2026-08-16T01:00:20Z'},

        {'phase': 'started', 'cycle_id': 'cycle-gate-1', 'ts': '2026-08-16T02:00:00Z'},
        {'phase': 'gate', 'cycle_id': 'cycle-gate-1', 'gate': 'smoke_test', 'status': 'fail', 'reason': 'blocked_by_smoke', 'ts': '2026-08-16T02:00:30Z'},

        {'phase': 'started', 'cycle_id': 'cycle-partial-1', 'ts': '2026-08-16T03:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-partial-1', 'status': 'partial', 'reason': 'step limit reached', 'ts': '2026-08-16T03:00:40Z'},

        {'phase': 'started', 'cycle_id': 'cycle-running-1', 'ts': '2026-08-16T04:00:00Z'},
    ]
    html_out = tv.build_cycle_feed(
        ledger_tail=ledger_tail,
        demand_completed=None,
        task_titles={},  # No titles
        evolution_tree=None,
        cycle_files=None,
    )

    # Human-readable reasons derived and rendered in place of missing task title
    assert 'idle: no demand' in html_out
    assert 'rejected: self_dedup' in html_out
    assert 'gate blocked: blocked_by_smoke' in html_out
    assert 'partial: step limit reached' in html_out
    assert 'in progress' in html_out

    # Badge CSS class for PARTIAL must be badge-partial (not badge-researching)
    assert 'badge-partial' in html_out
    assert 'PARTIAL' in html_out

    # Make sure PARTIAL does not use badge-researching
    assert 'badge badge-researching">PARTIAL' not in html_out


def test_task2_issue36_lineage_fitness_rendering() -> None:
    node_no_fitness = {
        'parent_sha': None,
        'branch': 'selfevo/cycle-none',
    }
    node_none_reward = {
        'parent_sha': None,
        'branch': 'selfevo/cycle-none',
        'fitness': {'reward': None, 'integrations': 104},
    }
    node_float_reward = {
        'parent_sha': None,
        'branch': 'selfevo/cycle-values',
        'fitness': {'reward': 0.8125, 'integrations': 50},
    }
    node_int_reward = {
        'parent_sha': None,
        'branch': 'selfevo/cycle-int',
        'fitness': {'reward': 1, 'integrations': 10},
    }

    assert 'evo-fitness' not in tv._evo_box_html('sha0', node_no_fitness, False, False, False, 0.0, 0.0, {}, {})
    assert 'evo-fitness' not in tv._evo_box_html('sha1', node_none_reward, False, False, False, 0.0, 0.0, {}, {})
    assert 'integrations' not in tv._evo_box_html('sha1', node_none_reward, False, False, False, 0.0, 0.0, {}, {})

    html_float = tv._evo_box_html('sha2', node_float_reward, False, False, False, 0.0, 0.0, {}, {})
    assert 'evo-fitness' in html_float
    assert 'r:0.81' in html_float
    assert 'integrations' not in html_float

    html_int = tv._evo_box_html('sha3', node_int_reward, False, False, False, 0.0, 0.0, {}, {})
    assert 'evo-fitness' in html_int
    assert 'r:1' in html_int
    assert 'integrations' not in html_int


def test_task3_issue37_hypothesis_evidence_anchors() -> None:
    hypotheses_lifecycle = {
        'entries': {
            'hyp-1': {
                'title': 'Hypothesis with cycle in feed',
                'status': 'answered',
                'answered_evidence': 'cycle-present',
            },
            'hyp-2': {
                'title': 'Hypothesis with cycle NOT in feed',
                'status': 'answered',
                'answered_evidence': 'cycle-absent',
            },
        },
    }
    feed_cycles = {'cycle-present'}

    html_out = tv.build_hypotheses_panel(hypotheses_lifecycle, feed_cycles=feed_cycles)

    # Present cycle gets a real link
    assert '<a href="#cycle-cycle-present">cycle-present</a>' in html_out
    # Absent cycle renders plain text, no <a> link
    assert 'cycle-absent' in html_out
    assert 'href="#cycle-cycle-absent"' not in html_out
    assert '<a href="#cycle-absent">' not in html_out


# ===========================================================================
# Batch 2 Data UX Tests (Issues #40, #41, #42)
# ===========================================================================

def test_hypotheses_panel_dedupe_by_title_keeps_newest() -> None:
    data = {
        'entries': {
            'h-old': {
                'title': 'Same Hypothesis Title',
                'status': 'researching',
                'first_seen': '2026-08-01T10:00:00Z',
                'last_touched': '2026-08-01T10:00:00Z',
            },
            'h-new': {
                'title': 'Same Hypothesis Title',
                'status': 'researching',
                'first_seen': '2026-08-01T10:00:00Z',
                'last_touched': '2026-08-20T12:00:00Z',
            },
        }
    }
    html = tv.build_hypotheses_panel(data)
    # The title should appear only once in the list
    assert html.count('Same Hypothesis Title') == 1
    # The rendered timestamp should correspond to the newest (Aug 20) not the old one (Aug 1)
    assert 'touched Aug 20' in html or 'Aug 20' in html
    assert 'touched Aug 1' not in html


def test_hypotheses_panel_active_before_stale_ordering() -> None:
    data = {
        'entries': {
            'h-stale-status': {
                'title': 'Stale By Status',
                'status': 'stale',
                'last_touched': '2026-08-20T10:00:00Z',
            },
            'h-active': {
                'title': 'Active Hypothesis',
                'status': 'researching',
                'last_touched': '2026-08-15T10:00:00Z',
            },
        }
    }
    html = tv.build_hypotheses_panel(data)
    active_idx = html.find('Active Hypothesis')
    stale_idx = html.find('Stale By Status')
    assert active_idx != -1 and stale_idx != -1
    assert active_idx < stale_idx, "Non-stale hypotheses must appear before stale hypotheses"


def test_hypotheses_panel_stale_badge_class() -> None:
    data = {
        'entries': {
            'h-stale': {
                'title': 'Stale One',
                'status': 'stale',
                'last_touched': '2026-08-01T10:00:00Z',
            },
        }
    }
    html = tv.build_hypotheses_panel(data)
    assert 'badge-stale' in html
    assert 'badge-researching' not in html


def test_hypotheses_panel_stale_collapse_when_gt_6() -> None:
    # 2 active, 7 stale => stale collapsed in <details><summary>
    entries = {
        'h-act-1': {'title': 'Act 1', 'status': 'researching', 'last_touched': '2026-08-20T10:00:00Z'},
        'h-act-2': {'title': 'Act 2', 'status': 'researching', 'last_touched': '2026-08-21T10:00:00Z'},
    }
    for i in range(7):
        entries[f'h-stale-{i}'] = {
            'title': f'Stale {i}',
            'status': 'stale',
            'last_touched': f'2026-07-0{i+1}T10:00:00Z',
        }
    data = {'entries': entries}
    html = tv.build_hypotheses_panel(data)
    assert '<details' in html
    assert '<summary' in html
    assert '7 stale hypotheses &mdash; show' in html or '7 stale hypotheses — show' in html or '7 stale hypotheses' in html
    # Active items should be outside details
    details_idx = html.find('<details')
    act1_idx = html.find('Act 1')
    act2_idx = html.find('Act 2')
    stale0_idx = html.find('Stale 0')
    assert act1_idx < details_idx
    assert act2_idx < details_idx
    assert stale0_idx > details_idx


def test_hypotheses_panel_no_collapse_when_lte_6_stale() -> None:
    entries = {
        'h-act-1': {'title': 'Act 1', 'status': 'researching', 'last_touched': '2026-08-20T10:00:00Z'},
    }
    for i in range(6):
        entries[f'h-stale-{i}'] = {
            'title': f'Stale {i}',
            'status': 'stale',
            'last_touched': f'2026-07-0{i+1}T10:00:00Z',
        }
    data = {'entries': entries}
    html = tv.build_hypotheses_panel(data)
    assert '<details' not in html


def test_now_panel_demand_grouping_fallback() -> None:
    demand_rotation = {
        'served': {
            'goal-gap-1111': '2026-08-20T10:00:00Z',
            'goal-gap-2222': '2026-08-20T11:00:00Z',
            'defect-3333': '2026-08-20T12:00:00Z',
            'priority-4444': '2026-08-20T13:00:00Z',
            'otheritem-5555': '2026-08-20T14:00:00Z',
        }
    }
    html = tv.build_now_panel(
        portfolio=None,
        evolution_tree=None,
        ledger_tail=[],
        demand_rotation=demand_rotation,
        demand_completed=None,
    )
    # Check group labels/counts
    assert 'goal-gap' in html
    assert 'defect' in html
    assert 'priority' in html
    assert 'demand-group' in html
    # Check chip IDs and tooltips
    assert 'goal-gap-1111' in html
    assert 'title=' in html


def test_fmt_compact() -> None:
    assert tv.fmt_compact(None) == 'n/a'
    assert tv.fmt_compact(0) == '0'
    assert tv.fmt_compact(42) == '42'
    assert tv.fmt_compact(123.456) == '123.46'
    assert tv.fmt_compact(1500) == '1.50K'
    assert tv.fmt_compact(166519.3317) == '+166.5K' or tv.fmt_compact(166519.3317, signed=True) == '+166.5K' or tv.fmt_compact(166519.3317) == '166.5K'
    assert tv.fmt_compact(1700935.1681) == '1.70M'
    assert tv.fmt_compact(-1500) == '-1.50K'
    assert tv.fmt_compact(-2500000) == '-2.50M'
    assert tv.fmt_compact('13.2%') == '13.2%'


def test_fmt_ts_short() -> None:
    from datetime import datetime, timezone
    ref_now = datetime(2026, 8, 24, 15, 30, 0, tzinfo=timezone.utc)
    # Today timestamp -> HH:MM UTC
    today_ts = '2026-08-24T06:39:15Z'
    assert tv.fmt_ts_short(today_ts, now=ref_now) == '06:39 UTC'

    # Same year, older date -> Mon DD
    older_same_year = '2026-08-14T10:00:00Z'
    assert tv.fmt_ts_short(older_same_year, now=ref_now) == 'Aug 14'

    # Different year -> Mon DD YYYY
    older_diff_year = '2025-08-14T10:00:00Z'
    assert tv.fmt_ts_short(older_diff_year, now=ref_now) == 'Aug 14 2025'

    # Invalid / empty
    assert tv.fmt_ts_short(None, now=ref_now) == 'n/a'
    assert tv.fmt_ts_short('', now=ref_now) == 'n/a'



def test_issue42_feed_delta_and_ts_humanized() -> None:
    ledger_tail = [
        {'phase': 'outcome', 'cycle_id': 'cycle-aaa', 'ts': '2026-08-24T06:39:44Z', 'delta': '166519.3317'},
    ]
    html = tv.build_cycle_feed(
        ledger_tail=ledger_tail,
        demand_completed=None,
        task_titles=None,
        evolution_tree=None,
        cycle_files=None,
    )
    assert '+166.5K' in html
    assert 'title="2026-08-24T06:39:44Z"' in html
    assert 'UTC' in html
    assert '06:39' in html


def test_issue42_now_lever_value_compact() -> None:
    portfolio = {
        'current': 'cycle-cost',
        'nodes': {
            'cycle-cost': {
                'lever_metric': 'cost.tokens_per_integration',
                'direction': 'lower',
                'status': 'researching',
                'last_lever_value': 1700935.1681,
            }
        },
    }
    html = tv.build_now_panel(
        portfolio=portfolio,
        evolution_tree=None,
        demand_rotation=None,
        demand_completed=None,
    )
    assert '1.70M' in html
    assert '1700935' not in html


def test_issue42_spark_mean_compact() -> None:
    svg = tv.build_sparkline([841238.0562, 4450.5615, 9461.3803])
    assert 'mean gain +' in svg
    assert 'K' in svg
    assert '.3317' not in svg


def test_issue42_evo_fitness_compact() -> None:
    node = {'parent_sha': None, 'branch': 'selfevo/cycle-x', 'fitness': {'reward': 1234567.8}}
    html = tv._evo_box_html('abc1234', node, False, False, False, 0.0, 0.0, {}, {})
    assert 'r:1.23M' in html


# =========================================================================
# Issue 43: Strip horizontal computed timestamp (moved to footer)
# =========================================================================

def test_issue43_empire_stats_strip_has_no_computed_timestamp() -> None:
    scorecard = {
        'loop': {'integrations': 42},
        'computed_at_utc': '2026-08-24T12:00:00Z',
    }
    html = tv.build_empire_stats_strip(scorecard)
    assert 'empire-computed' not in html
    assert 'computed 2026' not in html.lower()


def test_issue43_footer_has_computed_timestamp() -> None:
    scorecard = {
        'loop': {'integrations': 42},
        'computed_at_utc': '2026-08-24T12:00:00Z',
    }
    html = tv.render_page({'scorecard': scorecard, '_newest_source_age_seconds': 100}, host='testhost', generated_at='2026-08-24 12:30:00')
    assert '<footer class="page-footer">' in html
    assert 'footer-computed' in html
    assert 'scorecard computed 2026-08-24 12:00:00' in html


def test_issue43_render_page_omits_scorecard_computed_when_missing() -> None:
    scorecard = {'loop': {'integrations': 42}}
    html = tv.render_page({'scorecard': scorecard, '_newest_source_age_seconds': 100}, host='testhost', generated_at='2026-08-24 12:30:00')
    assert '<footer class="page-footer">' in html
    assert '<span class="footer-computed">' not in html
    assert 'scorecard computed' not in html


# =========================================================================
# Issue 44: Research mint column right border + no era-grid-line
# =========================================================================

def test_issue44_mint_column_has_right_border_rect() -> None:
    portfolio = {
        'current': 'root',
        'nodes': {
            'root': {'title': 'Root', 'status': 'completed', 'next': []},
        },
    }
    html = tv.build_tech_canvas(portfolio=portfolio, evolution_tree=None, ledger_tail=None)
    assert 'mint-col-border' in html


def test_issue44_canvas_has_no_era_grid_lines() -> None:
    portfolio = {
        'current': 'root',
        'nodes': {
            'root': {'title': 'Root', 'status': 'completed', 'next': []},
        },
    }
    html = tv.build_tech_canvas(portfolio=portfolio, evolution_tree=None, ledger_tail=None)
    assert 'era-grid-line' not in html


# =========================================================================
# Issue 45: Hypotheses lifecycle collapsible section
# =========================================================================

def test_issue45_answered_hypotheses_in_details_element() -> None:
    hl = {
        'entries': {
            'H1': {
                'title': 'Active Hypo',
                'status': 'exploring',
                'created_at': '2026-08-20T00:00:00Z',
            },
            'H2': {
                'title': 'Accepted Hypo',
                'status': 'accepted',
                'answered_at': '2026-08-22T00:00:00Z',
                'evidence_cycle': 'cycle-100',
            },
        }
    }
    html = tv.build_hypotheses_panel(hypotheses_lifecycle=hl, feed_cycles={'cycle-100'})
    assert '<details' in html
    assert '<summary' in html
    assert 'Answered (1)' in html
    assert 'Active Hypo' in html
    assert 'Accepted Hypo' in html


def test_issue45_no_answered_hypotheses_no_details() -> None:
    hl = {
        'entries': {
            'H1': {
                'title': 'Active Hypo',
                'status': 'exploring',
                'created_at': '2026-08-20T00:00:00Z',
            },
        }
    }
    html = tv.build_hypotheses_panel(hypotheses_lifecycle=hl)
    assert '<details' not in html
    assert 'Active Hypo' in html


# =========================================================================
# Issue 47: Feed integrated cycles have tree links
# =========================================================================

def test_issue47_feed_integrated_cycle_has_tree_link() -> None:
    ledger_tail = [
        {'phase': 'outcome', 'cycle_id': 'cycle-evo1', 'status': 'success'},
    ]
    evolution_tree = {
        'nodes': {
            'sha1234567890abcdef': {
                'cycle_id': 'cycle-evo1',
                'branch': 'selfevo/cycle-evo1',
                'parent_sha': None,
            }
        }
    }
    html = tv.build_cycle_feed(
        ledger_tail=ledger_tail,
        demand_completed=None,
        task_titles=None,
        evolution_tree=evolution_tree,
        cycle_files=None,
    )
    assert 'feed-tree-link' in html
    assert 'href="#node-sha1234"' in html


def test_issue47_feed_non_integrated_cycle_has_no_tree_link() -> None:
    ledger_tail = [
        {'phase': 'outcome', 'cycle_id': 'cycle-fail1', 'status': 'failed', 'reason': 'syntax error'},
    ]
    evolution_tree = {
        'nodes': {}
    }
    html = tv.build_cycle_feed(
        ledger_tail=ledger_tail,
        demand_completed=None,
        task_titles=None,
        evolution_tree=evolution_tree,
        cycle_files=None,
    )
    assert 'feed-tree-link' not in html


# =========================================================================
# Issue 48: Multi-column agent panel with Skills table
# =========================================================================

def test_issue48_agent_panel_multi_column_with_skills_table() -> None:
    agents_md = '# AGENTS.md content\nInstruction details here.'
    goal_text = {'charter': 'Improve test coverage and reduce cycle time'}
    skill_reads = {
        'reads': [
            {'skill': 'test-runner'},
            {'skill': 'test-runner'},
            {'skill': 'git-tools'},
        ]
    }
    html = tv.build_agent_panel(
        agents_md=agents_md,
        goal_text=goal_text,
        skill_reads=skill_reads,
    )
    assert 'agent-grid' in html
    assert 'agent-subcol' in html
    assert 'skills-table' in html
    assert 'test-runner' in html
    assert 'git-tools' in html
    assert 'agents-md-box' in html
    assert 'goal-text-box' in html


# ---------------------------------------------------------------------------
# Batch 3: issues #43 #44 #45 #47 #48 (UX pass)
# ---------------------------------------------------------------------------


def _batch3_page() -> str:
    return tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')


def test_batch3_issue43_no_era_grid_lines_or_dead_grid_code() -> None:
    html_out = _batch3_page()
    assert 'era-grid-line' not in html_out


def test_batch3_issue43_canvas_jump_link_present_and_targets_current_node() -> None:
    html_out = _batch3_page()
    assert 'canvas-jump' in html_out
    assert 'href="#node-child1s"' in html_out  # short_sha of current_sha


def test_batch3_issue43_canvas_jump_omitted_without_current_node() -> None:
    data = _fixture()
    data['evolution_tree'] = {'nodes': {}}
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'class="canvas-jump"' not in html_out


def test_batch3_issue43_date_range_label_present() -> None:
    html_out = _batch3_page()
    assert 'time range:' in html_out
    assert 'Aug 15 - Aug 17' in html_out


def test_batch3_issue44_no_nested_scroll_on_feed_and_hypo_lists() -> None:
    html_out = _batch3_page()
    assert 'max-height: 420px' not in html_out
    assert 'max-height: 320px' not in html_out
    assert 'max-height: 250px' not in html_out
    assert 'overscroll-behavior: contain' in html_out


def test_batch3_issue44_charter_boxes_are_details_with_line_counts() -> None:
    html_out = _batch3_page()
    assert '<details class="charter-details agents-md-box">' in html_out
    assert '<details class="charter-details goal-text-box">' in html_out
    assert 'AGENTS.md charter (3 lines)' in html_out
    assert 'Goals charter (1 lines)' in html_out


def test_batch3_issue45_many_files_expandable_few_files_plain() -> None:
    data = _fixture()
    data['cycle_files'] = {
        'cycle-a': ['a.py', 'b.py', 'c.py', 'd.py', 'e.py'],
        'cycle-failed-1': ['one.py'],
    }
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert '<details class="feed-files">' in html_out
    assert '+4 more' in html_out
    assert '<ul class="feed-files-list">' in html_out
    assert '<li>e.py</li>' in html_out
    # single-file row stays a plain div, no title-attr-only access
    assert '<div class="feed-files">' in html_out
    assert 'feed-files" title=' not in html_out


def test_batch3_issue47_panel_nav_links_to_all_sections() -> None:
    html_out = _batch3_page()
    for pid in ('panel-now', 'panel-lineage', 'panel-feed', 'panel-hypotheses', 'panel-agent'):
        assert f'id="{pid}"' in html_out
        assert f'href="#{pid}"' in html_out
    assert '<nav class="panel-nav" aria-label="Sections">' in html_out


def test_batch3_issue47_copyable_ids_and_inline_script_no_external_urls() -> None:
    html_out = _batch3_page()
    assert 'feed-cid copyable' in html_out
    assert 'evo-sha copyable' in html_out
    assert 'navigator.clipboard' in html_out
    assert 'https://' not in html_out.replace('http-equiv', '')


def test_batch3_issue48_meta_refresh_and_freshness_badge() -> None:
    data = _fixture()
    data['_newest_source_age_seconds'] = 120.0
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert '<meta http-equiv="refresh" content="600">' in html_out
    assert 'freshness-fresh' in html_out
    assert 'data-age-seconds="120"' in html_out
    assert 'data: 2m old' in html_out
    assert 'generated 12:00 UTC' in html_out


def test_batch3_issue48_freshness_unknown_level() -> None:
    data = _fixture()
    data['_newest_source_age_seconds'] = None
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'freshness-unknown' in html_out
    assert 'data: age unknown' in html_out


def test_batch3_issue48_very_stale_level() -> None:
    data = _fixture()
    data['_newest_source_age_seconds'] = 30000.0
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'freshness-very-stale' in html_out


def test_batch3_issue48_header_footer_same_timestamp() -> None:
    data = _fixture()
    data['_newest_source_age_seconds'] = 120.0
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    # footer carries the full timestamp; header badge shows its HH:MM part
    assert 'generated 2026-08-18 12:00:00 UTC' in html_out
    assert 'generated 12:00 UTC' in html_out

# ---------------------------------------------------------------------------
# Batch 4: issues #38 #39 #46 (retro terminal theme, host identity, a11y)
# ---------------------------------------------------------------------------


def test_batch4_issue38_no_empire_branding_or_gold() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'EEEBOT EMPIRE' not in html_out
    assert '# eeebot / tech-tree' in html_out
    assert 'c9a227' not in html_out  # gold accent gone
    assert 'Georgia' not in html_out  # serif headings gone


def test_batch4_issue38_terminal_palette_and_monospace() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    assert '#56d364' in html_out  # terminal green accent
    assert "'Consolas', 'Menlo', 'DejaVu Sans Mono', monospace" in html_out
    assert 'Segoe UI' not in html_out


def test_batch4_issue38_feed_lines_have_terminal_prefix() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    assert ".feed-title::before" in html_out
    assert "content: '>> '" in html_out


def test_batch4_issue39_host_identity_from_agents_md() -> None:
    data = _fixture()
    data['agents_md'] = (
        '# Instance Agent Instructions\n\nYou are the self-evolving agent instance on the `eeepc` host: '
        'i386 Debian 12, 2 GB RAM, Python 3.11.'
    )
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'class="host-identity"' in html_out
    assert 'i386' in html_out
    assert 'Debian 12' in html_out
    assert '2 GB RAM' in html_out
    assert 'Python 3.11' in html_out


def test_batch4_issue39_host_identity_absent_without_data() -> None:
    data = _fixture()
    data['agents_md'] = None
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'class="host-identity"' not in html_out


def test_batch4_issue46_single_h1_and_svg_aria() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    assert html_out.count('<h1') == 1
    assert 'role="img"' in html_out
    assert 'aria-label="Cycle lineage graph' in html_out


def test_batch4_issue46_contrast_colors_replaced() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    # old failing-contrast muted colors must be gone
    for old in ('#4f5a76', '#718096', '#6a7590', '#5c6370'):
        assert old not in html_out


def test_batch4_issue46_translate_no_on_ids() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'feed-cid copyable" translate="no"' in html_out
    assert 'evo-sha copyable" translate="no"' in html_out
    assert 'demand-chip' in html_out and 'translate="no"' in html_out

# ---------------------------------------------------------------------------
# Issue #53: DGM archive-tree presentation (best path, score tint, legend)
# ---------------------------------------------------------------------------


def test_issue53_best_path_edges_highlighted() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    # fixture: current = child1, ancestry root -> child1; root -> child2 is off-path
    assert 'evo-elbow-best' in html_out
    # exactly one highlighted edge (the second occurrence is the CSS rule)
    assert html_out.count('class="evo-elbow evo-elbow-best"') == 1


def test_issue53_star_marker_on_current_node_only() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    assert '&#9733;' in html_out
    assert html_out.count('&#9733;') == 1


def test_issue53_score_tint_and_legend_present_with_rewards() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'scoregrad' in html_out
    assert 'score 0.4' in html_out
    assert 'score 0.92' in html_out
    assert 'border-color:hsl(' in html_out  # scored node inline tint
    assert 'no node scores recorded yet' not in html_out


def test_issue53_no_rewards_muted_note_neutral_boxes() -> None:
    data = _fixture()
    for node in data['evolution_tree']['nodes'].values():
        node.pop('fitness', None)
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'no node scores recorded yet' in html_out
    assert 'scoregrad' not in html_out
    assert 'border-color:hsl(' not in html_out
    # best-path highlight is independent of rewards
    assert 'evo-elbow-best' in html_out
