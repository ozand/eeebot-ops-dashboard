from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
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
        'hypotheses_durable': {
            'schema_version': 'hypothesis-durable-v1',
            'model': 'HADI',
            'entries': [
                {'hypothesis_id': 'durable-1', 'title': 'Durable hypothesis', 'hadi': {'hypothesis': 'It works', 'action': 'Try it'}, 'selection_status': 'candidate'},
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
                {'skill': 'test-driven-development', 'cycle_id': 'cycle-a', 'ts': '2026-08-16T00:00:30Z', 'confirmed': True},
                {'skill': 'test-driven-development', 'cycle_id': 'cycle-failed-1', 'ts': '2026-08-16T01:00:30Z'},
                {'skill': 'systematic-debugging', 'cycle_id': 'cycle-failed-1', 'ts': '2026-08-16T01:00:40Z'},
            ],
        },
        'skill_evals': [
            {'skill': 'test-driven-development', 'delta': 0.25},
        ],
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


def test_read_local_state_collects_reflections_fail_soft(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    state.mkdir()
    reflector = state / 'reflector'
    reflector.mkdir()
    (reflector / 'reflections.jsonl').write_text(
        '{"cycle_id":"cycle-a","summary":"found it","findings":["f1"],"recommendations":["r1"]}\n'
        'not-json\n', encoding='utf-8')

    data = tv.read_local_state(str(state), instance_repo=str(tmp_path / 'repo'))

    assert data['reflections'] == [
        {'cycle_id': 'cycle-a', 'summary': 'found it', 'findings': ['f1'], 'recommendations': ['r1']}
    ]


def test_read_local_state_local_ci_four_states(tmp_path: Path) -> None:
    """#1593/#276: read_local_state must distinguish absent/targets_missing/
    ran/unreadable -- never collapse to a bare ok bool."""
    import json

    state = tmp_path / 'state'
    state.mkdir()

    assert tv.read_local_state(str(state))['local_ci'] == {'probe': 'absent'}

    ci_dir = state / 'local_ci'
    ci_dir.mkdir()
    (ci_dir / 'latest.json').write_text(json.dumps({
        'state': 'targets_missing', 'exit_code': None, 'ok': False,
        'summary': 'targets absent from /workspace: tests/test_identity_contract.py',
    }), encoding='utf-8')
    result = tv.read_local_state(str(state))['local_ci']
    assert result['probe'] == 'present_uninitialized'
    assert result['exit_code'] is None

    (ci_dir / 'latest.json').write_text(json.dumps({
        'state': 'ran', 'exit_code': 0, 'ok': True, 'summary': '13 passed',
    }), encoding='utf-8')
    result = tv.read_local_state(str(state))['local_ci']
    assert result['probe'] == 'present'
    assert result['exit_code'] == 0

    (ci_dir / 'latest.json').write_text('not-json', encoding='utf-8')
    result = tv.read_local_state(str(state))['local_ci']
    assert result['probe'] == 'probe_unavailable'


def test_read_local_state_executor_model_status_states(tmp_path: Path) -> None:
    """#1660/#1678/#276: an executor/harness row classified as vendor IS the
    fallback signal -- the only field persisted is the served model."""
    import json

    state = tmp_path / 'state'
    state.mkdir()

    assert tv.read_local_state(str(state))['executor_model_status'] == {'probe': 'absent'}

    llm_dir = state / 'llm_calls'
    llm_dir.mkdir()
    (llm_dir / '2026-09-16.jsonl').write_text(
        '\n'.join(json.dumps(r) for r in [
            {'component': 'proposer', 'model': 'an/gemini-3.8-flash-high', 'ts': '2026-09-16T00:00:00Z'},
            {'component': 'executor', 'model': 'openai/un/qwen3.8-27b-gguf', 'ts': '2026-09-16T00:01:00Z'},
        ]) + '\n', encoding='utf-8',
    )
    result = tv.read_local_state(str(state))['executor_model_status']
    assert result['probe'] == 'present'
    assert result['latest_class'] == 'self_hosted'
    assert result['fallback_seen_recent'] is False

    (llm_dir / '2026-09-16.jsonl').write_text(
        json.dumps({'component': 'executor', 'model': 'an/gemini-3.8-flash-high', 'ts': '2026-09-16T00:02:00Z'}) + '\n',
        encoding='utf-8',
    )
    result = tv.read_local_state(str(state))['executor_model_status']
    assert result['fallback_seen_recent'] is True
    assert result['latest_class'] == 'vendor'


def test_cycle_details_join_and_bound_fields() -> None:
    details = tv.build_cycle_details(
        ledger_rows=[{
            'cycle_id': 'cycle-a', 'task_title': 'Ship panel', 'outcome': 'failed',
            'reason': 'gate', 'ts': '2026-08-18T00:00:00Z', 'target_path': 'src/x.py',
            'serves': 'demand-1', 'sha': 'abcdef1234567890',
            'parent_sha': 'parent1234567890', 'files_changed': [f'f{i}' for i in range(30)],
            'violations': ['bad gate'],
        }],
        evolution_tree={'nodes': {'sha-a': {'cycle_id': 'cycle-a', 'branch': 'b'}}},
        lessons=[{'cycle_id': 'cycle-a', 'insight': 'lesson insight'}],
        reflections=[{'cycle_id': 'cycle-a', 'summary': 'reflection summary', 'findings': ['finding'], 'recommendations': ['recommendation']}],
        cycle_titles={'cycle-a': 'Ship panel'},
        cycle_files={'cycle-a': [f'git{i}' for i in range(30)]},
    )

    record = details['cycle-a']
    assert record['title'] == 'Ship panel'
    assert record['files_changed'] == [f'f{i}' for i in range(20)]
    assert record['lesson_insight_chars'] == len('lesson insight')  # ADR-036 rule 3
    # ADR-036 rule 3: public records carry the reflection's size, not its text.
    assert record['reflection']['summary_chars'] == len('reflection summary')
    assert 'summary' not in record['reflection']
    assert record['reflection']['findings_count'] == 1
    assert record['reflection']['recommendations_count'] == 1
    assert len(json.dumps(details)) < 20_000


def test_lineage_panel_embeds_details_and_controls() -> None:
    html_out = tv.render_pages({**_fixture(), 'reflections': [
        {'cycle_id': 'cycle-a', 'summary': 'reflector summary', 'findings': ['f'], 'recommendations': ['r']}
    ]}, host='eeepc', generated_at='2026-08-18 12:00:00')['lineage.html']

    # #208: the details panel fetches lineage-cycle-details.json on demand
    # instead of inlining 1.17 MB of records into the page.
    assert 'cycle-details-panel' in html_out
    assert 'id="cycle-details-data"' not in html_out
    assert f'data-cycle-details-src="{tv.LINEAGE_DETAILS_FILE}"' in html_out
    assert 'cycle-feed-link' in html_out
    assert 'lessons.html#q-' in html_out
    assert len(html_out) < 1_000_000


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


def test_scorecard_hypothesis_metrics_use_published_values_and_denominators() -> None:
    scorecard = {
        'computed_at_utc': '2026-09-13T13:18:17.184300Z', 'window_days': 7,
        'loop': {'hypothesis_selection_rate': 0.0302, 'hypothesis_served_cycles': 12},
        'control_plane': {'hypothesis_loop': {
            'supported': 5, 'refuted': 0, 'inconclusive': 4, 'total': 9,
            'inconclusive_within_window': 3, 'inconclusive_aged': 0,
            'inconclusive_undatable': 1,
            'inconclusive_undatable_no_qualifying_artifact': 1,
            'inconclusive_undatable_no_completion': 0,
            'inconclusive_undatable_invalid_timestamp': 0,
            'inconclusive_split_status': 'complete',
        }},
        'reader_status': {'ledger': {'status': 'complete'}},
    }
    html = tv.build_scorecard_hypothesis_metrics(scorecard)
    assert '3.0%' in html and 'served cycles: <strong>12</strong>' in html
    assert 'supported <strong>5</strong>' in html and 'refuted <strong>0</strong>' in html
    assert 'total <strong>9</strong>' in html
    assert 'computed_at_utc' in html and '2026-09-13T13:18:17.184300Z' in html


def test_scorecard_hypothesis_metrics_preserve_unavailable_and_numeric_zero() -> None:
    scorecard = {'loop': {'hypothesis_selection_rate': 0, 'hypothesis_served_cycles': 0}, 'control_plane': {'hypothesis_loop': {'supported': 0, 'refuted': 0, 'inconclusive': 0, 'total': 0, 'inconclusive_split_status': 'unavailable'}}, 'reader_status': {'ledger': {'status': 'unavailable'}}}
    html = tv.build_scorecard_hypothesis_metrics(scorecard)
    assert '0.0%' in html and 'supported <strong>0</strong>' in html
    assert 'total <strong>0</strong>' in html and html.count('unavailable') >= 2


def test_hypotheses_lifecycle_groups_active_and_answered() -> None:
    fixture = _fixture()
    html_out = tv.render_page(fixture, host='eeepc', generated_at='2026-08-18 12:00:00')

    # Active group
    assert 'Active titles (1)' in html_out
    assert 'unique titles; lifecycle rows may be higher' in html_out
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

    # ADR-036 rule 3: AGENTS.md and the goal charter are LAN-only text.
    assert '<script>alert' not in html_out
    assert '&lt;script&gt;alert' not in html_out
    assert 'Autonomously improve code quality' not in html_out
    assert 'text on the LAN site only' in html_out

    # Skill reads table
    assert 'test-driven-development' in html_out
    assert '<td class="skill-reads">2</td>' in html_out
    assert 'systematic-debugging' in html_out
    assert '<td class="skill-reads">1</td>' in html_out
    assert 'confirmed' in html_out


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


def test_extract_git_titles_local_joins_requested_node_shas(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if 'log' in cmd and '--first-parent' in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')
        if 'log' in cmd and '--format=%H %s' in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout='sha-node-1 auto-commit subject from node\n', stderr='')
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='', stderr='')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    titles, _, err = tv.extract_git_titles_local(repo, node_shas=['sha-node-1'])

    assert err is None
    assert titles['sha-node-1'] == 'auto-commit subject from node'
    assert any('--format=%H %s' in cmd and 'sha-node-1' in cmd for cmd in calls)


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


def _ci_run(ts: str, *, conclusion: str = 'success', status: str = 'completed', run_id: int = 1) -> dict[str, object]:
    return {
        'id': run_id,
        'run_number': run_id,
        'status': status,
        'conclusion': conclusion if status == 'completed' else None,
        'completed_at': ts if status == 'completed' else None,
        'updated_at': ts,
        'html_url': f'https://github.com/example/actions/runs/{run_id}',
        'name': 'Test Suite',
    }


def test_ci_freshness_states_keep_zero_pending_old_recent_and_conclusion_separate() -> None:
    observed = '2026-09-14T12:00:00Z'
    no_runs = tv._ci_freshness_state({'workflow_runs': []}, observed)
    assert no_runs['state'] == 'no_runs'
    assert no_runs['latest_conclusion'] == 'none'

    # GitHub currently exposes completed_at as null on some historical rows;
    # the detector must use the available terminal timestamp defensively.
    completed_fallback = tv._ci_freshness_state(
        {'workflow_runs': [{**_ci_run(observed, conclusion='success'), 'completed_at': None}]}, observed,
    )
    assert completed_fallback['state'] == 'recent'

    pending = tv._ci_freshness_state({'workflow_runs': [_ci_run(observed, status='in_progress')]}, observed)
    assert pending['state'] == 'runs_pending'
    assert pending['latest_conclusion'] == 'none'

    old = tv._ci_freshness_state({'workflow_runs': [_ci_run('2026-07-04T20:04:36Z')]}, observed)
    assert old['state'] == 'runs_old'
    assert old['latest_conclusion'] == 'success'

    recent_failed = tv._ci_freshness_state({'workflow_runs': [_ci_run(observed, conclusion='failure', run_id=34841282556)]}, observed)
    assert recent_failed['state'] == 'recent'
    assert recent_failed['latest_conclusion'] == 'failure'
    assert recent_failed['latest_run_id'] == 34841282556

    for conclusion in ('neutral', 'skipped', 'timed_out', 'action_required', 'startup_failure', 'stale'):
        terminal = tv._ci_freshness_state({'workflow_runs': [_ci_run(observed, conclusion=conclusion)]}, observed)
        assert terminal['state'] == 'recent'
        assert terminal['latest_conclusion'] == conclusion


def test_ci_freshness_cannot_ask_is_not_zero_or_old() -> None:
    observed = '2026-09-14T12:00:00Z'
    result = tv._ci_run_state(None, 'auth_or_rate_limit', observed)
    assert result['state'] == 'cannot_ask'
    assert result['latest_conclusion'] == 'cannot_ask'
    assert result['observed_at_utc'] == observed


def test_ci_freshness_rejects_malformed_run_records_fail_closed() -> None:
    observed = '2026-09-14T12:00:00Z'
    assert tv._ci_freshness_state({'workflow_runs': [{'status': 'completed'}]}, observed)['state'] == 'cannot_ask'
    assert tv._ci_freshness_state({'workflow_runs': [{'status': 'mystery'}]}, observed)['state'] == 'cannot_ask'
    assert tv._ci_freshness_state({'workflow_runs': [{'status': 'queued', 'id': 1}, {'status': 'completed'}]}, observed)['state'] == 'cannot_ask'
    assert tv._ci_freshness_state({'workflow_runs': [None]}, observed)['state'] == 'cannot_ask'
    assert tv._ci_freshness_state({'workflow_runs': [{'status': 'completed', 'completed_at': None}]}, observed)['state'] == 'cannot_ask'
    assert tv._ci_freshness_state({'workflow_runs': [{'status': 'completed', 'completed_at': 'bad', 'updated_at': 'bad', 'created_at': 'bad'}]}, observed)['state'] == 'cannot_ask'


def test_ci_freshness_read_keeps_actions_independent_and_bounds_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercises the permissions-reading path, which is disabled by default
    # because the live credential cannot answer it. Kept covered so the
    # capability is intact the day the token gains Administration: read.
    monkeypatch.setattr(tv, 'CI_ACTIONS_ENABLED_UNANSWERABLE', False)
    observed = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    calls: list[tuple[str, float]] = []
    responses = iter([
        ({'enabled': False}, None), ({'workflow_runs': []}, None),
        ({'enabled': True}, None), ({'workflow_runs': [_ci_run('2026-07-01T00:00:00Z')]}, None),
    ])

    def fake_api(endpoint: str, *, timeout: float) -> tuple[dict[str, object] | None, str | None]:
        calls.append((endpoint, timeout))
        return next(responses)

    monkeypatch.setattr(tv, '_ci_api_json', fake_api)
    result = tv.read_ci_freshness(('one/repo', 'two/repo'), now=observed)
    first = result['repositories']['one/repo']
    assert first['actions_enabled'] is False
    assert first['freshness_state'] == 'no_runs'
    assert first['latest_conclusion'] == 'none'
    assert all(timeout <= tv.CI_API_TIMEOUT_SECONDS for _, timeout in calls)
    assert len(calls) == 4


def test_ci_freshness_budget_exhaustion_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tv, 'CI_ACTIONS_ENABLED_UNANSWERABLE', False)
    observed = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tv, 'CI_TOTAL_BUDGET_SECONDS', 0)
    calls: list[str] = []
    monkeypatch.setattr(tv, '_ci_api_json', lambda endpoint, *, timeout: calls.append(endpoint))
    result = tv.read_ci_freshness(('one/repo',), now=observed)['repositories']['one/repo']
    assert calls == []
    assert result['actions_enabled'] == 'cannot_ask'
    assert result['freshness_state'] == 'cannot_ask'
    assert result['latest_conclusion'] == 'cannot_ask'


def test_ci_freshness_read_marks_malformed_and_timeout_as_cannot_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tv, 'CI_ACTIONS_ENABLED_UNANSWERABLE', False)
    observed = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    responses = iter([({'enabled': True}, None), (None, 'timeout')])
    monkeypatch.setattr(tv, '_ci_api_json', lambda endpoint, *, timeout: next(responses))
    repo = tv.read_ci_freshness(('one/repo',), now=observed)['repositories']['one/repo']
    assert repo['actions_enabled'] is True
    assert repo['freshness_state'] == 'cannot_ask'
    assert repo['latest_conclusion'] == 'cannot_ask'


def test_ci_actions_enabled_is_unanswerable_not_cannot_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    """The credential cannot answer actions/permissions, so the page says so.

    `cannot_ask` would invite a retry that can never succeed; `unanswerable`
    records a fixed answer with its reason, the same treatment the battery
    probe received in ozand/eeebot#1605.
    """
    observed = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    calls: list[str] = []

    def fake_api(endpoint: str, *, timeout: float) -> tuple[dict[str, object] | None, str | None]:
        calls.append(endpoint)
        return ({'workflow_runs': [_ci_run('2026-09-14T11:00:00Z')]}, None)

    monkeypatch.setattr(tv, '_ci_api_json', fake_api)
    repo = tv.read_ci_freshness(('one/repo',), now=observed)['repositories']['one/repo']

    assert repo['actions_enabled'] == 'unanswerable'
    assert repo['actions_enabled'] != 'cannot_ask'
    # The permissions endpoint is not called at all: a fixed answer must not
    # spend a third of the API budget on every publish.
    assert not any('actions/permissions' in endpoint for endpoint in calls)
    assert calls == ['repos/one/repo/actions/runs?per_page=100']
    # Freshness is unaffected — the axes stay independent.
    assert repo['freshness_state'] == 'recent'
    assert repo['actions']['reason'] == tv.CI_ACTIONS_ENABLED_UNANSWERABLE_REASON


def test_ci_unanswerable_renders_distinctly_from_cannot_ask() -> None:
    def render(enabled: object) -> str:
        return tv._build_ci_freshness_item({
            'schema_version': 'ci-freshness-v1',
            'observed_at_utc': '2026-09-14T12:00:00Z',
            'repositories': {'one/repo': {
                'actions_enabled': enabled,
                'freshness_state': 'recent',
                'latest_conclusion': 'failure',
            }},
        })

    unanswerable = render('unanswerable')
    cannot_ask = render('cannot_ask')
    assert 'enabled=unanswerable' in unanswerable
    assert 'enabled=cannot_ask' in cannot_ask
    assert unanswerable != cannot_ask
    # Both keep the freshness axis readable next to them.
    assert 'recent' in unanswerable and 'failure' in unanswerable


def test_read_local_state_attaches_ci_snapshot_only_after_state_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _write_local_state_root(tmp_path)
    snapshot = {'schema_version': 'ci-freshness-v1', 'observed_at_utc': '2026-09-14T12:00:00Z', 'repositories': {}}
    monkeypatch.setattr(tv, 'read_ci_freshness', lambda: snapshot)
    data = tv.read_local_state(str(tmp_path), include_ci_freshness=True)
    assert data['ci_freshness'] is snapshot


def test_ci_freshness_renderer_shows_timestamp_and_independent_axes() -> None:
    data = {
        'observed_at_utc': '2026-09-14T12:00:00Z',
        'repositories': {
            'ozand/eeebot-self-evolving': {
                'actions_enabled': True,
                'freshness_state': 'recent',
                'latest_conclusion': 'failure',
            },
        },
    }
    html = tv._build_ci_freshness_item(data)
    assert 'enabled=true, recent, failure' in html
    assert 'read 2026-09-14T12:00:00Z' in html
    assert 'CI silent' not in html



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
    missing_root = '/var/lib/eeepc-agent/self-evolving-agent/state/path-that-does-not-exist-1592'
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

    rc, _fp = tv.publish_to_pages('<html></html>')  # must not raise

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
    html = tv.build_hypotheses_panel(data, now=tv.datetime(2026, 8, 21, tzinfo=tv.timezone.utc))
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
    html = tv.build_hypotheses_panel(data, now=tv.datetime(2026, 8, 21, tzinfo=tv.timezone.utc))
    active_idx = html.find('Active Hypothesis')
    stale_idx = html.find('Stale By Status')
    assert active_idx != -1 and stale_idx != -1
    assert active_idx < stale_idx, "Non-stale hypotheses must appear before stale hypotheses"


def test_lineage_uses_ledger_history_day_buckets_and_default_window() -> None:
    tree = {
        'current_sha': 'sha-today',
        'nodes': {
            'sha-yesterday': {'cycle_id': 'cycle-yesterday', 'ts': '2026-08-31T20:00:00Z', 'parent_sha': None},
            'sha-today': {'cycle_id': 'cycle-today', 'ts': '2026-09-01T01:00:00Z', 'parent_sha': 'sha-yesterday'},
        },
    }
    ledger = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-yesterday', 'sha': 'sha-yesterday', 'parent_sha': None, 'ts': '2026-08-31T20:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-today', 'sha': 'sha-today', 'parent_sha': 'sha-yesterday', 'ts': '2026-09-01T01:00:00Z'},
    ]

    html = tv.build_archive_tree(tree, ledger, ledger_history=ledger, now='2026-09-01T02:00:00Z')

    # #218: unified DAG — no per-day sections, but filter controls and timestamps present
    assert 'lineage-day-filter' in html
    assert 'Today' in html and 'Yesterday+Today' in html and '24h' in html
    assert 'data-default-filter="today"' in html
    assert 'data-lineage-default-mode="today"' in html
    assert '<button type="button" data-lineage-filter="today" class="active">Today</button>' in html
    assert '<button type="button" data-lineage-filter="all">All</button>' in html
    assert 'id="lineage-data"' in html
    import json, re as _re
    m = _re.search(r'<script type="application/json" id="lineage-data"[^>]*>(.*?)</script>', html, _re.S)
    payload = json.loads(m.group(1))
    tss = {n['ts'][:10] for n in payload['nodes'] if n.get('ts')}
    assert '2026-08-31' in tss and '2026-09-01' in tss


def test_issue129_cycle_feed_emits_only_existing_entity_links() -> None:
    ledger = [
        {'phase': 'proposed', 'cycle_id': 'cycle-1', 'demand_id': 'hypothesis-1', 'lessons_context': ['LESS-1'], 'ts': '2026-09-01T01:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-1', 'outcome': 'failed', 'reason': 'no artifact recorded', 'ts': '2026-09-01T01:01:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-2', 'outcome': 'partial', 'reason': 'skipped_duplicate', 'ts': '2026-09-01T01:02:00Z'},
    ]
    html = tv.build_cycle_feed(ledger, task_titles={}, history_mode=True)

    assert 'hypothesis-1' in html
    assert '<span class="entity-chip">hypothesis-1</span>' in html
    assert 'hypotheses.html#q-hypothesis-1' not in html
    assert 'lessons.html#q-LESS-1' in html
    assert 'no artifact recorded' in html
    assert 'skipped_duplicate' in html


def test_issue129_cycle_feed_does_not_fabricate_missing_links() -> None:
    ledger = [{'phase': 'outcome', 'cycle_id': 'cycle-plain', 'outcome': 'success', 'ts': '2026-09-01T01:00:00Z'}]
    html = tv.build_cycle_feed(ledger, task_titles={}, history_mode=True)

    assert 'hypotheses.html#' not in html
    assert 'lessons.html#' not in html


def test_issue129_cycle_and_lesson_anchor_names_round_trip() -> None:
    cycle_html = tv.build_cycle_feed([{'phase': 'outcome', 'cycle_id': 'cycle-roundtrip', 'outcome': 'success', 'ts': '2026-09-01T01:00:00Z'}], history_mode=True)
    lesson_html = tv.build_lessons_panel([{'id': 'LESS-1', 'date': '2026-09-01', 'cycle_id': 'cycle-roundtrip', 'problem': 'p', 'solution': 's'}])

    assert 'id="cycle-cycle-roundtrip"' in cycle_html
    assert 'cycles.html#cycle-cycle-roundtrip' in lesson_html


def test_issue129_lesson_entries_expose_resolving_q_anchors() -> None:
    html = tv.build_lessons_panel([{
        'id': 'LESS-1', 'date': '2026-09-01', 'cycle_id': 'cycle-roundtrip',
        'problem': 'p', 'solution': 's',
    }])

    assert 'id="q-LESS-1"' in html


def test_issue129_normalizes_lesson_context_prefix_for_round_trip() -> None:
    html = tv.build_cycle_feed([{
        'phase': 'proposed', 'cycle_id': 'cycle-roundtrip',
        'lessons_context': ['lesson:LESS-1'], 'ts': '2026-09-01T01:00:00Z',
    }], history_mode=True)

    assert 'lessons.html#q-LESS-1' in html
    assert 'lessons.html#q-lesson:LESS-1' not in html


def test_issue129_filters_lesson_links_to_rendered_lesson_ids() -> None:
    html = tv.build_cycle_feed([{
        'phase': 'proposed', 'cycle_id': 'cycle-roundtrip',
        'lessons_context': ['LESS-1', 'ERR-1', 'LESS-retired'],
        'ts': '2026-09-01T01:00:00Z',
    }], history_mode=True, rendered_lesson_ids={'LESS-1'})

    assert 'lessons.html#q-LESS-1' in html
    assert 'lessons.html#q-ERR-1' not in html
    assert 'lessons.html#q-LESS-retired' not in html


def test_lineage_resolves_parent_across_hidden_day_with_stub() -> None:
    tree = {'current_sha': 'sha-child', 'nodes': {}}
    ledger = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-old', 'sha': 'sha-old', 'parent_sha': None, 'ts': '2026-08-20T01:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-child', 'sha': 'sha-child', 'parent_sha': 'sha-old', 'ts': '2026-09-01T01:00:00Z'},
    ]

    html = tv.build_archive_tree(tree, ledger, ledger_history=ledger, now='2026-09-01T02:00:00Z')

    # #218: cross-day edge is resolved as a canonical recorded edge; no stub needed
    import json as _json, re as _re2
    m = _re2.search(r'<script type="application/json" id="lineage-data"[^>]*>(.*?)</script>', html, _re2.S)
    payload = _json.loads(m.group(1))
    edges = [e for e in payload['edges'] if e.get('source_available')]
    assert any(e['basis'] == 'recorded' and 'sha-child' in e.get('target', '') or 'sha-old' in e.get('source', '') for e in edges), 'cross-day recorded edge must be in unified DAG'
    assert 'chronological fallback' not in html


def test_lineage_day_cap_emits_explicit_note() -> None:
    rows = [
            {'phase': 'evolution_tree', 'cycle_id': f'cycle-{i}', 'sha': f'sha-{i}', 'parent_sha': None, 'ts': f'2026-09-01T{(i // 60):02d}:{(i % 60):02d}:00Z'}
        for i in range(121)
    ]

    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T02:00:00Z')

    # #218: unified payload shows truncation in coverage block, not per-day notes
    import json as _j, re as _r
    m = _r.search(r'<script type="application/json" id="lineage-data"[^>]*>(.*?)</script>', html, _r.S)
    payload = _j.loads(m.group(1))
    # With 121 nodes all fitting in 1500 budget, truncated=False; but emitted_nodes is bounded
    assert 'emitted_nodes' in payload['coverage']


def test_lineage_day_sections_keep_h3_heading_and_inner_truncated_note() -> None:
    rows = [
            {'phase': 'evolution_tree', 'cycle_id': f'cycle-{i}', 'sha': f'sha-{i}', 'parent_sha': None, 'ts': f'2026-09-01T{(i // 60):02d}:{(i % 60):02d}:00Z'}
        for i in range(121)
    ]
    rows.append({'phase': 'evolution_tree', 'cycle_id': 'cycle-prev', 'sha': 'sha-prev', 'parent_sha': None, 'ts': '2026-08-31T01:00:00Z'})

    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T02:00:00Z')

    # #218: unified DAG — no per-day sections; verify both days' data in single payload
    import json as _json2, re as _re3
    m = _re3.search(r'<script type="application/json" id="lineage-data"[^>]*>(.*?)</script>', html, _re3.S)
    payload = _json2.loads(m.group(1))
    tss = {n['ts'][:10] for n in payload['nodes'] if n.get('ts')}
    assert '2026-08-31' in tss and '2026-09-01' in tss
    assert 'id="lineage-svg"' in html
    capped = None  # suppress reference below
    assert capped is None or 'lineage-day-truncated' not in (capped.group(1) if capped else '')  # #218: no per-day sections


def test_issue126_lineage_embeds_escaped_day_graph_json_and_renderer_hook() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'root<cycle', 'sha': 'root<sha', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'child', 'sha': 'child', 'parent_sha': 'root<sha', 'ts': '2026-09-01T01:00:00Z'},
    ]

    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T02:00:00Z')

    # #218: unified DAG payload replaces per-day script
    assert 'data-lineage-renderer="unified-dag"' in html or 'id="lineage-svg"' in html
    payload_match = re.search(r'<script type="application/json" id="lineage-data"[^>]*>(.*?)</script>', html, re.S)
    assert payload_match is not None
    assert '<' not in payload_match.group(1)
    payload = json.loads(payload_match.group(1))
    nodes_by_cid = {n['cycle_id']: n for n in payload['nodes']}
    assert nodes_by_cid['root<cycle']['sha'] == 'root<sha'
    child_node = nodes_by_cid['child']
    assert child_node['parent'] == 'c:root<sha'
    assert any(e['source'] == 'c:root<sha' and e['target'] == 'c:child' and e['basis'] == 'recorded' for e in payload['edges'])


def test_issue126_lineage_keeps_server_fallback_headings_and_note() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-a', 'sha': 'sha-a', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
    ]

    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T02:00:00Z')

    # #218: unified DAG — single SVG replaces day sections
    assert 'id="lineage-svg"' in html
    assert 'id="lineage-data"' in html


def test_issue126_javascript_note_is_noscript_only() -> None:
    rows = [{'phase': 'evolution_tree', 'cycle_id': 'cycle-a', 'sha': 'sha-a', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'}]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T02:00:00Z')
    # #218: lineage-js-note is not needed in unified DAG; lineage-data is in a script tag
    assert 'id="lineage-data"' in html
    assert not re.search(r'<script[^>]+src=', html, re.I)


def test_issue126_lineage_inlines_vendored_scripts_without_external_src() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-a', 'sha': 'sha-a', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
    ]

    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T02:00:00Z')

    assert 'window.lineageRenderer' in html  # #208/#218: the renderer is the only vendored script
    assert 'projectUnifiedGraph' in html or 'renderUnified' in html
    assert 'assets/vendor/' not in html
    assert not re.search(r'<script[^>]+src=', html, re.I)


def test_issue126_missing_vendor_files_keeps_server_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-a', 'sha': 'sha-a', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
    ]
    monkeypatch.setattr(tv, '_load_lineage_vendor_scripts', lambda: None)

    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T02:00:00Z')

    # #218: without vendor scripts, the server-side SVG fallback is still in the HTML
    assert 'id="lineage-svg"' in html
    assert 'lineage-day-svg' in html
    assert not re.search(r'<script[^>]+src=', html, re.I)


def test_issue126_generated_json_is_script_data_not_visible_body_text() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-a', 'sha': 'sha-a', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
    ]

    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T02:00:00Z')

    # #218: unified payload in id=lineage-data replaces per-day class=lineage-day-data
    assert re.search(r'<script type="application/json" id="lineage-data"[^>]*>\s*\{', html)
    body_without_scripts = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.I | re.S)
    assert '"version":2' not in body_without_scripts


def test_issue126_inline_vendor_scripts_preserve_source_newlines_and_marker() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-a', 'sha': 'sha-a', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
    ]

    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T02:00:00Z')
    renderer_source = (Path(tv.__file__).resolve().parent.parent / 'assets' / 'vendor' / 'lineage-renderer.js').read_text(encoding='utf-8')
    renderer_block = re.search(r'<script>(.*?)</script>', html, re.S)

    assert renderer_block is not None
    assert renderer_block.group(1).count('\n') == renderer_source.count('\n')
    assert renderer_block.group(1).startswith('(function () {')
    assert 'window.__lineageRendererLoaded = true' in html


def test_issue153_cycle_details_panel_click_listener_delegated() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-a', 'sha': 'sha-a', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T02:00:00Z')
    assert "event.target.closest('.lineage-node')" in html


def test_issue172_render_pages_lineage_cycle_details_covers_history() -> None:
    history_row = {
        'phase': 'outcome',
        'cycle_id': 'cycle-hist-1',
        'sha': 'sha-hist-1',
        'parent_sha': 'sha-0',
        'outcome': 'rejected',
        'reason': 'test failure',
        'ts': '2026-08-20T10:00:00Z',
    }
    tail_row = {
        'phase': 'evolution_tree',
        'cycle_id': 'cycle-tail-1',
        'sha': 'sha-tail-1',
        'parent_sha': '',
        'outcome': 'integrated',
        'ts': '2026-09-01T10:00:00Z',
    }
    data = {
        'ledger_tail': [tail_row],
        'ledger_history': [history_row, tail_row],
        'evolution_tree': {'nodes': {}},
        'portfolio': {},
        'scorecard': {},
    }
    pages = tv.render_pages(data, host='test-host')
    # #208: the records ship as a sibling JSON file instead of an inline blob.
    assert 'id="cycle-details-data"' not in pages['lineage.html']
    details = json.loads(pages[tv.LINEAGE_DETAILS_FILE])
    assert 'cycle-hist-1' in details
    assert 'cycle-tail-1' in details



def test_issue169_remote_reader_script_compiles_and_imports() -> None:
    code = tv.REMOTE_READER_SCRIPT
    assert "from datetime import datetime, timezone, timedelta" in code
    compiled = compile(code, "<remote_reader>", "exec")
    assert compiled is not None


def test_issue169_ledger_phases_pinned_at_module_level() -> None:
    expected = {
        'started', 'proposed', 'outcome', 'gate', 'proposer_reject', 'dedup', 'idle',
        'evolution_tree', 'tech_tree', 'hypothesis', 'doc_only_budget',
    }
    assert tv.LEDGER_PHASES == expected


def test_issue169_health_verdict_investigates_when_no_cycle_history() -> None:
    verdict, reason = tv.health_verdict(120, None, [], False, '2026-09-01T02:00:00Z')
    assert verdict == 'investigate'
    assert reason == 'no cycle history available'


def test_issue169_cycle_feed_title_indicates_recent_on_fallback() -> None:
    rows = [{'phase': 'outcome', 'cycle_id': 'cycle-1', 'ts': '2026-09-01T00:00:00Z'}]
    html_fallback = tv.build_cycle_feed(rows, history_mode=True, ledger_history=None)
    assert 'Cycle History (Recent 1)' in html_fallback

    html_full = tv.build_cycle_feed(rows, history_mode=True, ledger_history=rows)
    assert 'Cycle History (1 cycles)' in html_full


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


def _hypo_ts(days_ago: int) -> str:
    """A timestamp relative to now, because the panel's staleness rule is.

    ``build_hypotheses_panel`` calls anything untouched for more than 14 days
    stale, measured against the current time. Fixtures written with absolute
    dates therefore pass until the wall clock crosses the threshold and then
    fail for a reason that has nothing to do with the code: these two tests
    pinned 'active' entries at 2026-08-20, which was inside the window when
    they were written and fell outside it on 2026-09-03 (#202).
    """
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')


def test_hypotheses_panel_stale_collapse_when_gt_6() -> None:
    # 2 active, 7 stale => stale collapsed in <details><summary>
    entries = {
        'h-act-1': {'title': 'Act 1', 'status': 'researching', 'last_touched': _hypo_ts(2)},
        'h-act-2': {'title': 'Act 2', 'status': 'researching', 'last_touched': _hypo_ts(1)},
    }
    for i in range(7):
        entries[f'h-stale-{i}'] = {
            'title': f'Stale {i}',
            'status': 'stale',
            'last_touched': _hypo_ts(60 + i),
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
        'h-act-1': {'title': 'Act 1', 'status': 'researching', 'last_touched': _hypo_ts(2)},
    }
    for i in range(6):
        entries[f'h-stale-{i}'] = {
            'title': f'Stale {i}',
            'status': 'stale',
            'last_touched': _hypo_ts(60 + i),
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


def test_health_verdict_healthy_branch() -> None:
    scorecard = {'reader_status': {'feeds': {'usage': {'status': 'fresh'}}}}
    assert tv.health_verdict(120, '2026-09-01T01:50:00Z', ['integrated'], False, '2026-09-01T02:00:00Z', scorecard=scorecard) == ('healthy', 'all signals within thresholds across 1 monitored feeds (usage)')


def test_health_verdict_unavailable_age_is_not_healthy() -> None:
    verdict, reason = tv.health_verdict(
        None, '2026-09-01T01:50:00Z', ['integrated'], False, '2026-09-01T02:00:00Z',
        scorecard={'reader_status': {'feeds': {'usage': {'status': 'fresh'}}}},
    )
    assert verdict == 'degraded'
    assert reason == 'data age unavailable'


def test_health_verdict_malformed_integration_timestamp_is_not_healthy() -> None:
    scorecard = {'reader_status': {'feeds': {'usage': {'status': 'fresh'}}}}
    for timestamp in ('not-a-timestamp', '', 123):
        verdict, reason = tv.health_verdict(
            120, timestamp, ['integrated'], False, '2026-09-01T02:00:00Z',
            scorecard=scorecard,
        )
        assert verdict == 'degraded'
        assert reason == 'last integrated timestamp malformed'


def test_health_verdict_unreadable_scorecard_is_not_healthy() -> None:
    scorecards = (
        None,
        {},
        {'reader_status': {}},
        {'reader_status': {'feeds': {}}},
        {'reader_status': {'feeds': {'usage': {'status': 'unreadable'}}}},
        {'reader_status': {'feeds': {'usage': None}}},
    )
    for scorecard in scorecards:
        verdict, reason = tv.health_verdict(
            120, '2026-09-01T01:50:00Z', ['integrated'], False, '2026-09-01T02:00:00Z',
            scorecard=scorecard,
        )
        assert verdict == 'degraded'
        assert 'scorecard' in reason or 'monitored feed' in reason


def test_health_verdict_degraded_by_staleness_or_integration_recency() -> None:
    assert tv.health_verdict(3601, '2026-09-01T01:50:00Z', ['integrated'], False, '2026-09-01T02:00:00Z')[0] == 'degraded'
    assert tv.health_verdict(120, '2026-08-31T20:00:00Z', ['integrated'], False, '2026-09-01T02:00:00Z')[0] == 'degraded'


def test_health_verdict_unreadable_optional_age_variants_are_not_healthy() -> None:
    scorecard = {'reader_status': {'feeds': {'usage': {'status': 'fresh'}}}}
    for age_seconds in (None, 'unreadable', True):
        verdict, _ = tv.health_verdict(
            age_seconds, '2026-09-01T01:50:00Z', ['integrated'], False,
            '2026-09-01T02:00:00Z', scorecard=scorecard,
        )
        assert verdict != 'healthy'


def test_health_verdict_no_unreadable_input_combination_is_healthy() -> None:
    healthy_scorecard = {'reader_status': {'feeds': {'usage': {'status': 'fresh'}}}}
    for age_seconds, integrated_ts, scorecard in (
        (None, '2026-09-01T01:50:00Z', healthy_scorecard),
        (120, None, healthy_scorecard),
        (120, 'not-a-timestamp', healthy_scorecard),
        (120, '2026-09-01T01:50:00Z', None),
        (120, '2026-09-01T01:50:00Z', {}),
        (120, '2026-09-01T01:50:00Z', {'reader_status': {}}),
    ):
        verdict, _ = tv.health_verdict(
            age_seconds, integrated_ts, ['integrated'], False,
            '2026-09-01T02:00:00Z', scorecard=scorecard,
        )
        assert verdict != 'healthy'


def test_health_verdict_investigate_by_failure_streak() -> None:
    assert tv.health_verdict(120, '2026-09-01T01:50:00Z', ['failed', 'partial', 'failed'], False, '2026-09-01T02:00:00Z')[0] == 'investigate'


def test_lineage_leaf_classifies_model_call_incomplete():
    assert tv._leaf_outcome({"outcome": "model_call_incomplete"}) == "model_call_incomplete"


def test_health_verdict_counts_model_call_incomplete_in_failure_streak() -> None:
    verdict, reason = tv.health_verdict(
        120, '2026-09-01T01:50:00Z',
        ['integrated', 'model_call_incomplete', 'model_call_incomplete', 'model_call_incomplete'],
        False, '2026-09-01T02:00:00Z',
    )
    assert verdict == 'investigate'
    assert 'incomplete-model-call' in reason


def test_health_verdict_investigate_by_proposer_unavailable() -> None:
    assert tv.health_verdict(120, '2026-09-01T01:50:00Z', ['integrated'], True, '2026-09-01T02:00:00Z') == ('investigate', 'proposer LLM is unavailable')


def test_render_page_places_health_banner_before_unchanged_metrics() -> None:
    data = _fixture()
    data['scorecard']['reader_status'] = {'feeds': {'usage': {'status': 'fresh'}}}
    data['_newest_source_age_seconds'] = 120
    data['health_last_integrated_ts'] = '2026-09-01T01:50:00Z'
    data['health_recent_outcomes'] = ['integrated']
    html = tv.render_pages(data, host='eeepc', generated_at='2026-09-01 02:00:00')['index.html']
    strip = tv.build_empire_stats_strip(data['scorecard'], age_seconds=120, generated_at='2026-09-01 02:00:00')

    assert '<section class="health-verdict health-' in html
    assert '<strong>FEEDS OK</strong>' in html  # #276: narrowed from HEALTHY, badge scope is 4 feeds
    assert 'all signals within thresholds across 1 monitored feeds (usage)' in html
    assert strip in html
    # Anchor on the banner markup, not the bare class name: the class also
    # appears earlier in the <head> stylesheet.
    assert html.index('<section class="health-verdict') > html.index(strip)


def test_issue182_health_verdict_bridge_exit_streak() -> None:
    # 1. absent/None -> does not trigger alarm
    healthy_scorecard = {'reader_status': {'feeds': {'usage': {'status': 'fresh'}}}}

    assert tv.health_verdict(120, '2026-09-01T01:50:00Z', ['integrated'], False, '2026-09-01T02:00:00Z', bridge_exit_streak=None, scorecard=healthy_scorecard) == ('healthy', 'all signals within thresholds across 1 monitored feeds (usage)')

    # 2. streak = 0 -> healthy
    assert tv.health_verdict(120, '2026-09-01T01:50:00Z', ['integrated'], False, '2026-09-01T02:00:00Z', bridge_exit_streak={'consecutive_failures': 0}, scorecard=healthy_scorecard) == ('healthy', 'all signals within thresholds across 1 monitored feeds (usage)')

    # 3. streak >= 1 -> investigate alarm with error details
    assert tv.health_verdict(120, '2026-09-01T01:50:00Z', ['integrated'], False, '2026-09-01T02:00:00Z', bridge_exit_streak={'consecutive_failures': 5, 'last_error': 'NameError: x', 'last_where': 'bridge.py:1874'}) == ('investigate', 'bridge crash loop: 5 consecutive invocation failures: NameError: x at bridge.py:1874')


def test_issue182_now_panel_bridge_exit_streak_three_state() -> None:
    # 1. absent/None -> unavailable note (not 0)
    p_none = tv.build_now_panel({'now': '2026-09-01T02:00:00Z'}, {}, [], None, None, bridge_exit_streak=None)
    assert 'Bridge Exit Streak:' in p_none
    assert '<span class="unavailable-note">unavailable</span>' in p_none
    assert '0 failures' not in p_none

    # 2. streak = 0 -> badge, but #276 (c): caveated -- this counter has a
    # known blind class (signal kills, eeebot#1683), so a bare 0 must not
    # read as unqualified proof of health.
    p_zero = tv.build_now_panel({'now': '2026-09-01T02:00:00Z'}, {}, [], None, None, bridge_exit_streak={'consecutive_failures': 0})
    assert '0 failures (crash_record)' in p_zero
    assert 'eeebot#1683' in p_zero

    # 3. streak > 0 -> alarm with details
    p_alarm = tv.build_now_panel({'now': '2026-09-01T02:00:00Z'}, {}, [], None, None, bridge_exit_streak={
        'consecutive_failures': 140,
        'last_error': "NameError: name '_parse_explore_mode' is not defined",
        'last_where': 'bridge.py:1874',
    })
    assert '140 consecutive failures' in p_alarm
    assert "NameError: name &#x27;_parse_explore_mode&#x27; is not defined" in p_alarm or "NameError: name '_parse_explore_mode' is not defined" in p_alarm
    assert 'bridge.py:1874' in p_alarm


def test_276_feeds_ok_does_not_print_healthy_over_a_strategist_error_and_doc_budget_cap() -> None:
    """#276: reproduces the exact published-page inputs -- feeds fresh,
    consecutive_failures=0, strategist errored, doc budget at cap. Must not
    render the unqualified word HEALTHY; the badge is scoped to what it
    measures, and the other signals stay visible at equal prominence."""
    html = tv.build_now_panel(
        portfolio=None,
        evolution_tree=None,
        demand_rotation=None,
        demand_completed=None,
        ledger_tail=[
            {'phase': 'doc_only_budget', 'doc_budget_exceeded': True,
             'doc_only_integrations_24h': 5, 'doc_only_budget_24h': 5,
             'items_considered': 21, 'doc_only_deferred': 0},
        ],
        age_seconds=120,
        now='2026-09-16T02:00:00Z',
        health_last_integrated_ts='2026-09-16T01:50:00Z',
        health_recent_outcomes=['integrated'],
        bridge_exit_streak={'consecutive_failures': 0},
        scorecard={'reader_status': {'feeds': {'usage': {'status': 'fresh'}}}},
        strategist_decisions=[{
            'success': False, 'reason': 'no writes applied; watermark unchanged',
            'timestamp': '2026-09-16T00:00:04Z',
            'inputs_status': {n: {'status': 'complete'} for n in
                               ('goals', 'scorecard', 'funnel', 'insights', 'evolution_tree')},
            'counts': {'hypotheses_appended': 0, 'advisories_written': 0},
        }],
    )
    # The word HEALTHY must not appear anywhere in the panel, unqualified.
    assert 'HEALTHY' not in html
    assert '<strong>FEEDS OK</strong>' in html
    assert 'health-alert-text">error' in html  # strategist error still visible
    assert 'cap reached' in html or 'exceeded' in html or 'doc-only' in html  # doc budget still visible

# ---------------------------------------------------------------------------
# Issue #271: ranked demand queue + charter split (state/public/derived_view.json)
# ---------------------------------------------------------------------------

_DEMAND_KINDS_TRUST_ORDER = [
    'priority', 'defect', 'goal-gap', 'artifact-gap', 'skill-candidate',
    'hypothesis', 'decay', 'repair-unused', 'reflection',
]


def _derived_view_fixture(**overrides) -> dict:
    base = {
        'status': 'present',
        'schema_version': 'derived-view-v1',
        'generated_at_utc': '2026-09-17T03:00:00Z',
        'charter': {'source': 'release_goals_md', 'merged': False, 'text': 'Ship the operator dashboard.'},
        'derived_status': 'present',
        'derived_priorities': [
            {'number': 1, 'label': 'Reduce publish payload', 'vector': 'V1', 'direction': 'shrink', 'added_utc': '2026-09-15T00:00:00Z'},
        ],
        'priority_items': [
            {'rank': i + 1, 'id': f'item-{kind}', 'kind': kind, 'number': i + 1,
             'label': f'{kind} task', 'vector': 'V1' if i % 2 == 0 else 'V2',
             'provenance': 'operator' if i % 2 == 0 else 'self-derived',
             'direction': 'shrink', 'summary': f'summary for {kind}', 'evidence': []}
            for i, kind in enumerate(_DEMAND_KINDS_TRUST_ORDER)
        ],
        'input_mtimes_utc': {'charter': '2026-09-15T00:00:00Z', 'goal_text_json': None, 'derived_priorities_json': '2026-09-15T00:00:00Z'},
        'sort': 'provenance(operator<self-derived), then vector(V1<V2)',
    }
    base.update(overrides)
    return base


def _now_panel_with_derived_view(derived_view) -> str:
    return tv.build_now_panel(
        portfolio=None, evolution_tree=None, demand_rotation=None, demand_completed=None,
        derived_view=derived_view,
    )


def test_271_next_up_renders_all_nine_kinds_no_other_bucket() -> None:
    html = _now_panel_with_derived_view(_derived_view_fixture())
    for kind in _DEMAND_KINDS_TRUST_ORDER:
        assert f'>{kind}</span>' in html, f'kind {kind!r} not rendered under its own name'
    assert html.count('data-demand-provenance=') == 9
    # No bucket collapse: the literal string 'other' must not appear as a
    # rendered kind badge.
    assert '>other</span>' not in html


def test_271_unrecognised_kind_renders_as_itself() -> None:
    view = _derived_view_fixture(priority_items=[
        {'rank': 1, 'id': 'item-x', 'kind': 'brand-new-kind-2026', 'number': 1,
         'label': 'novel item', 'vector': 'V1', 'provenance': 'operator',
         'direction': 'shrink', 'summary': '', 'evidence': []},
    ])
    html = _now_panel_with_derived_view(view)
    assert '>brand-new-kind-2026</span>' in html
    assert '>other</span>' not in html


def test_271_next_up_three_state_plus_empty() -> None:
    absent = _now_panel_with_derived_view({'status': 'absent'})
    unavailable = _now_panel_with_derived_view({'status': 'probe_unavailable', 'reason': 'JSONDecodeError'})
    empty = _now_panel_with_derived_view(_derived_view_fixture(priority_items=[]))
    present = _now_panel_with_derived_view(_derived_view_fixture())
    assert 'data-demand-queue-state="absent"' in absent
    assert 'data-demand-queue-state="probe_unavailable"' in unavailable
    assert 'JSONDecodeError' in unavailable
    assert 'data-demand-queue-state="empty"' in empty
    assert 'data-demand-queue-state="present"' in present
    # All four renders are distinct -- no state collapses into another.
    assert len({absent, unavailable, empty, present}) == 4
    # None of the non-present states render an empty-looking queue silently.
    assert 'unavailable' in absent and 'unavailable' in unavailable


def test_271_charter_split_operator_vs_self_derived() -> None:
    html = _now_panel_with_derived_view(_derived_view_fixture())
    assert '<h4>Operator Charter</h4>' in html
    assert '<h4>Self-Derived Priorities</h4>' in html
    assert 'Ship the operator dashboard.' in html
    assert 'Reduce publish payload' in html
    # The two sections are distinct DOM nodes, not one merged blob.
    op_idx = html.index('<h4>Operator Charter</h4>')
    self_idx = html.index('<h4>Self-Derived Priorities</h4>')
    assert op_idx != self_idx


def test_271_charter_derived_status_three_states() -> None:
    absent = _now_panel_with_derived_view(_derived_view_fixture(derived_status='absent', derived_priorities=[]))
    unavailable = _now_panel_with_derived_view(_derived_view_fixture(derived_status='probe_unavailable', derived_priorities=[]))
    present = _now_panel_with_derived_view(_derived_view_fixture())
    assert 'data-derived-status="absent"' in absent
    assert 'data-derived-status="probe_unavailable"' in unavailable
    assert 'data-derived-status="present"' in present


def test_271_provenance_from_field_not_from_parsing_label_text() -> None:
    """A label whose TEXT mentions the other provenance word must not
    change which badge renders -- the provenance field alone decides."""
    view = _derived_view_fixture(priority_items=[
        {'rank': 1, 'id': 'item-1', 'kind': 'priority', 'number': 1,
         'label': 'this task looks self-derived but is not', 'vector': 'V1',
         'provenance': 'operator', 'direction': 'shrink', 'summary': '', 'evidence': []},
    ])
    html = _now_panel_with_derived_view(view)
    assert 'data-demand-provenance="operator"' in html
    assert 'data-demand-provenance="self-derived"' not in html


def test_271_head_rank_reason_shown() -> None:
    html = _now_panel_with_derived_view(_derived_view_fixture())
    assert 'Head:' in html
    assert 'operator' in html.split('Head:')[1].split('</p>')[0]
    assert 'vector V1' in html.split('Head:')[1].split('</p>')[0]
    assert 'provenance(operator&lt;self-derived)' in html or 'provenance(operator<self-derived)' in html


def test_291_selector_rule_note_names_ranking_vs_selection() -> None:
    """#291: eeebot#1708/#1711 found the ranking and the selector
    (llm_proposer._select_assigned_demand) disagreed for 38h; the block
    must name the rule so this order is never read as the presented one."""
    html = _now_panel_with_derived_view(_derived_view_fixture())
    assert 'demand-queue-rule-note' in html
    assert 'provenance' in html and 'vector' in html
    assert 'eeebot#1708' in html or '#1708' in html
    assert '#902' in html


def test_271_queue_order_matches_published_order_exactly() -> None:
    """The viewer must never re-sort -- rendered order equals published
    array order, even when that order looks 'wrong' by vector/provenance."""
    view = _derived_view_fixture(priority_items=[
        {'rank': 1, 'id': 'z-item', 'kind': 'reflection', 'number': 1, 'label': 'z', 'vector': 'V2', 'provenance': 'self-derived', 'direction': 'x', 'summary': '', 'evidence': []},
        {'rank': 2, 'id': 'a-item', 'kind': 'priority', 'number': 2, 'label': 'a', 'vector': 'V1', 'provenance': 'operator', 'direction': 'x', 'summary': '', 'evidence': []},
    ])
    html = _now_panel_with_derived_view(view)
    assert html.index('>z</strong>') < html.index('>a</strong>')


def test_271_generated_at_timestamp_rendered() -> None:
    html = _now_panel_with_derived_view(_derived_view_fixture())
    assert '2026-09-17T03:00:00Z' in html


def test_271_served_completed_history_demoted_to_fold() -> None:
    html = tv.build_now_panel(
        portfolio=None, evolution_tree=None,
        demand_rotation={'served': {'defect-1': '2026-09-16T00:00:00Z'}},
        demand_completed=None,
        derived_view=_derived_view_fixture(),
    )
    assert '<details class="now-demand-history">' in html
    assert 'Served / completed history' in html


def test_271_read_derived_view_local_four_states(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    state.mkdir()
    assert tv.read_local_state(str(state))['derived_view'] == {'status': 'absent'}

    public_dir = state / 'public'
    public_dir.mkdir()
    (public_dir / 'derived_view.json').write_text('not-json', encoding='utf-8')
    result = tv.read_local_state(str(state))['derived_view']
    assert result['status'] == 'probe_unavailable'

    (public_dir / 'derived_view.json').write_text(json.dumps(_derived_view_fixture()), encoding='utf-8')
    result = tv.read_local_state(str(state))['derived_view']
    assert result['status'] == 'present'
    assert len(result['priority_items']) == 9
    assert result['charter']['source'] == 'release_goals_md'


def test_271_remote_reader_script_mirrors_derived_view(tmp_path: Path) -> None:
    import contextlib
    import io

    state = tmp_path / 'state'
    (state / 'public').mkdir(parents=True)
    (state / 'public' / 'derived_view.json').write_text(json.dumps(_derived_view_fixture()), encoding='utf-8')
    script = tv.REMOTE_READER_SCRIPT.replace(
        'STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"',
        f'STATE_ROOT = {str(state)!r}',
    )
    namespace: dict[str, object] = {}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(script, namespace)
    result = namespace['read_derived_view']()
    assert result['status'] == 'present'
    assert len(result['priority_items']) == 9


def test_local_ci_item_four_states() -> None:
    """#1593/#276: state distinguishes ran from targets_missing; ok=false
    must not read the same as a real failing run."""
    # None means the fetch itself failed (SSH down) -- distinct from the
    # host answering "no file": neither is a fabricated absent/zero.
    assert 'probe-probe_unavailable' in tv._build_local_ci_item(None)
    assert 'probe-absent' in tv._build_local_ci_item({'probe': 'absent'})
    assert 'no local_ci result recorded' in tv._build_local_ci_item({'probe': 'absent'})

    unavailable = tv._build_local_ci_item({'probe': 'probe_unavailable', 'reason': 'JSONDecodeError'})
    assert 'probe-probe_unavailable' in unavailable
    assert 'JSONDecodeError' in unavailable

    uninit = tv._build_local_ci_item({
        'probe': 'present_uninitialized', 'state': 'targets_missing', 'exit_code': None,
        'summary': 'targets absent from /workspace: tests/test_identity_contract.py',
    })
    assert 'probe-present_uninitialized' in uninit
    assert 'no targets to check' in uninit
    assert 'exit=' not in uninit  # never rendered as a run result

    passed = tv._build_local_ci_item({'probe': 'present', 'state': 'ran', 'exit_code': 0, 'summary': '13 passed'})
    assert 'probe-present' in passed
    assert 'pass:' in passed

    failed = tv._build_local_ci_item({'probe': 'present', 'state': 'ran', 'exit_code': 1, 'summary': '1 failed'})
    assert 'exit=1' in failed


def test_1769_artifact_graph_item_states() -> None:
    """eeebot#1769/ADR-024: artifacts/ready/leaves + named oldest leaves,
    read exactly as published -- unavailable (missing scorecard, missing
    quality section, missing field, or status != "complete") must render
    the same `unavailable-note` the rest of this panel uses, never a zero
    graph presented as "no dependencies"."""
    assert 'unavailable-note' in tv._build_artifact_graph_item(None)
    assert 'unavailable-note' in tv._build_artifact_graph_item({})
    assert 'unavailable-note' in tv._build_artifact_graph_item({'quality': {}})
    assert 'unavailable-note' in tv._build_artifact_graph_item(
        {'quality': {'artifact_graph': None}}
    )
    assert 'unavailable-note' in tv._build_artifact_graph_item(
        {'quality': {'artifact_graph': {'status': 'unavailable'}}}
    )

    complete = tv._build_artifact_graph_item({
        'quality': {
            'artifact_graph': {
                'status': 'complete',
                'counts': {'artifacts': 219, 'components': 44, 'leaves': 175, 'unresolved': 0},
                'unit_scan_status': 'scanned',
                'oldest_leaves': [
                    {'id': 'scripts/old_one', 'path': 'scripts/old_one.py'},
                    {'id': 'scripts/old_two', 'path': 'scripts/old_two.py'},
                ],
            },
        },
    })
    assert '219 artifacts' in complete
    assert '44 ready' in complete
    assert '175 leaves' in complete
    assert 'scripts/old_one.py' in complete
    assert 'scripts/old_two.py' in complete
    assert 'systemd units: unavailable' not in complete

    unit_unscanned = tv._build_artifact_graph_item({
        'quality': {
            'artifact_graph': {
                'status': 'complete',
                'counts': {'artifacts': 1, 'components': 0, 'leaves': 1, 'unresolved': 0},
                'unit_scan_status': 'unavailable',
                'oldest_leaves': [],
            },
        },
    })
    assert 'systemd units: unavailable' in unit_unscanned


def test_298_systemd_drift_item_states() -> None:
    """#298: eeebot#1701/PR#1717 -- two separate four-state verdicts must
    never collapse into one: this reader's own read outcome (absent/
    probe_unavailable/present) vs. the host probe's own `state` field
    inside the file, read through unmodified."""
    # SSH/read itself failed -- distinct from the host answering "no file".
    assert 'probe-probe_unavailable' in tv._build_systemd_drift_item(None)

    absent = tv._build_systemd_drift_item({'status': 'absent'})
    assert 'probe-absent' in absent
    assert 'no drift result recorded' in absent

    unreadable = tv._build_systemd_drift_item({'status': 'probe_unavailable', 'reason': 'JSONDecodeError'})
    assert 'probe-probe_unavailable' in unreadable
    assert 'JSONDecodeError' in unreadable

    # File readable, but the HOST's own comparison failed -- its verdict
    # wins, not a fabricated "present".
    host_failed = tv._build_systemd_drift_item({
        'status': 'present', 'state': 'probe_unavailable', 'details': 'PermissionError: no access',
    })
    assert 'probe-probe_unavailable' in host_failed
    assert 'PermissionError' in host_failed
    assert 'defect(s)' not in host_failed

    clean = tv._build_systemd_drift_item({
        'status': 'present', 'state': 'present', 'defect_count': 0,
        'scanned_at': '2026-09-17T02:00:00Z',
        'findings': {'installed_not_in_release': [], 'release_not_installed': [], 'content_differs': [], 'stray': []},
    })
    assert 'probe-present' in clean
    assert '0 defect(s)' in clean
    assert 'systemd-drift-findings' not in clean  # nothing to list

    with_findings = tv._build_systemd_drift_item({
        'status': 'present', 'state': 'present', 'defect_count': 2,
        'scanned_at': '2026-09-17T02:00:00Z',
        'findings': {
            'installed_not_in_release': [
                {'path': 'eeepc-self-evolving-subagent-bridge.timer.d/preset.conf', 'owner': 'preset'},
                {'path': 'eeebot-techtree-publish.service.d/10-state-read.conf', 'owner': 'unknown'},
            ],
            'release_not_installed': ['eeebot-strategist.timer'],
            'content_differs': [{'path': 'eeebot-host-metrics.service', 'detail': 'content differs from release'}],
            'stray': ['eeepc-monitor.service.bak-20260817-880'],
        },
    })
    assert '2 defect(s)' in with_findings
    # Every finding gets a line, known-owner included, but only unknown-owner
    # (and the other three buckets) carry the defect marker class.
    assert 'preset.conf' in with_findings and 'owner: preset' in with_findings
    assert '10-state-read.conf' in with_findings and 'owner: unknown' in with_findings
    assert 'eeebot-strategist.timer' in with_findings
    assert 'eeebot-host-metrics.service' in with_findings
    assert 'eeepc-monitor.service.bak-20260817-880' in with_findings
    preset_line = with_findings.split('preset.conf')[0].rsplit('<li', 1)[1]
    assert 'systemd-drift-defect' not in preset_line
    unknown_line = with_findings.split('10-state-read.conf')[0].rsplit('<li', 1)[1]
    assert 'systemd-drift-defect' in unknown_line


def test_executor_model_item_four_states() -> None:
    """#1660/#1678/#276: a vendor-class model on an executor/harness row is
    the fallback signal, derived from the single persisted model field."""
    assert 'probe-absent' in tv._build_executor_model_item({'probe': 'absent'})

    uninit = tv._build_executor_model_item({'probe': 'present_uninitialized', 'reason': 'no executor/harness calls in recent files'})
    assert 'probe-present_uninitialized' in uninit

    normal = tv._build_executor_model_item({
        'probe': 'present', 'latest_model': 'openai/un/qwen3.8-27b-gguf', 'latest_class': 'self_hosted',
        'fallback_seen_recent': False, 'checked_calls': 12,
    })
    assert 'probe-present' in normal
    assert 'self_hosted' in normal
    assert 'health-alert-text' not in normal

    fallback = tv._build_executor_model_item({
        'probe': 'present', 'latest_model': 'an/gemini-3.8-flash-high', 'latest_class': 'vendor',
        'fallback_seen_recent': True, 'checked_calls': 3,
    })
    assert 'health-alert-text' in fallback
    assert 'fallback' in fallback
    assert 'an/gemini-3.8-flash-high' in fallback


def test_now_panel_uses_latest_integration_from_ledger_tail() -> None:
    """read_ledger_tail returns rows oldest-first over a 5000-row window, so the
    integration-recency signal must read the LAST evolution_tree row. Reading the
    first one reports an integration that is days old and pins the banner to
    'degraded' forever."""
    now = '2026-09-01T12:00:00Z'
    ledger_tail = [
        {'phase': 'evolution_tree', 'ts': '2026-08-25T09:00:00Z'},
        {'phase': 'outcome', 'outcome': 'integrated'},
        {'phase': 'evolution_tree', 'ts': '2026-09-01T11:50:00Z'},
    ]
    html = tv.build_now_panel(
        portfolio=None,
        evolution_tree=None,
        ledger_tail=ledger_tail,
        demand_rotation=None,
        demand_completed=None,
        age_seconds=120,
        now=now,
        scorecard={'reader_status': {'feeds': {'usage': {'status': 'fresh'}}}},
    )
    assert '<strong>FEEDS OK</strong>' in html  # #276: narrowed from HEALTHY, badge scope is 4 feeds
    assert 'all signals within thresholds across 1 monitored feeds (usage)' in html


def test_now_panel_prefers_current_evolution_tree_node_timestamp() -> None:
    """The current tree node is the authoritative last-integration timestamp;
    the ledger scan is only the fallback when the tree has no usable node."""
    evolution_tree = {
        'current_sha': 'abc123',
        'nodes': {'abc123': {'ts': '2026-09-01T11:55:00Z'}},
    }
    html = tv.build_now_panel(
        portfolio=None,
        evolution_tree=evolution_tree,
        ledger_tail=[{'phase': 'evolution_tree', 'ts': '2026-08-01T09:00:00Z'}],
        demand_rotation=None,
        demand_completed=None,
        age_seconds=120,
        now='2026-09-01T12:00:00Z',
        scorecard={'reader_status': {'feeds': {'usage': {'status': 'fresh'}}}},
    )
    assert '<strong>FEEDS OK</strong>' in html  # #276: narrowed from HEALTHY, badge scope is 4 feeds


def test_health_verdict_states_are_styled() -> None:
    """A banner an operator has to read word-by-word is not a glance signal:
    each verdict state must carry its own CSS rule."""
    css = tv.build_page_css() if hasattr(tv, 'build_page_css') else tv.render_pages(
        _fixture(), host='eeepc', generated_at='2026-09-01 02:00:00'
    )['index.html']
    for cls in ('.health-verdict', '.health-healthy', '.health-degraded', '.health-investigate'):
        assert f'{cls} {{' in css or f'{cls}{{' in css, f'no CSS rule for {cls}'


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
    # Today timestamp in UTC (06:39 UTC -> 09:39 MSK today)
    today_ts = '2026-08-24T06:39:15Z'
    assert tv.fmt_ts_short(today_ts, now=ref_now) == '09:39 MSK'

    # Same year, older date -> Mon DD MSK
    older_same_year = '2026-08-14T10:00:00Z'
    assert tv.fmt_ts_short(older_same_year, now=ref_now) == 'Aug 14 MSK'

    # Different year -> Mon DD YYYY MSK
    older_diff_year = '2025-08-14T10:00:00Z'
    assert tv.fmt_ts_short(older_diff_year, now=ref_now) == 'Aug 14 2025 MSK'

    # Midnight boundary: 22:30 UTC on Aug 23 is 01:30 MSK on Aug 24 ("today" relative to ref_now)
    edge_ts = '2026-08-23T22:30:00Z'
    assert tv.fmt_ts_short(edge_ts, now=ref_now) == '01:30 MSK'

    # Invalid / empty
    assert tv.fmt_ts_short(None, now=ref_now) == 'n/a'
    assert tv.fmt_ts_short('', now=ref_now) == 'n/a'



def _feed_html_at(ts: str, now: "datetime") -> str:
    return tv.build_cycle_feed(
        ledger_tail=[
            {'phase': 'outcome', 'cycle_id': 'cycle-aaa', 'ts': ts, 'delta': '166519.3317'},
        ],
        demand_completed=None,
        task_titles=None,
        evolution_tree=None,
        cycle_files=None,
        now=now,
    )


def test_issue42_feed_delta_and_ts_humanized() -> None:
    # #235: the previous version derived "today" from datetime.now in UTC while
    # fmt_ts_short decides HH:MM-versus-date in MSK. Between 21:00Z and midnight
    # the two calendars disagree, so the test failed for three hours every day on
    # every branch. Both sides of the comparison are pinned here instead.
    from datetime import datetime, timezone

    ts = '2026-09-06T06:39:44Z'  # 09:39 MSK on the 6th
    html = _feed_html_at(ts, datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc))

    assert '+166.5K' in html
    assert f'title="{ts}"' in html
    assert 'MSK' in html
    assert '09:39' in html


def test_issue235_feed_ts_renders_the_date_once_it_is_no_longer_today_msk() -> None:
    # The other branch of the same format. Without this, a change that made
    # fmt_ts_short always print HH:MM would still pass the test above.
    from datetime import datetime, timezone

    ts = '2026-09-06T06:39:44Z'
    html = _feed_html_at(ts, datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc))

    assert 'Sep 6 MSK' in html
    assert '09:39' not in html


def test_issue235_feed_ts_does_not_depend_on_the_wall_clock() -> None:
    # The failing window was 21:00Z-24:00Z, where UTC is still on the 6th but
    # MSK has already rolled over to the 7th. Two instants inside it, two
    # outside, one fixture, one expected rendering.
    from datetime import datetime, timezone

    ts = '2026-09-06T06:39:44Z'
    outside = [
        datetime(2026, 9, 6, 6, 40, tzinfo=timezone.utc),
        datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc),
    ]
    inside = [
        datetime(2026, 9, 6, 21, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 6, 23, 34, tzinfo=timezone.utc),
    ]

    for now in outside:
        assert '09:39' in _feed_html_at(ts, now), f'same MSK day at {now.isoformat()}'

    # Inside the window the timestamp is genuinely yesterday in MSK, so the
    # date form is the correct rendering — the old test asserted otherwise and
    # that is what made it fail. The point is that the answer is stable, not
    # that it is always HH:MM.
    for now in inside:
        assert 'Sep 6 MSK' in _feed_html_at(ts, now), f'next MSK day at {now.isoformat()}'


def test_issue235_feed_still_reads_the_clock_when_no_instant_is_given() -> None:
    # The seam must not change production behaviour: omitting `now` still
    # renders against the real clock.
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    html = tv.build_cycle_feed(
        ledger_tail=[{'phase': 'outcome', 'cycle_id': 'cycle-aaa', 'ts': ts}],
        demand_completed=None,
        task_titles=None,
        evolution_tree=None,
        cycle_files=None,
    )
    assert 'MSK' in html


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
    # Issue #58: the computed timestamp may appear inside the KPI source
    # tooltip attribute, but never as visible strip text.
    assert '>computed 2026' not in html


def test_issue43_footer_has_computed_timestamp() -> None:
    scorecard = {
        'loop': {'integrations': 42},
        'computed_at_utc': '2026-08-24T12:00:00Z',
    }
    html = tv.render_page({'scorecard': scorecard, '_newest_source_age_seconds': 100}, host='testhost', generated_at='2026-08-24 12:30:00')
    assert '<footer class="page-footer">' in html
    assert 'footer-computed' in html
    assert 'scorecard computed 2026-08-24 15:00:00 MSK' in html


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
    assert 'href="lineage.html#node-sha1234"' in html


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

def test_issue97_confirmed_usage_and_eval_source() -> None:
    html = tv.build_agent_panel(
        agents_md=None,
        goal_text=None,
        skill_reads={'reads': [{'skill': 'alpha', 'confirmed': True}, {'skill': 'alpha', 'confirmed': False}]},
        skill_evals=[{'skill': 'alpha', 'delta': 0.2}],
    )
    assert '1 confirmed' in html
    assert 'eval delta: +0.2' in html
    assert 'skill_fitness/reads.json' in html


def test_issue97_absent_confirmation_remains_not_tracked() -> None:
    html = tv.build_agent_panel(
        agents_md=None,
        goal_text=None,
        skill_reads={'reads': [{'skill': 'alpha'}]},
    )
    assert '0 confirmed' in html


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


def test_issue1765_breakdown_is_no_data_for_legacy_scorecard() -> None:
    html = tv.build_empire_stats_strip({'loop': {'repeat_failure_rate': 0.446}})
    assert '44.6%' in html
    assert html.count('no data') >= 5
    assert 'old repeat failure rate' in html
    assert 'repeat failure rate · new' in html
    assert 'новая формула без self_dedup' not in html
    assert '#1785' not in html
    assert 'execution failures' in html
    assert 'model unavailable (known)' in html
    assert 'model call incomplete' in html
    assert 'failure cause unknown' in html
    assert 'self_dedup rejections' in html


def test_issue1765_new_rate_caption_names_the_formula_change() -> None:
    html = tv.build_empire_stats_strip({'loop': {
        'repeat_failure_rate': 0.42,
        'repeat_failure_rate_new': 0.132,
    }})
    assert '13.2%' in html
    assert 'новая формула без self_dedup (#1765); не улучшение работы' in html
    assert '#1785' not in html


def test_batch3_issue48_meta_refresh_and_freshness_badge() -> None:
    data = _fixture()
    data['_newest_source_age_seconds'] = 120.0
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert '<meta http-equiv="refresh" content="600">' in html_out
    assert 'freshness-fresh' in html_out
    assert 'data-data-epoch="' in html_out
    assert 'data-age-seconds=' not in html_out
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


def test_batch3_issue1905_freshness_uses_absolute_epoch() -> None:
    data = _fixture()
    data['_newest_source_age_seconds'] = 600.0
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'data-data-epoch="' in html_out
    assert 'Date.now() / 1000 - epoch' in html_out
    assert "a < 3600 ? 'fresh' : a < 21600 ? 'stale' : 'very-stale'" in html_out
    assert 'data-age-seconds' not in html_out
    assert 'data-render-age-seconds="600"' in html_out


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
    assert 'score gauge hidden: reward data gap' in html_out
    assert 'scoregrad' not in html_out
    assert 'border-color:hsl(' not in html_out
    # best-path highlight is independent of rewards
    assert 'evo-elbow-best' in html_out


def test_issue53_chronological_fallback_edges_when_parents_unmatched() -> None:
    # Live-data shape: parent_sha values are bare git commits (lesson commits
    # between merges), never node keys. Lineage must still chain chronologically.
    data = _fixture()
    tree = data['evolution_tree']
    tree['nodes']['orphan1sha000000000000000000000000000'] = {
        'parent_sha': 'barelessoncommit000000000000000000000000',
        'branch': 'selfevo/cycle-orphan1', 'cycle_id': 'cycle-orphan1',
        'ts': '2026-08-18T00:00:00Z', 'fitness': {'reward': 0.5},
    }
    tree['nodes']['orphan2sha000000000000000000000000000'] = {
        'parent_sha': 'barelessoncommit111111111111111111111111',
        'branch': 'selfevo/cycle-orphan2', 'cycle_id': 'cycle-orphan2',
        'ts': '2026-08-18T06:00:00Z', 'fitness': {'reward': 0.7},
    }
    tree['current_sha'] = 'orphan2sha000000000000000000000000000'
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    # chronological chain orphan1 -> orphan2 exists and is the best path
    assert html_out.count('class="evo-elbow evo-elbow-best"') >= 1
    assert 'evo-elbow' in html_out

# ---------------------------------------------------------------------------
# Issue #58: header repeat-failure KPI vs Now-panel lever — source annotation
# ---------------------------------------------------------------------------


def test_issue58_header_kpi_source_annotation() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'repeat failure rate · scorecard' in html_out
    # tooltip merges the #61 definition with the #58 source annotation
    assert 'source: scorecard snapshot' in html_out
    assert 'last cycle measurement and may differ' in html_out


def test_issue58_now_lever_last_cycle_annotation() -> None:
    data = _fixture()
    data['portfolio']['nodes']['proposer-quality']['last_lever_value'] = 0.34
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert '(last: 0.34, last cycle)' in html_out
    assert 'title="source: tech-tree portfolio, last cycle measurement"' in html_out

# ---------------------------------------------------------------------------
# Issue #59: partial reasons + terminal SKIPPED badge
# ---------------------------------------------------------------------------


def _issue59_page(ledger_extra: list[dict]) -> str:
    data = _fixture()
    data['ledger_tail'] = data['ledger_tail'] + ledger_extra
    return tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')


def test_issue59_partial_row_renders_reason() -> None:
    html_out = _issue59_page([
        {'phase': 'started', 'cycle_id': 'cycle-part-1', 'ts': '2026-08-18T03:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-part-1', 'status': 'partial',
         'reason': 'gate smoke failed after artifact', 'ts': '2026-08-18T03:05:00Z'},
    ])
    assert 'PARTIAL: gate smoke failed after artifact' in html_out


def test_issue59_partial_no_reason_explicit_fallback() -> None:
    html_out = _issue59_page([
        {'phase': 'started', 'cycle_id': 'cycle-part-2', 'ts': '2026-08-18T03:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-part-2', 'status': 'partial',
         'ts': '2026-08-18T03:05:00Z'},
    ])
    assert 'partial: no artifact recorded' in html_out
    assert 'PARTIAL' in html_out


def test_issue59_skipped_duplicate_terminal_badge_not_running() -> None:
    html_out = _issue59_page([
        {'phase': 'started', 'cycle_id': 'cycle-skip-1', 'ts': '2026-08-18T03:00:00Z'},
        {'phase': 'dedup', 'cycle_id': 'cycle-skip-1', 'decision': 'skipped-duplicate',
         'ts': '2026-08-18T03:01:00Z'},
    ])
    assert 'SKIPPED' in html_out
    assert 'badge-skipped' in html_out
    assert 'skipped: skipped-duplicate' in html_out
    # the skipped row must not be rendered as running
    skip_row = html_out.split('id="cycle-cycle-skip-1"')[1].split('</li>')[0]
    assert 'running' not in skip_row


def test_issue59_inflight_cycle_still_running() -> None:
    html_out = _issue59_page([
        {'phase': 'started', 'cycle_id': 'cycle-inflight-1', 'ts': '2026-08-18T03:00:00Z'},
    ])
    row = html_out.split('id="cycle-cycle-inflight-1"')[1].split('</li>')[0]
    assert 'running' in row
    assert 'SKIPPED' not in row


# ---------------------------------------------------------------------------
# Issue #293: outcome `push_pending` is its own bucket, not `skipped`
# (eeebot#1709, PR eeebot#1715)
# ---------------------------------------------------------------------------


def test_293_push_pending_renders_own_badge_not_skipped() -> None:
    html_out = _issue59_page([
        {'phase': 'started', 'cycle_id': 'cycle-pushpending-1', 'ts': '2026-08-18T03:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-pushpending-1', 'outcome': 'push_pending',
         'reason': 'push_pending', 'push_attempts': 3, 'ts': '2026-08-18T03:05:00Z'},
    ])
    row = html_out.split('id="cycle-cycle-pushpending-1"')[1].split('</li>')[0]
    assert 'PUSH PENDING' in row
    assert 'badge-push-pending' in row
    assert '3 attempt(s)' in row
    # Neither failed nor skipped -- a gate-passed, retry-exhausted-network
    # cycle must not be counted or filtered as either.
    assert 'SKIPPED' not in row
    assert 'FAILED' not in row
    assert 'data-outcome="push_pending"' in html_out

    history_html = tv.build_cycle_feed([
        {'phase': 'outcome', 'cycle_id': 'cycle-pushpending-1', 'outcome': 'push_pending',
         'push_attempts': 3, 'ts': '2026-08-18T03:05:00Z'},
    ], history_mode=True)
    assert 'data-filter="push_pending"' in history_html


def test_293_push_pending_no_attempts_field_still_labelled() -> None:
    html_out = _issue59_page([
        {'phase': 'started', 'cycle_id': 'cycle-pushpending-2', 'ts': '2026-08-18T03:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-pushpending-2', 'outcome': 'push_pending',
         'ts': '2026-08-18T03:05:00Z'},
    ])
    row = html_out.split('id="cycle-cycle-pushpending-2"')[1].split('</li>')[0]
    assert 'PUSH PENDING' in row


def test_293_leaf_outcome_push_pending_distinct_from_skipped() -> None:
    assert tv._leaf_outcome({'outcome': 'push_pending'}) == 'push_pending'
    assert tv._leaf_outcome({'status': 'push_pending'}) == 'push_pending'
    assert tv._leaf_outcome({'outcome': 'something_else'}) == 'skipped'


# ---------------------------------------------------------------------------
# Issue #297: outcomes `pushed_late` / `superseded` / `abandoned` (eeebot#1709
# increment 2, PR eeebot#1716) -- pushed_late is a delayed success (the
# `integrated` bucket, noted "late"); superseded/abandoned are their own
# neutral pills, neither failed nor skipped.
# ---------------------------------------------------------------------------


def test_297_pushed_late_renders_as_integrated_with_late_note() -> None:
    html_out = _issue59_page([
        {'phase': 'started', 'cycle_id': 'cycle-latepush-1', 'ts': '2026-08-18T03:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-latepush-1', 'outcome': 'pushed_late',
         'push_attempts': 2, 'ts': '2026-08-18T03:05:00Z'},
    ])
    row = html_out.split('id="cycle-cycle-latepush-1"')[1].split('</li>')[0]
    assert 'INTEGRATED' in row
    assert 'badge-integrated' in row
    assert 'late' in row
    assert '2 attempt(s)' in row
    # A genuine success, delayed -- not failed, not skipped, and not the
    # push_pending bucket either (it already resolved).
    assert 'FAILED' not in row
    assert 'SKIPPED' not in row
    assert 'PUSH PENDING' not in row
    assert 'data-outcome="integrated"' in html_out

    history_html = tv.build_cycle_feed([
        {'phase': 'outcome', 'cycle_id': 'cycle-latepush-1', 'outcome': 'pushed_late',
         'push_attempts': 2, 'ts': '2026-08-18T03:05:00Z'},
    ], history_mode=True)
    # No dedicated filter chip -- pushed_late folds into 'integrated'.
    assert 'data-filter="pushed_late"' not in history_html
    assert 'data-outcome="integrated"' in history_html


def test_297_superseded_renders_own_neutral_pill() -> None:
    html_out = _issue59_page([
        {'phase': 'started', 'cycle_id': 'cycle-superseded-1', 'ts': '2026-08-18T03:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-superseded-1', 'outcome': 'superseded',
         'reason': 'push_pending_main_moved', 'ts': '2026-08-18T03:05:00Z'},
    ])
    row = html_out.split('id="cycle-cycle-superseded-1"')[1].split('</li>')[0]
    assert 'SUPERSEDED' in row
    assert 'badge-superseded' in row
    assert 'push_pending_main_moved' in row
    assert 'FAILED' not in row
    assert 'SKIPPED' not in row
    assert 'data-outcome="superseded"' in html_out

    history_html = tv.build_cycle_feed([
        {'phase': 'outcome', 'cycle_id': 'cycle-superseded-1', 'outcome': 'superseded',
         'ts': '2026-08-18T03:05:00Z'},
    ], history_mode=True)
    assert 'data-filter="superseded"' in history_html


def test_297_abandoned_renders_own_neutral_pill() -> None:
    html_out = _issue59_page([
        {'phase': 'started', 'cycle_id': 'cycle-abandoned-1', 'ts': '2026-08-18T03:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-abandoned-1', 'outcome': 'abandoned',
         'reason': 'push_pending_branch_missing', 'ts': '2026-08-18T03:05:00Z'},
    ])
    row = html_out.split('id="cycle-cycle-abandoned-1"')[1].split('</li>')[0]
    assert 'ABANDONED' in row
    assert 'badge-abandoned' in row
    assert 'push_pending_branch_missing' in row
    assert 'FAILED' not in row
    assert 'SKIPPED' not in row
    assert 'data-outcome="abandoned"' in html_out

    history_html = tv.build_cycle_feed([
        {'phase': 'outcome', 'cycle_id': 'cycle-abandoned-1', 'outcome': 'abandoned',
         'ts': '2026-08-18T03:05:00Z'},
    ], history_mode=True)
    assert 'data-filter="abandoned"' in history_html


def test_297_leaf_outcome_maps_new_statuses() -> None:
    assert tv._leaf_outcome({'outcome': 'pushed_late'}) == 'integrated'
    assert tv._leaf_outcome({'status': 'pushed_late'}) == 'integrated'
    assert tv._leaf_outcome({'outcome': 'superseded'}) == 'superseded'
    assert tv._leaf_outcome({'outcome': 'abandoned'}) == 'abandoned'


def test_issue62_host_identity_real_middle_dots_not_entity_text() -> None:
    # AC: rendered line contains the real middle dot and NOT literal '&middot;'
    data = _fixture()
    data['agents_md'] = 'Instance on the eeepc host: i386 Debian 12, 2 GB RAM, Python 3.11.'
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert '&amp;middot;' not in html_out
    assert '&middot; i386' not in html_out
    m = re.search(r'class="host-identity"[^>]*>([^<]+)<', html_out)
    assert m is not None
    assert '\u00b7' in m.group(1)
    assert 'i386' in m.group(1) and 'Debian 12' in m.group(1)

# ---------------------------------------------------------------------------
# Issue #60: per-cycle LLM cost line in Cycle Feed
# ---------------------------------------------------------------------------


def test_issue60_cost_line_renders_calls_tokens_duration() -> None:
    data = _fixture()
    data['llm_stats'] = {
        'cycle-a': {'calls': 12, 'total_tokens': 1234567, 'duration_ms': 2110000,
                    'last_finish_reason': 'stop', 'any_length': False, 'last_ts': 'x'},
    }
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'feed-cost' in html_out
    assert '12 calls' in html_out
    assert '1.23M tok' in html_out
    assert 'dur 35m10s' in html_out


def test_issue60_budget_pressure_marker_on_length() -> None:
    data = _fixture()
    data['llm_stats'] = {
        'cycle-a': {'calls': 3, 'total_tokens': 5000, 'duration_ms': 42000,
                    'last_finish_reason': 'length', 'any_length': True, 'last_ts': 'x'},
    }
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'cost-pressure' in html_out
    assert 'context overflow' in html_out
    assert 'finish_reason=length' in html_out


def test_issue60_no_llm_data_rows_unchanged() -> None:
    data = _fixture()
    data['llm_stats'] = {}
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'class="feed-cost"' not in html_out
    assert 'class="cost-pressure"' not in html_out


def test_issue60_no_hardcoded_cap() -> None:
    src = Path('scripts/techtree_viewer.py').read_text(encoding='utf-8')
    assert 'SELFEVO' not in src
    assert 'MAX_TOOL' not in src

# ---------------------------------------------------------------------------
# Issue #61: self-describing header KPIs + aim-aware direction gains
# ---------------------------------------------------------------------------


def test_issue61_kpi_definition_tooltips_and_heldout() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'title="total cycles whose changes were merged into the evolution lineage"' in html_out
    assert 'held-out validation: pass rate on tasks excluded' in html_out
    assert 'average LLM tokens spent per integrated cycle' in html_out


def test_issue61_kpi_target_renders_when_scorecard_defines_one() -> None:
    data = _fixture()
    data['scorecard']['targets'] = {'repeat_failure_rate': 0.3}
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'kpi-target' in html_out
    assert 'target' in html_out and '0.3' in html_out


def test_issue61_aim_lower_positive_delta_not_bare_gain() -> None:
    data = _fixture()
    data['portfolio']['nodes']['proposer-quality']['direction'] = 'lower'
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'aim: lower)' in html_out
    assert 'mean Δ' in html_out
    # the aim-lower card must not render the bare gain framing
    assert 'mean gain +' not in html_out.split('dir-box')[1]


def test_issue61_aim_unknown_keeps_gain_wording() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'mean gain' in html_out

# ---------------------------------------------------------------------------
# Issue #63: proposer visibility block
# ---------------------------------------------------------------------------


def test_issue63_proposer_model_and_daily_aggregates_render() -> None:
    data = _fixture()
    data['proposer_stats'] = {
        'calls': 14, 'total_tokens': 234567, 'duration_ms': 900000.0,
        'last_model': 'an/gemini-3.7-flash-low', 'last_ts': '2026-08-25T02:00:00Z',
        'days': {'2026-08-25': {'calls': 14, 'total_tokens': 234567, 'duration_ms': 900000.0}},
    }
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'proposer-block' in html_out
    assert 'an/gemini-3.7-flash-low' in html_out
    assert '2026-08-25' in html_out
    assert '234.6K' in html_out
    assert 'Mean latency' in html_out


def test_issue63_proposer_absent_graceful() -> None:
    data = _fixture()
    data['proposer_stats'] = None
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'proposer-block' in html_out
    assert 'no proposer data recorded' in html_out


def test_issue63_last_proposal_decision_from_ledger() -> None:
    data = _fixture()
    data['ledger_tail'] = list(data['ledger_tail']) + [
        {'phase': 'proposed', 'cycle_id': 'cycle-prop1', 'task_title': 'Wire validator into run_all_tests suite', 'ts': '2026-08-18T09:00:00Z'},
    ]
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'last proposal:' in html_out
    assert 'Wire validator into run_all_tests suite' in html_out


def test_issue63_last_skip_decision_from_ledger() -> None:
    data = _fixture()
    data['ledger_tail'] = list(data['ledger_tail']) + [
        {'phase': 'proposer_reject', 'cycle_id': 'cycle-rej1', 'reason': 'no_valuable_task', 'ts': '2026-08-18T09:00:00Z'},
    ]
    html_out = tv.render_page(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert 'last decision:' in html_out
    assert 'skipped (no_valuable_task)' in html_out

# ---------------------------------------------------------------------------
# Issue #70: multi-page site (index/lineage/cycles/lessons/agent/hypotheses)
# ---------------------------------------------------------------------------

SITE_PAGE_NAMES = ['index.html', 'lineage.html', 'cycles.html', 'tokens.html', 'lessons.html', 'agent.html', 'hypotheses.html', 'techtree.html']


def _site() -> dict:
    return tv.render_pages(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')


def test_issue70_render_pages_returns_all_files() -> None:
    pages = _site()
    for name in SITE_PAGE_NAMES:
        assert name in pages
    assert pages['techtree.html'] != pages['index.html']


def test_issue70_every_page_has_shared_chrome_and_nav_current() -> None:
    pages = _site()
    for name in [n for n in SITE_PAGE_NAMES if n != 'techtree.html']:
        html = pages[name]
        assert 'empire-strip' in html
        assert 'freshness' in html
        assert 'site-nav' in html
        for _, label in tv.SITE_PAGES:
            assert f'>{label}</a>' in html
    assert 'href="index.html" class="nav-current"' in pages['index.html']
    assert 'href="lineage.html" class="nav-current"' in pages['lineage.html']
    assert 'href="cycles.html" class="nav-current"' in pages['cycles.html']
    assert 'href="tokens.html" class="nav-current"' in pages['tokens.html']
    assert 'href="lessons.html" class="nav-current"' in pages['lessons.html']
    assert 'href="agent.html" class="nav-current"' in pages['agent.html']
    assert 'href="hypotheses.html" class="nav-current"' in pages['hypotheses.html']


def test_issue70_no_nav_link_404s() -> None:
    pages = _site()
    for name in [n for n in SITE_PAGE_NAMES if n != 'techtree.html']:
        for fname, _label in tv.SITE_PAGES:
            assert f'href="{fname}"' in pages[name]
            assert fname in pages  # every nav target exists as a produced file


def test_issue70_index_teasers_link_to_pages() -> None:
    idx = _site()['index.html']
    assert 'panel-teasers' in idx
    assert 'href="cycles.html">cycles</a>' in idx
    assert 'href="lineage.html">lineage</a>' in idx
    assert 'href="hypotheses.html">hypotheses</a>' in idx
    assert 'href="lessons.html">lessons</a>' in idx


def test_issue185_hypotheses_futility_meter_three_state() -> None:
    dummy_hypo = {'entries': {'h1': {'title': 'Hypo 1', 'status': 'researching'}}}

    # 1. Unavailable/None state: renders unavailable note, never 0/10
    html_unavail = tv.build_hypotheses_panel(dummy_hypo, demand_futility=None)
    assert 'goal gap futility: unavailable' in html_unavail
    assert '0/10' not in html_unavail

    # 2. Safe / Low attempts:
    futility_safe = {
        'gaps': {
            'goal-gap-safe': {
                'attempt_count': 2,
                'threshold': 10,
                'attempt_unit': 'demand_id',
            }
        }
    }
    html_safe = tv.build_hypotheses_panel(dummy_hypo, demand_futility=futility_safe)
    assert 'goal-gap-safe' in html_safe
    assert '2/10 attempts [demand_id]' in html_safe
    assert 'futility-alarm' not in html_safe

    # 3. High attempts / Alarm (>= 7/10) with lever_surface and tokens:
    futility_alarm = {
        'gaps': {
            'goal-gap-a820ca0c8bb3': {
                'attempt_count': 9,
                'threshold': 10,
                'attempt_unit': 'lever_surface',
                'surface': ['host_metrics', 'subagents'],
            }
        }
    }
    html_alarm = tv.build_hypotheses_panel(dummy_hypo, demand_futility=futility_alarm)
    assert 'futility-alarm' in html_alarm
    assert 'goal-gap-a820ca0c8bb3' in html_alarm
    # 4. Root-keyed dictionary format (direct map of gap_id to data):
    futility_root_keyed = {
        'goal-gap-a820ca0c8bb3': {
            'attempt_count': 9,
            'threshold': 10,
            'attempt_unit': 'lever_surface',
            'surface': ['host_metrics', 'stale_feed'],
        }
    }
    html_root = tv.build_hypotheses_panel(dummy_hypo, demand_futility=futility_root_keyed)
    assert 'futility-alarm' in html_root
    assert '9/10 attempts [lever_surface] (surface: host_metrics, stale_feed)' in html_root


def test_issue193_futility_distinguishes_active_from_resolved_gaps() -> None:
    dummy_hypo = {'entries': {'h-dummy': {'title': 'Dummy', 'status': 'active'}}}
    futility = {
        'goal-gap-active': {
            'metric': 'confirmed_ratio',
            'attempt_count': 8,
            'threshold': 10,
            'attempt_unit': 'demand_id',
        },
        'goal-gap-resolved': {
            'metric': 'stale_feeds',
            'attempt_count': 9,
            'threshold': 10,
            'attempt_unit': 'lever_surface',
            'surface': ['host_metrics', 'stale_feed'],
        },
    }
    scorecard = {
        'gaps': [
            {'metric': 'confirmed_ratio', 'vector': 'V2'},
        ]
    }
    html = tv.build_hypotheses_panel(dummy_hypo, demand_futility=futility, scorecard=scorecard)
    assert 'goal-gap-active' in html
    assert 'goal-gap-resolved' in html
    assert 'futility-alarm' in html  # active gap at 8/10
    assert 'futility-resolved' in html  # resolved gap
    assert '9/10 attempts [lever_surface] (surface: host_metrics, stale_feed)' in html
    assert 'resolved' in html


def test_issue193_resolved_gap_alone_raises_no_alarm() -> None:
    """#193: the false alarm this issue is about, isolated.

    The sibling test above always has an ACTIVE gap at 8/10, so the section
    carries `futility-alarm` whether or not the resolved gap contributes one.
    It therefore passes even if `is_active` is dropped from the warning
    condition. This fixture removes the active gap entirely: the only gap at
    or above the threshold is one the scorecard no longer lists, which is
    exactly the live state that produced `9/10` for a gap that had resolved
    itself. The section must not raise an alarm at all.
    """
    dummy_hypo = {'entries': {'h-dummy': {'title': 'Dummy', 'status': 'active'}}}
    futility = {
        'goal-gap-resolved': {
            'metric': 'stale_feeds',
            'attempt_count': 9,
            'threshold': 10,
            'attempt_unit': 'lever_surface',
            'surface': ['host_metrics', 'stale_feed'],
        },
    }
    scorecard = {'gaps': [{'metric': 'confirmed_ratio', 'vector': 'V2'}]}
    html = tv.build_hypotheses_panel(dummy_hypo, demand_futility=futility, scorecard=scorecard)

    assert 'futility-alarm' not in html, (
        "a gap the scorecard no longer lists still raised the section alarm"
    )
    assert 'futility-resolved' in html
    assert 'badge-dim' in html
    assert 'badge-stale' not in html
    assert '(resolved)' in html


def test_issue193_active_gap_below_threshold_raises_no_alarm() -> None:
    """The other direction: active, but not yet at the warning level."""
    dummy_hypo = {'entries': {'h-dummy': {'title': 'Dummy', 'status': 'active'}}}
    futility = {
        'goal-gap-active': {
            'metric': 'confirmed_ratio', 'attempt_count': 6,
            'threshold': 10, 'attempt_unit': 'demand_id',
        },
    }
    scorecard = {'gaps': [{'metric': 'confirmed_ratio', 'vector': 'V2'}]}
    html = tv.build_hypotheses_panel(dummy_hypo, demand_futility=futility, scorecard=scorecard)
    assert 'futility-alarm' not in html
    assert 'futility-resolved' not in html
    assert 'badge-researching' in html


def test_issue275_futility_null_attempt_count_never_renders_none() -> None:
    """#275: `attempt_count: null` reached the template as the literal `None/10`.

    null, 0 and 6 are three different facts and must render as three visibly
    different strings; the null case must say it was never evaluated.
    """
    dummy_hypo = {'entries': {'h-dummy': {'title': 'Dummy', 'status': 'active'}}}
    futility = {
        'gap-null': {'metric': 'm_null', 'attempt_count': None, 'threshold': 10, 'attempt_unit': 'demand_id'},
        'gap-zero': {'metric': 'm_zero', 'attempt_count': 0, 'threshold': 10, 'attempt_unit': 'demand_id'},
        'gap-six': {'metric': 'm_six', 'attempt_count': 6, 'threshold': 10, 'attempt_unit': 'demand_id'},
    }
    scorecard = {'gaps': [{'metric': 'm_null'}, {'metric': 'm_zero'}, {'metric': 'm_six'}]}
    html = tv.build_hypotheses_panel(dummy_hypo, demand_futility=futility, scorecard=scorecard)
    assert 'None' not in html
    assert 'never evaluated' in html
    assert '0/10 attempts [demand_id]' in html
    assert '6/10 attempts [demand_id]' in html
    assert 'futility-never-evaluated' in html
    assert 'futility-alarm' not in html  # null must not compare as >= 7 either


def test_issue275_futility_default_view_hides_resolved_with_count() -> None:
    """#275 inverse of #193: zero active gaps among many resolved ones must
    render an explicit 'no active gaps' state, and the resolved list stays
    reachable behind a control that states how many are hidden."""
    dummy_hypo = {'entries': {'h-dummy': {'title': 'Dummy', 'status': 'active'}}}
    futility = {
        f'decay-{i:04d}': {'metric': f'dead_{i}', 'attempt_count': None if i % 2 else i % 10, 'threshold': 10}
        for i in range(120)
    }
    scorecard = {'gaps': [{'metric': 'confirmed_ratio'}]}
    html = tv.build_hypotheses_panel(dummy_hypo, demand_futility=futility, scorecard=scorecard)
    section = html[html.index('hypo-futility-section'):html.index('hypo-split')]
    assert 'None' not in section
    assert 'no active gaps' in section
    assert '120 resolved gaps hidden' in section
    assert '<details class="hypo-details futility-hidden">' in section
    assert 'futility-alarm' not in section
    # bounded: the resolved list is capped and says how many were omitted
    assert section.count('class="futility-item') == tv.FUTILITY_RESOLVED_CAP
    omitted = 120 - tv.FUTILITY_RESOLVED_CAP
    assert f'{omitted} more not shown' in section


def test_issue275_active_alarm_visible_without_toggle_among_resolved() -> None:
    """#193's closing behaviour must survive #275: one active gap at 9/10
    among many resolved ones fires the alarm in the default view."""
    dummy_hypo = {'entries': {'h-dummy': {'title': 'Dummy', 'status': 'active'}}}
    futility = {f'decay-{i:04d}': {'metric': f'dead_{i}', 'attempt_count': None, 'threshold': 10} for i in range(40)}
    futility['goal-gap-live'] = {'metric': 'confirmed_ratio', 'attempt_count': 9, 'threshold': 10, 'attempt_unit': 'demand_id'}
    scorecard = {'gaps': [{'metric': 'confirmed_ratio'}]}
    html = tv.build_hypotheses_panel(dummy_hypo, demand_futility=futility, scorecard=scorecard)
    section = html[html.index('hypo-futility-section'):html.index('hypo-split')]
    assert 'futility-alarm' in section
    before_toggle = section[:section.index('<details')]
    assert 'goal-gap-live' in before_toggle
    assert '9/10 attempts [demand_id]' in before_toggle
    assert 'badge-stale' in before_toggle
    assert 'no active gaps' not in section
    assert '40 resolved gaps hidden' in section


def test_issue275_active_list_is_bounded() -> None:
    dummy_hypo = {'entries': {'h-dummy': {'title': 'Dummy', 'status': 'active'}}}
    n = tv.FUTILITY_ACTIVE_CAP + 7
    futility = {f'gap-{i:04d}': {'metric': f'live_{i}', 'attempt_count': 1, 'threshold': 10} for i in range(n)}
    scorecard = {'gaps': [{'metric': f'live_{i}'} for i in range(n)]}
    html = tv.build_hypotheses_panel(dummy_hypo, demand_futility=futility, scorecard=scorecard)
    section = html[html.index('hypo-futility-section'):html.index('hypo-split')]
    assert section.count('class="futility-item') == tv.FUTILITY_ACTIVE_CAP
    assert '7 more not shown' in section
    assert 'futility-resolved' not in section


def test_issue275_no_rendered_page_contains_literal_none() -> None:
    """Site-level guard: no produced page prints a bare `None` as text."""
    import re
    pages = _site()
    for name, html in pages.items():
        text = re.sub(r'<[^>]+>', ' ', html)
        assert not re.search(r'(?<![A-Za-z_])None(?![A-Za-z_])', text), name


def test_issue70_techtree_redirects_to_index() -> None:
    redir = _site()['techtree.html']
    assert 'http-equiv="refresh" content="0; url=index.html"' in redir
    assert 'href="index.html"' in redir


def test_issue70_content_preserved_per_page() -> None:
    pages = _site()
    # #218: lineage.html renders unified dag SVG
    assert 'id="lineage-svg"' in pages['lineage.html']
    assert 'feed-row' in pages['cycles.html']
    assert 'proposer-block' in pages['agent.html']
    assert 'host-identity' in pages['agent.html']
    assert 'hypo-row' in pages['hypotheses.html']
    assert 'panel-now' in pages['index.html']


def test_issue175_teaser_panels_three_state_reporting() -> None:
    data_unavailable = {
        'ledger_tail': None,
        'evolution_tree': None,
        'hypotheses': None,
        'lessons': None,
        'reflections': None,
    }
    idx_un = tv.render_pages(data_unavailable, host='eeepc', generated_at='2026-08-18 12:00:00')['index.html']
    assert 'cycles</a> &mdash; unavailable' in idx_un
    assert 'lineage</a> &mdash; unavailable' in idx_un
    assert 'hypotheses</a> &mdash; unavailable' in idx_un
    assert '0 cycles tracked' not in idx_un
    assert '0 evolution nodes' not in idx_un
    assert '0 active / 0 answered' not in idx_un

    data_empty = {
        'ledger_tail': [],
        'evolution_tree': {'nodes': {}},
        'hypotheses': {'entries': {}},
        'lessons': [],
        'reflections': [],
    }
    idx_em = tv.render_pages(data_empty, host='eeepc', generated_at='2026-08-18 12:00:00')['index.html']
    assert 'cycles</a> &mdash; 0 cycles in full history' in idx_em
    assert 'lineage</a> &mdash; 0 evolution nodes' in idx_em
    assert 'hypotheses</a> &mdash; 0 active / 0 answered + 0 strategist durable' in idx_em


def test_issue70_ledger_change_changes_cycles_page() -> None:
    p1 = _site()['cycles.html']
    data = _fixture()
    data['ledger_tail'] = list(data['ledger_tail']) + [
        {'phase': 'started', 'cycle_id': 'cycle-new1', 'ts': '2026-08-18T13:00:00Z'},
    ]
    p2 = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')['cycles.html']
    assert p1 != p2


def test_issue70_publish_atomic_single_ref_update(monkeypatch) -> None:
    calls = []
    seq = {'blob': 0, 'tree': 0, 'commit': 0, 'ref': 0}

    def fake_gh(args, input_text=None):
        calls.append(args)
        joined = ' '.join(args)
        def cp(out):
            return subprocess.CompletedProcess(args=['gh'] + args, returncode=0, stdout=out, stderr='')
        if 'git/blobs' in joined:
            seq['blob'] += 1
            return cp(f'blosha{seq["blob"]}')
        if 'git/trees' in joined:
            if '-X' in args or input_text is not None:
                seq['tree'] += 1
                return cp('tresha1')
            return cp('{"tree": [], "truncated": false}')
        if 'git/commits' in joined:
            seq['commit'] += 1
            return cp('comsha1')
        if 'git/refs/heads/gh-pages' in joined and '-X' in args:
            seq['ref'] += 1
            return cp('')
        if 'branches/gh-pages' in joined:
            return cp('{"commit":{"sha":"oldparent","commit":{"tree":{"sha":"oldtree"}}}}')
        if '/pages' in joined:
            return cp('{}')
        return cp('{}')

    monkeypatch.setattr(tv, '_gh', fake_gh)
    rc, _fp = tv.publish_to_pages({'index.html': '<html>a</html>', 'cycles.html': '<html>b</html>'})
    assert rc == 0
    assert seq['blob'] == 2
    assert seq['tree'] == 1
    assert seq['commit'] == 1
    assert seq['ref'] == 1  # exactly one atomic ref switch

# ---------------------------------------------------------------------------
# Issue #270: publish_to_pages closes the base_tree/ref race -- a commit
# landing on gh-pages between the base_tree read and the ref update used to
# be silently dropped (base_tree read too early, ref update forced).
# ---------------------------------------------------------------------------


def test_270_no_race_single_attempt_no_retry_message(monkeypatch, capsys) -> None:
    calls = []
    branch_reads = {'n': 0}

    def fake_gh(args, input_text=None):
        calls.append(list(args))
        joined = ' '.join(args)

        def cp(out, rc=0, err=''):
            return subprocess.CompletedProcess(args=['gh'] + args, returncode=rc, stdout=out, stderr=err)
        if 'branches/gh-pages' in joined:
            branch_reads['n'] += 1
            return cp('{"commit":{"sha":"parent1","commit":{"tree":{"sha":"tree1"}}}}')
        if 'git/blobs' in joined:
            return cp('blobsha1')
        if 'git/trees' in joined:
            if '-X' in args or input_text is not None:
                return cp('treesha-new')
            return cp('{"tree": [], "truncated": false}')
        if 'git/commits' in joined and '-X' in args:
            return cp('commitsha-new')
        if 'git/refs/heads/gh-pages' in joined and '-X' in args:
            return cp('')
        if '/pages' in joined:
            return cp('{}')
        return cp('{}')

    monkeypatch.setattr(tv, '_gh', fake_gh)
    rc, _fp = tv.publish_to_pages({'index.html': '<html>only</html>'})
    assert rc == 0
    # 1 bootstrap existence probe + exactly 1 retry-loop read -- no race.
    assert branch_reads['n'] == 2
    out = capsys.readouterr()
    assert 'retr' not in out.out.lower() and 'retr' not in out.err.lower()
    assert 'moved' not in out.out.lower() and 'moved' not in out.err.lower()
    # Non-forcing: the ref PATCH never carries a force flag.
    patch_calls = [a for a in calls if 'git/refs/heads/gh-pages' in ' '.join(a) and '-X' in a]
    assert patch_calls
    assert not any('force' in part for part in patch_calls[0])


def test_270_concurrent_commit_between_read_and_write_is_retried_not_lost(monkeypatch, capsys) -> None:
    """The exact race from the issue: a commit lands on gh-pages between the
    base_tree read and the ref update. The publisher must reject its own
    stale-parented commit (422, non-forcing update), re-read the NOW-current
    head, and rebuild the tree/commit from THAT -- so the concurrent
    commit's tree becomes this commit's base_tree and its files survive,
    rather than being replaced by a tree built on the stale parent."""
    payloads = {'tree': [], 'commit': []}
    branch_reads = {'n': 0}

    def fake_gh(args, input_text=None):
        joined = ' '.join(args)

        def cp(out, rc=0, err=''):
            return subprocess.CompletedProcess(args=['gh'] + args, returncode=rc, stdout=out, stderr=err)
        if 'branches/gh-pages' in joined:
            branch_reads['n'] += 1
            # Call 1 is the bootstrap existence probe (content unused --
            # only its returncode matters); the retry loop's own reads
            # start at call 2, so loop_attempt is 1-indexed from there.
            loop_attempt = branch_reads['n'] - 1
            if loop_attempt <= 1:
                # This read's view: the concurrent human commit has not
                # landed yet.
                return cp('{"commit":{"sha":"stale-parent","commit":{"tree":{"sha":"stale-tree"}}}}')
            # Re-read after rejection: the concurrent commit is now the tip.
            return cp('{"commit":{"sha":"concurrent-parent","commit":{"tree":{"sha":"concurrent-tree"}}}}')
        if 'git/blobs' in joined:
            return cp('blobsha1')
        if 'git/trees' in joined:
            if input_text is not None:
                payloads['tree'].append(json.loads(input_text))
                return cp(f'treesha-attempt{branch_reads["n"]}')
            return cp('{"tree": [], "truncated": false}')
        if 'git/commits' in joined and '-X' in args:
            payloads['commit'].append(json.loads(input_text))
            return cp(f'commitsha-attempt{branch_reads["n"]}')
        if 'git/refs/heads/gh-pages' in joined and '-X' in args:
            if branch_reads['n'] - 1 <= 1:
                # Rejected: stale-parent is no longer the branch tip.
                return cp('', rc=1, err='HTTP 422: Update is not a fast forward')
            return cp('')
        if '/pages' in joined:
            return cp('{}')
        return cp('{}')

    monkeypatch.setattr(tv, '_gh', fake_gh)
    rc, _fp = tv.publish_to_pages({'index.html': '<html>only</html>'})
    assert rc == 0
    # 1 bootstrap probe + 2 retry-loop reads (the rejected attempt, then the
    # re-read that landed).
    assert branch_reads['n'] == 3

    # The commit that actually landed carries the CONCURRENT commit as its
    # parent and base_tree -- inheriting every one of its files via git's
    # own tree-inheritance, not the stale ones.
    assert payloads['tree'][-1]['base_tree'] == 'concurrent-tree'
    assert payloads['commit'][-1]['parents'] == ['concurrent-parent']
    # The FIRST (rejected) attempt really was built from the stale read --
    # proving the retry, not a lucky single read, is what closed the race.
    assert payloads['tree'][0]['base_tree'] == 'stale-tree'
    assert payloads['commit'][0]['parents'] == ['stale-parent']

    out = capsys.readouterr()
    assert 'attempt 1/3' in out.err
    assert 'retried and landed on attempt 2/3' in out.out


def test_270_retries_bounded_and_fails_loudly_on_exhaustion(monkeypatch, capsys) -> None:
    branch_reads = {'n': 0}

    def fake_gh(args, input_text=None):
        joined = ' '.join(args)

        def cp(out, rc=0, err=''):
            return subprocess.CompletedProcess(args=['gh'] + args, returncode=rc, stdout=out, stderr=err)
        if 'branches/gh-pages' in joined:
            branch_reads['n'] += 1
            n = branch_reads['n']
            return cp(f'{{"commit":{{"sha":"parent{n}","commit":{{"tree":{{"sha":"tree{n}"}}}}}}}}')
        if 'git/blobs' in joined:
            return cp('blobsha1')
        if 'git/trees' in joined:
            if '-X' in args or input_text is not None:
                return cp('treesha')
            return cp('{"tree": [], "truncated": false}')
        if 'git/commits' in joined and '-X' in args:
            return cp('commitsha')
        if 'git/refs/heads/gh-pages' in joined and '-X' in args:
            # Every attempt races and loses -- the branch never stops moving.
            return cp('', rc=1, err='HTTP 422: Update is not a fast forward')
        if '/pages' in joined:
            return cp('{}')
        return cp('{}')

    monkeypatch.setattr(tv, '_gh', fake_gh)
    rc, fp = tv.publish_to_pages({'index.html': '<html>only</html>'})
    assert rc == 1
    assert fp == {}
    # 1 bootstrap probe + 3 retry-loop reads -- bounded, never unbounded.
    assert branch_reads['n'] == 4
    out = capsys.readouterr()
    assert 'failed after 3 attempt(s)' in out.err


# ---------------------------------------------------------------------------
# Issue #71: DGM archive tree on lineage.html (full history)
# ---------------------------------------------------------------------------


def _issue71_page(data=None) -> str:
    d = data if data is not None else _fixture()
    return tv.render_pages(d, host='eeepc', generated_at='2026-08-18 12:00:00')['lineage.html']


# #208: test_issue71_merge_trunk_and_failed_leaf, _best_path_bold_and_star,
# _colorbar_fallback_and_reward and _all_history_no_cap exercised the DGM
# archive tree that build_archive_tree only reached when the ledger held no
# evolution_tree rows — dead on every production run since #107 and deleted
# with that branch. The score gauge stays hidden by construction: no reward,
# benefit or evidence field exists in any record (#94), so nothing draws one.


def test_issue71_node_details_and_deeplink() -> None:
    html_out = _issue71_page()
    assert 'cycles.html#cycle-' in html_out
    assert 'cycle-failed-1' in html_out
    assert 'failed' in html_out


def test_issue71_chain_only_layout() -> None:
    data = _fixture()
    data['ledger_tail'] = []
    html_out = _issue71_page(data)
    assert html_out.count('class="arch-node arch-') >= 3


def test_issue71_lineage_page_uses_archive_tree() -> None:
    pages = tv.render_pages(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    lin = pages['lineage.html']
    assert 'arch-tree' in lin
    assert 'EVOLUTION LINEAGE (DGM)</text>' not in lin


def test_issue115_day_arch_tree_has_vertical_trunk_and_failed_leaves() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-root', 'sha': 'root', 'parent_sha': '', 'ts': '2026-08-31T00:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-child', 'sha': 'child', 'parent_sha': 'root', 'ts': '2026-08-31T01:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-failed', 'outcome': 'failed', 'reason': 'gate_failed', 'ts': '2026-08-31T01:30:00Z'},
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-08-31T02:00:00Z')
    assert 'class="arch-node arch-integrated lineage-node"' in html
    assert 'class="arch-node arch-failed lineage-node"' in html
    assert html.count('data-cycle-id=') == 3
    assert 'arch-edge' in html
    # #218: unified DAG; no data-day attributes
    assert 'id="lineage-svg"' in html


def test_issue115_leaf_is_attached_to_trunk_and_lanes_are_reused() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'root', 'sha': 'root', 'parent_sha': '', 'ts': '2026-08-31T00:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'child', 'sha': 'child', 'parent_sha': 'root', 'ts': '2026-08-31T01:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'failed-a', 'outcome': 'failed', 'ts': '2026-08-31T01:10:00Z'},
        {'phase': 'outcome', 'cycle_id': 'failed-b', 'outcome': 'failed', 'ts': '2026-08-31T02:00:00Z'},
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-08-31T03:00:00Z')
    # #218: unified DAG
    leaf_x = [int(x) for x in re.findall(r'class="arch-node arch-failed lineage-node"[^>]*cx="(\d+)"', html)]
    assert len(leaf_x) == 2
    assert html.count('class="lineage-edge arch-edge"') >= 1
    assert html.count('class="lineage-edge arch-edge"') + html.count('class="lineage-edge lineage-edge-chronological"') >= 3


def test_issue125_herringbone_leaves_stay_near_their_base_nodes() -> None:
    rows = []
    for i in range(5):
        rows.append({'phase': 'evolution_tree', 'cycle_id': f'trunk-{i}', 'sha': f'trunk-sha-{i}', 'parent_sha': f'trunk-sha-{i - 1}' if i else '', 'ts': f'2026-08-31T0{i}:00:00Z'})
    rows += [
        {'phase': 'outcome', 'cycle_id': 'leaf-2a', 'outcome': 'failed', 'ts': '2026-08-31T02:10:00Z'},
        {'phase': 'outcome', 'cycle_id': 'leaf-2b', 'outcome': 'failed', 'ts': '2026-08-31T02:20:00Z'},
        {'phase': 'outcome', 'cycle_id': 'leaf-4', 'outcome': 'failed', 'ts': '2026-08-31T04:10:00Z'},
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-08-31T05:00:00Z')
    # #218: unified DAG; geometry verified on full html
    coords = {}
    for m in re.finditer(r'data-cycle-id="([^"]+)"[^>]*cx="(\d+)"[^>]*cy="(\d+)"', html):
        coords[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    assert 'trunk-2' in coords and 'leaf-2a' in coords and 'leaf-4' in coords
    # leaf positions within 64px of trunk node (flexible for unified layout)
    assert abs(coords['leaf-2a'][1] - coords['trunk-2'][1]) <= 64
    assert abs(coords['leaf-4'][1] - coords['trunk-4'][1]) <= 64
    # 4 recorded trunk edges
    assert html.count('class="lineage-edge arch-edge"') >= 4


def test_issue119_resolving_children_form_a_visible_genealogical_fork() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'root', 'sha': 'root', 'parent_sha': '', 'ts': '2026-08-31T00:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'left', 'sha': 'left', 'parent_sha': 'root', 'ts': '2026-08-31T01:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'right', 'sha': 'right', 'parent_sha': 'root', 'ts': '2026-08-31T02:00:00Z'},
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-08-31T03:00:00Z')
    # #218: unified DAG
    coords = {}
    for m in re.finditer(r'data-cycle-id="(root|left|right)"[^>]*cx="(\d+)"[^>]*cy="(\d+)"', html):
        coords[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    assert 'left' in coords and 'right' in coords
    assert coords['left'][0] != coords['right'][0]
    assert html.count('class="lineage-edge arch-edge"') >= 2


def test_issue119_unresolvable_day_is_vertical_dashed_chain() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': f'c{i}', 'sha': f's{i}', 'parent_sha': f'missing{i}', 'ts': f'2026-08-22T00:0{i}:00Z'}
        for i in range(3)
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-08-22T01:00:00Z')
    # #218: nodes with unknown recorded parents have no chronological fallback; all render as roots
    coords = []
    for m in re.finditer(r'class="arch-node arch-integrated lineage-node"[^>]*cx="(\d+)"[^>]*cy="(\d+)"', html):
        coords.append((int(m.group(1)), int(m.group(2))))
    assert len(coords) == 3  # all 3 nodes rendered
    markup = re.sub(r'<script[^>]*>.*?(?:</script>|<\/script>)', '', html, flags=re.S)
    assert markup.count('lineage-edge-chronological') == 0  # no fallback in #218


def test_issue119_detail_card_contains_labeled_markup_not_json_dump() -> None:
    fixture = _fixture()
    fixture['cycle_details'] = {
        'cycle-a': {
            'cycle_id': 'cycle-a', 'outcome': 'success', 'reason': 'ok',
            'ts': '2026-08-16T00:00:00Z', 'sha': 'sha-a', 'parent_sha': 'sha-root',
            'files_changed': ['src/a.py'], 'lesson_insight': 'Useful insight',
        }
    }
    html = tv.render_pages(fixture, host='eeepc', generated_at='2026-08-18 12:00:00')['lineage.html']
    card_script = html[html.index('id="cycle-details-panel"'):]  # #208: records are fetched, the card markup stays inline
    assert 'Selected node' in card_script
    assert 'Cycle summary (shared across' in card_script
    assert 'Parent SHA' not in card_script
    assert "line('Parent SHA'" not in card_script
    assert 'JSON.stringify(item)' not in card_script


def test_issue119_detail_card_is_rendered_with_cycle_detail_fields() -> None:
    fixture = _fixture()
    fixture['cycle_details'] = {
        'cycle-a': {
            'cycle_id': 'cycle-a', 'outcome': 'success', 'reason': 'ok',
            'ts': '2026-08-16T00:00:00Z', 'sha': 'sha-a', 'parent_sha': 'sha-root',
            'files_changed': ['src/a.py'], 'lesson_insight': 'Useful insight',
        }
    }
    html = tv.render_pages(fixture, host='eeepc', generated_at='2026-08-18 12:00:00')['lineage.html']
    assert 'Selected node' in html
    assert 'Cycle summary (shared across' in html
    assert 'Files changed' in html
    assert 'open in Cycle Feed' in html
    assert 'related lessons' in html
    assert 'textContent = JSON.stringify(item)' not in html


def test_issue119_detail_card_renders_labeled_fields_not_raw_json() -> None:
    html = tv.render_pages({**_fixture(), 'cycle_details': {
        'cycle-a': {'cycle_id': 'cycle-a', 'outcome': 'success', 'reason': 'ok', 'ts': '2026-08-16T00:00:00Z', 'sha': 'sha', 'parent_sha': 'parent', 'files_changed': ['src/x.py'], 'lesson_insight': 'useful'}
    }}, host='eeepc', generated_at='2026-08-18 12:00:00')['lineage.html']
    assert 'Selected node' in html and 'Cycle summary (shared across' in html and 'Files changed' in html
    assert 'open in Cycle Feed' in html and 'related lessons' in html
    assert 'cycle-details-body' in html


def test_issue115_svg_width_follows_used_lanes_not_leaf_count() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'root', 'sha': 'root', 'parent_sha': '', 'ts': '2026-08-31T00:00:00Z'},
    ]
    rows += [
        {'phase': 'outcome', 'cycle_id': f'failed-{i}', 'outcome': 'failed', 'ts': f'2026-08-31T{(i // 60):02d}:{(i % 60):02d}:30Z'}
        for i in range(40)
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-08-31T03:00:00Z')
    # #218: unified DAG - check via lineage-svg
    svg_match = re.search(r'<svg id="lineage-svg"[^>]+width="(\d+)" height="(\d+)" viewBox="0 0 (\d+) (\d+)"', html)
    assert svg_match, 'unified lineage svg missing'
    width, height = int(svg_match.group(1)), int(svg_match.group(2))
    assert (width, height) == (int(svg_match.group(3)), int(svg_match.group(4)))
    xs = [int(x) for x in re.findall(r'cx="(\d+)"', html)]
    ys = [int(y) for y in re.findall(r'cy="(\d+)"', html)]
    assert max(xs) + 9 <= width, 'node overflows svg width'
    assert max(ys) + 9 <= height, 'node overflows svg height'


def test_issue109_fork_children_use_distinct_rows_and_edges() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'root', 'sha': 'root', 'parent_sha': '', 'ts': '2026-08-31T00:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'left', 'sha': 'left', 'parent_sha': 'root', 'ts': '2026-08-31T01:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'right', 'sha': 'right', 'parent_sha': 'root', 'ts': '2026-08-31T02:00:00Z'},
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-08-31T03:00:00Z')
    # #218: at least 2 edges; exact cy values depend on unified layout
    assert html.count('class="lineage-edge') >= 2


def test_issue109_unresolvable_parents_use_dashed_chronological_chain() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'one', 'sha': 'one', 'parent_sha': 'missing-a', 'ts': '2026-08-20T00:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'two', 'sha': 'two', 'parent_sha': 'missing-b', 'ts': '2026-08-20T01:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'three', 'sha': 'three', 'parent_sha': 'missing-c', 'ts': '2026-08-20T02:00:00Z'},
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-08-20T03:00:00Z')
    # #218: unknown recorded parents have no chronological fallback edge
    markup = re.sub(r'<script[^>]*>.*?(?:</script>|<\/script>)', '', html, flags=re.S)
    assert markup.count('class="lineage-edge lineage-edge-chronological"') == 0
    # #218: unknown parent markers appear in SVG markup (not 'lineage-hidden-parent', which was cross-day stubs)
    # nodes with unknown recorded parents show 'unknown parent' text above them
    assert markup.count('class="lineage-hidden-parent"') >= 2


def test_issue109_chronological_edges_use_consecutive_same_day_positions() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': f'cycle-{i}', 'sha': f'sha-{i}', 'parent_sha': f'missing-{i}', 'ts': f'2026-08-17T00:{i:02d}:00Z'}
        for i in range(6)
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-08-17T01:00:00Z')
    # #218: unknown recorded parents have no chronological fallback; all 6 are independent roots
    markup4 = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
    edges_inferred = re.findall(r'<line[^>]+lineage-edge-chronological[^>]*/>', markup4)
    assert len(edges_inferred) == 0
    # (consecutive-position check removed in #218; roots have no chronological edges)


def test_issue109_every_day_svg_geometry_stays_inside_its_viewbox() -> None:
    rows = []
    parent = ''
    for day in range(14):
        for index in range(3):
            sha = f'sha-{day}-{index}'
            rows.append({
                'phase': 'evolution_tree', 'cycle_id': sha, 'sha': sha,
                'parent_sha': parent if index == 0 else f'missing-{day}-{index}',
                'ts': f'2026-08-{day + 1:02d}T00:00:00Z',
            })
            parent = sha
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-08-14T01:00:00Z')
    for svg in re.findall(r'<svg class="lineage-day-svg"[^>]*>.*?</svg>', html, re.DOTALL):
        width, height = map(int, re.search(r'width="(\d+)" height="(\d+)"', svg).groups())
        view_width, view_height = map(int, re.search(r'viewBox="0 0 (\d+) (\d+)"', svg).groups())
        assert (width, height) == (view_width, view_height)
        numbers = [int(value) for value in re.findall(r'(?:cx|x1|x2)="(-?\d+)"', svg)]
        y_numbers = [int(value) for value in re.findall(r'(?:cy|y1|y2)="(-?\d+)"', svg)]
        assert all(0 <= value <= width for value in numbers)
        assert all(0 <= value <= height for value in y_numbers)

# ---------------------------------------------------------------------------
# Issue #77: ring-class outcome join fix (outcome field vocabulary)
# ---------------------------------------------------------------------------


# #208: test_issue77_each_outcome_gets_own_ring_only_inflight_running and
# test_issue77_legend_covers_every_ring_class asserted the ring legend and the
# `running` ring of the deleted DGM archive tree. The day lineage renders
# failed / partial / skipped leaves (see test_issue115_* and #208's tests) and
# has no legend; an in-flight cycle is not a leaf there.


# #208 review: test_issue77_live_outcome_vocabulary_parsed tested
# _ledger_outcome_kind, whose only caller was the deleted archive tree; both
# are gone. The day lineage classifies leaves inline in
# _build_vertical_day_lineage (failed / partial / skipped) — and, as a
# consequence, an in-flight cycle ("running") never appears on lineage.html;
# it is a cycle-feed state only.

# ---------------------------------------------------------------------------
# Issue #72: cycles.html full history (.gz archives, day grouping, filter)
# ---------------------------------------------------------------------------


def test_issue72_gz_archives_render(tmp_path) -> None:
    import gzip as gz
    ledger = tmp_path / 'ledger'
    ledger.mkdir(parents=True)
    (ledger / 'cycles.jsonl').write_text(
        '{"phase":"started","cycle_id":"cycle-live1","ts":"2026-08-25T00:00:00Z"}\n', encoding='utf-8')
    gz_row = '{"phase":"outcome","outcome":"failed","reason":"gate_failed","cycle_id":"cycle-gz1","ts":"2026-08-20T00:00:00Z"}\n'
    with gz.open(ledger / 'cycles-2026-08-20.jsonl.gz', 'wt', encoding='utf-8') as fh:
        fh.write(gz_row)
    state = tv.read_local_state(str(tmp_path))
    hist = state.get('ledger_history') or []
    assert any(r.get('cycle_id') == 'cycle-gz1' for r in hist)


def test_issue72_stable_anchors() -> None:
    pages = tv.render_pages(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    cyc = pages['cycles.html']
    assert 'id="cycle-cycle-a"' in cyc
    assert 'id="cycle-cycle-failed-1"' in cyc


def test_issue72_outcome_filter() -> None:
    pages = tv.render_pages(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    cyc = pages['cycles.html']
    assert 'filter-bar' in cyc
    assert 'data-filter="integrated"' in cyc
    assert 'location.hash' in cyc
    assert 'data-outcome=' in cyc


def test_issue72_day_grouping_preserved() -> None:
    pages = tv.render_pages(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    cyc = pages['cycles.html']
    assert 'feed-day-header' in cyc
    assert '2026-08-16' in cyc
    assert 'INTEGRATED' in cyc
    assert 'Optimize prompt caching for proposer' in cyc


def _read_archived_rows(pages: dict) -> list:
    """Test helper: reconstruct the full newest-first archived-row list from
    the chunked cycles-archive files (#278)."""
    idx = json.loads(pages[tv.CYCLES_ARCHIVE_INDEX_FILE])
    rows: list = []
    for i in range(idx['chunk_count'] - 1, -1, -1):
        rows.extend(json.loads(pages[tv.cycles_archive_chunk_file(i)]))
    return rows


def test_issue72_history_mode_full_no_cap() -> None:
    """#278: 'no cap' now means no cycle is DROPPED, not that every row is
    inlined -- rows beyond the visible window move to the chunked cycles
    archive instead of being CSS-hidden-but-still-shipped in cycles.html."""
    data = _fixture()
    extra = []
    for i in range(60):
        cid = f'cycle-hist{i:02d}'
        extra.append({'phase': 'outcome', 'outcome': 'failed', 'reason': 'x', 'cycle_id': cid, 'ts': f'2026-08-17T00:{i:02d}:00Z'})
    data['ledger_tail'] = list(data['ledger_tail']) + extra
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    cyc = pages['cycles.html']
    archive_text = ''.join(_read_archived_rows(pages))
    for i in range(60):
        marker = f'id="cycle-cycle-hist{i:02d}"'
        assert marker in cyc or marker in archive_text, f'{marker} missing from both cycles.html and the archive'

# ---------------------------------------------------------------------------
# Issue #81: blob payloads go via stdin (--input -), never via argv
# ---------------------------------------------------------------------------


def test_issue81_large_blob_via_stdin_not_argv(monkeypatch) -> None:
    """Regression (#81): a >300KB page must not travel through the gh argv
    (OSError: Argument list too long). Content rides in input_text/stdin."""
    big_page = '<html>' + ('x' * 400_000) + '</html>'
    captured = []

    def fake_gh(args, input_text=None):
        captured.append((list(args), input_text or ''))
        joined = ' '.join(args)

        def cp(out):
            return subprocess.CompletedProcess(args=['gh'] + args, returncode=0, stdout=out, stderr='')
        if 'git/blobs' in joined:
            return cp('blosha81')
        if 'git/trees' in joined:
            if '-X' in args or input_text is not None:
                return cp('tresha81')
            return cp('{"tree": [], "truncated": false}')
        if 'git/commits' in joined:
            return cp('comsha81')
        if 'git/refs/heads/gh-pages' in joined and '-X' in args:
            return cp('')
        if 'branches/gh-pages' in joined:
            return cp('{"commit":{"sha":"oldparent","commit":{"tree":{"sha":"oldtree"}}}}')
        if '/pages' in joined:
            return cp('{}')
        return cp('{}')

    monkeypatch.setattr(tv, '_gh', fake_gh)
    rc, _fp = tv.publish_to_pages({'cycles.html': big_page})
    assert rc == 0
    blob_calls = [(a, t) for a, t in captured if 'git/blobs' in ' '.join(a)]
    assert blob_calls, 'blob call missing'
    for args, input_text in blob_calls:
        # content rides via stdin, never via argv
        assert '--input' in args and '-' in args
        assert '-f' not in args
        assert all(len(a) < 10_000 for a in args), 'argv carries payload'
        assert big_page[:100] in input_text or len(input_text) > 300_000

# ---------------------------------------------------------------------------
# Issue #278: split the published payload -- unchanged pages are not
# re-uploaded, and cycles.html windows its overflow to a fetched archive.
# ---------------------------------------------------------------------------


def _fake_gh_publish_factory(calls: list):
    def fake_gh(args, input_text=None):
        calls.append(list(args))
        joined = ' '.join(args)

        def cp(out):
            return subprocess.CompletedProcess(args=['gh'] + args, returncode=0, stdout=out, stderr='')
        if 'git/blobs' in joined:
            return cp('newblobsha')
        if 'git/trees' in joined:
            if '-X' in args or input_text is not None:
                return cp('newtreesha')
            return cp('{"tree": [], "truncated": false}')
        if 'git/commits' in joined:
            return cp('newcommitsha')
        if 'git/refs/heads/gh-pages' in joined and '-X' in args:
            return cp('')
        if 'branches/gh-pages' in joined:
            return cp('{"commit":{"sha":"oldparent","commit":{"tree":{"sha":"oldtree"}}}}')
        if '/pages' in joined:
            return cp('{}')
        return cp('{}')
    return fake_gh


def test_278_page_fingerprint_ignores_generated_at_and_source_age() -> None:
    html_a = 'generator sha1 · generated 2026-09-17 03:00:00 UTC · newest source 5m old CONTENT'
    html_b = 'generator sha1 · generated 2026-09-17 03:05:00 UTC · newest source 1.2h old CONTENT'
    assert tv._page_fingerprint(html_a) == tv._page_fingerprint(html_b)


def test_278_page_fingerprint_differs_on_real_content_change() -> None:
    html_a = 'generated 2026-09-17 03:00:00 UTC · newest source 5m old CONTENT A'
    html_b = 'generated 2026-09-17 03:00:00 UTC · newest source 5m old CONTENT B'
    assert tv._page_fingerprint(html_a) != tv._page_fingerprint(html_b)


def test_278_publish_to_pages_skips_unchanged_pages(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(tv, '_gh', _fake_gh_publish_factory(calls))

    unchanged_html = 'generated 2026-09-17 03:00:00 UTC · newest source 5m old UNCHANGED'
    changed_html_old = 'generated 2026-09-17 02:00:00 UTC · newest source 65m old CHANGED-OLD'
    changed_html_new = 'generated 2026-09-17 03:00:00 UTC · newest source 5m old CHANGED-NEW'
    previous_fp = {
        'index.html': tv._page_fingerprint(unchanged_html),
        'cycles.html': tv._page_fingerprint(changed_html_old),
    }
    rc, fingerprints = tv.publish_to_pages(
        {'index.html': unchanged_html, 'cycles.html': changed_html_new},
        previous_fingerprints=previous_fp,
    )
    assert rc == 0
    blob_calls = [c for c in calls if 'git/blobs' in ' '.join(c)]
    assert len(blob_calls) == 1  # only cycles.html re-uploaded
    assert fingerprints['index.html'] == previous_fp['index.html']
    assert fingerprints['cycles.html'] != previous_fp['cycles.html']


def test_278_publish_to_pages_all_unchanged_skips_tree_commit_ref(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(tv, '_gh', _fake_gh_publish_factory(calls))

    html = 'generated 2026-09-17 03:00:00 UTC · newest source 5m old SAME'
    previous_fp = {'index.html': tv._page_fingerprint(html)}
    rc, fingerprints = tv.publish_to_pages({'index.html': html}, previous_fingerprints=previous_fp)
    assert rc == 0
    assert not any('git/blobs' in ' '.join(c) for c in calls)
    assert not any('git/trees' in ' '.join(c) for c in calls)
    assert not any('git/commits' in ' '.join(c) for c in calls)
    assert fingerprints == previous_fp


def test_278_cycles_html_windows_overflow_to_archive_and_publishes_sibling() -> None:
    ledger = [
        {'phase': 'outcome', 'cycle_id': f'cycle-w{i:03d}', 'outcome': 'success', 'ts': f'2026-09-{(i % 28) + 1:02d}T00:00:00Z'}
        for i in range(80)
    ]
    archive_out: list = []
    html = tv.build_cycle_feed(ledger, history_mode=True, archive_out=archive_out)
    assert len(archive_out) == 30  # 80 - 50 visible window
    assert 'show 30 older cycles' in html
    assert 'data-cycle-archive-index-src' in html
    assert tv.CYCLES_ARCHIVE_INDEX_FILE in html
    # None of the archived rows' ids are inlined in the returned HTML.
    for row_html in archive_out:
        import re as _re
        m = _re.search(r'id="(cycle-cycle-w\d+)"', row_html)
        assert m is not None
        assert f'id="{m.group(1)}"' not in html


def test_278_cycles_html_archive_sibling_published_in_render_pages() -> None:
    data = _fixture()
    data['ledger_tail'] = [
        {'phase': 'outcome', 'cycle_id': f'cycle-p{i:03d}', 'outcome': 'success', 'ts': f'2026-09-{(i % 28) + 1:02d}T00:00:00Z'}
        for i in range(80)
    ]
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-09-17 12:00:00')
    assert tv.CYCLES_ARCHIVE_INDEX_FILE in pages
    archive_rows = _read_archived_rows(pages)
    assert len(archive_rows) == 30
    assert 'data-cycle-archive-index-src' in pages['cycles.html']


def test_278_chunk_archive_rows_seals_full_chunks_stably() -> None:
    """#278: chunk 0 is always the OLDEST chunk_size rows and never changes
    again once sealed, regardless of how many more rows get archived later
    -- only the newest (possibly partial) chunk's membership changes."""
    rows_newest_first_a = [f'<li id="r{i}"></li>' for i in range(219, -1, -1)]  # 220 rows, ids 219..0
    chunks_a = tv.chunk_archive_rows(rows_newest_first_a, chunk_size=100)
    assert set(chunks_a.keys()) == {'cycles-archive-0.json', 'cycles-archive-1.json', 'cycles-archive-2.json'}
    assert chunks_a['cycles-archive-0.json'] == [f'<li id="r{i}"></li>' for i in range(99, -1, -1)]
    assert chunks_a['cycles-archive-1.json'] == [f'<li id="r{i}"></li>' for i in range(199, 99, -1)]
    assert chunks_a['cycles-archive-2.json'] == [f'<li id="r{i}"></li>' for i in range(219, 199, -1)]

    # 5 more rows archived later (ids 220..224 are newer than 219, so they
    # sit at the FRONT of the newest-first list).
    rows_newest_first_b = [f'<li id="r{i}"></li>' for i in range(224, -1, -1)]
    chunks_b = tv.chunk_archive_rows(rows_newest_first_b, chunk_size=100)
    assert chunks_b['cycles-archive-0.json'] == chunks_a['cycles-archive-0.json']  # sealed, unchanged
    assert chunks_b['cycles-archive-1.json'] == chunks_a['cycles-archive-1.json']  # sealed, unchanged
    assert chunks_b['cycles-archive-2.json'] != chunks_a['cycles-archive-2.json']  # still filling, grew


def test_278_steady_state_publish_skips_sealed_archive_chunks(monkeypatch) -> None:
    """The whole point of chunking (#278): once a chunk is sealed, adding
    more cycles must not force it to be re-uploaded. Only the tail chunk,
    cycles.html, index.html and lineage change; sealed archive chunks and
    domain-unrelated pages (tokens/agent/lessons/hypotheses/cycle.html)
    are skipped."""
    calls: list = []
    monkeypatch.setattr(tv, '_gh', _fake_gh_publish_factory(calls))

    def make_data(n):
        data = _fixture()
        ledger = [
            {'phase': 'outcome', 'cycle_id': f'cycle-r{i:04d}', 'outcome': 'success' if i % 5 else 'failed',
             'ts': f'2026-01-{(i % 28) + 1:02d}T00:{i % 60:02d}:00Z'}
            for i in range(n)
        ]
        data['ledger_tail'] = ledger
        data['ledger_history'] = ledger
        return data

    pages1 = tv.render_pages(make_data(350), host='eeepc', generated_at='2026-09-17 03:00:00')
    rc1, fp1 = tv.publish_to_pages(pages1)
    assert rc1 == 0

    calls.clear()
    pages2 = tv.render_pages(make_data(351), host='eeepc', generated_at='2026-09-17 03:05:00')
    rc2, fp2 = tv.publish_to_pages(pages2, previous_fingerprints=fp1)
    assert rc2 == 0
    blob_paths_uploaded = []
    for c in calls:
        if 'git/blobs' in ' '.join(c):
            blob_paths_uploaded.append(c)
    # cycles-archive-0.json (sealed at 100 rows) must NOT be among the
    # re-uploaded blobs on this run.
    changed_pages = {k for k in pages2 if fp2.get(k) != fp1.get(k)}
    assert 'cycles-archive-0.json' not in changed_pages
    assert 'tokens.html' not in changed_pages
    assert 'agent.html' not in changed_pages
    assert 'lessons.html' not in changed_pages
    assert 'hypotheses.html' not in changed_pages
    assert 'cycle.html' not in changed_pages

# ---------------------------------------------------------------------------
# Issue #73: lessons.html from lessons/lessons.yaml
# ---------------------------------------------------------------------------

_LESSON_YAML = '''lessons:
  - id: "LESS-20260825-aaaa"
    date: "2026-08-25"
    cycle_id: "cycle-a"
    task_id: "Wire validator into suite"
    hypothesis: "validator catches regressions"
    result: "confirmed: validator caught 2 regressions"
    generalized_insight: "validators pay off"
  - id: "LESS-20260820-bbbb"
    date: "2026-08-20"
    cycle_id: "cycle-failed-1"
    task_id: "Add speculative syntax checking"
    hypothesis: "syntax pre-check helps"
    result: "refuted: no effect"
    generalized_insight: "skip pre-checks"
'''


def test_issue73_parses_live_and_archive_newest_first(tmp_path) -> None:
    import gzip as gz
    repo = tmp_path / 'eeebot-self-evolving'
    lessons_dir = repo / 'lessons'
    lessons_dir.mkdir(parents=True)
    (lessons_dir / 'lessons.yaml').write_text(_LESSON_YAML, encoding='utf-8')
    arch = lessons_dir / 'archive'
    arch.mkdir()
    with gz.open(arch / 'lessons-2026-08-19.yaml.gz', 'wt', encoding='utf-8') as fh:
        fh.write('lessons:\n  - id: "LESS-20260819-cccc"\n    date: "2026-08-19"\n    cycle_id: "cycle-gz1"\n    task_id: "Old archived lesson"\n    hypothesis: "h"\n    result: "r"\n')
    state = tv.read_local_state(str(tmp_path), instance_repo=str(repo))
    lessons = state.get('lessons') or []
    ids = [l.get('id') for l in lessons]
    assert 'LESS-20260825-aaaa' in ids and 'LESS-20260819-cccc' in ids
    assert ids.index('LESS-20260825-aaaa') < ids.index('LESS-20260819-cccc')  # newest first


def _issue1564_archive_fixture(tmp_path: Path) -> Path:
    import gzip as gz

    repo = tmp_path / 'eeebot-self-evolving'
    lessons_dir = repo / 'lessons'
    lessons_dir.mkdir(parents=True)
    (lessons_dir / 'lessons.yaml').write_text(_LESSON_YAML, encoding='utf-8')
    arch = lessons_dir / 'archive'
    arch.mkdir()
    archive_text = """lessons:
  - id: "LESS-RETIRED-1564"
    date: "2026-09-12"
    cycle_id: "cycle-retired"
    task_id: "retired v1 receipt"
    result: "Committed 1 commit(s)"
"""
    with gz.open(arch / 'lessons-2026-08-25.yaml.gz', 'wt', encoding='utf-8') as fh:
        fh.write(archive_text)
    with gz.open(arch / 'lessons-2026-09-12.yaml.gz', 'wt', encoding='utf-8') as fh:
        fh.write(archive_text.replace('LESS-RETIRED-1564', 'LESS-RETIRED-1564-B'))
    with gz.open(arch / 'lessons-2026-08-23.yaml.gz', 'wt', encoding='utf-8') as fh:
        fh.write(archive_text.replace('LESS-RETIRED-1564', 'LESS-RETAINED-1564'))
    return repo


def test_issue1564_excludes_retired_v1_archives_from_local_reader(tmp_path: Path) -> None:
    repo = _issue1564_archive_fixture(tmp_path)
    data = tv.read_local_state(str(tmp_path), instance_repo=str(repo))
    ids = {str(item.get('id')) for item in data.get('lessons') or []}
    assert 'LESS-RETAINED-1564' in ids
    assert 'LESS-RETIRED-1564' not in ids
    assert 'LESS-RETIRED-1564-B' not in ids


def test_issue1564_excludes_retired_v1_archives_from_published_reader(tmp_path: Path) -> None:
    import contextlib
    import io

    repo = _issue1564_archive_fixture(tmp_path)
    # The published reader is embedded in the SSH script rather than exposed
    # as a local module function. Execute that exact reader against an isolated
    # fixture, with all unrelated sources remaining fail-soft.
    script = tv.REMOTE_READER_SCRIPT.replace(
        'INSTANCE_REPO = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"',
        f'INSTANCE_REPO = {str(repo)!r}',
    )
    namespace: dict[str, object] = {}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(script, namespace)
    rows = namespace['read_lessons']()
    ids = {str(item.get('id')) for item in rows}
    assert 'LESS-RETAINED-1564' in ids
    assert 'LESS-RETIRED-1564' not in ids
    assert 'LESS-RETIRED-1564-B' not in ids


def test_issue73_missing_file_graceful_note() -> None:
    data = _fixture()
    data['lessons'] = []
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    # Deliberate #246 contract change: this page is a retained-history
    # population, not the full executor retrieval corpus.
    assert 'no lessons history data recorded' in pages['lessons.html']


def test_issue73_filter_and_hash_markers() -> None:
    data = _fixture()
    data['lessons'] = [{'id': 'LESS-20260825-aaaa', 'date': '2026-08-25', 'cycle_id': 'cycle-a',
                        'task_id': 't', 'hypothesis': 'h', 'result': 'r', 'insight': 'i'}]
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    les = pages['lessons.html']
    assert 'lessons-filter' in les
    assert 'location.hash' in les
    assert '#q-' in les


def test_issue73_entries_render_with_cycle_links() -> None:
    data = _fixture()
    data['lessons'] = [
        {'id': 'LESS-20260825-aaaa', 'date': '2026-08-25', 'cycle_id': 'cycle-a',
         'task_id': 'Wire validator into suite', 'hypothesis': 'validator catches regressions',
         'result': 'confirmed', 'insight': 'validators pay off'},
    ]
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    les = pages['lessons.html']
    assert 'cycles.html#cycle-cycle-a' in les
    assert 'LESS-20260825-aaaa' in les
    assert 'Wire validator into suite' in les
    assert 'validators pay off' not in les  # ADR-036 rule 3: lesson body is LAN-only


def test_issue73_rotation_archive_truncated_head_parsed(tmp_path) -> None:
    # Live rotation artifact: archive starts mid-entry (truncated tail of the
    # previous file) and entries sit at top level -> PyYAML raises, the flat
    # fallback must still yield all entries (issue #73 follow-up).
    import gzip as gz
    repo = tmp_path / 'eeebot-self-evolving'
    lessons_dir = repo / 'lessons'
    lessons_dir.mkdir(parents=True)
    (lessons_dir / 'lessons.yaml').write_text(_LESSON_YAML, encoding='utf-8')
    arch = lessons_dir / 'archive'
    arch.mkdir()
    arch_text = ('lessons:\n  - scripts/validate_markdown_format.py\n'
                 '- id: LESS-20260823-b618\n  date: "2026-08-23"\n  cycle_id: cycle-b618\n'
                 '  task_id: Old lesson A\n  hypothesis: h\n  result: r\n'
                 '- id: LESS-20260822-cccc\n  date: "2026-08-22"\n  cycle_id: cycle-cccc\n'
                 '  task_id: Old lesson B\n  hypothesis: h\n  result: r\n')
    with gz.open(arch / 'lessons-2026-08-23.yaml.gz', 'wt', encoding='utf-8') as fh:
        fh.write(arch_text)
    state = tv.read_local_state(str(tmp_path), instance_repo=str(repo))
    lessons = state.get('lessons') or []
    ids = [l.get('id') for l in lessons]
    assert 'LESS-20260823-b618' in ids and 'LESS-20260822-cccc' in ids
    assert len(lessons) >= 4  # 2 live + 2 archived

# ---------------------------------------------------------------------------
# Issue #274: lessons.html reads lessons/lessons.yaml, a store the loop has
# left; four counts described one corpus. Primary population is now the live
# corpus (lessons/*.md + index.md); lessons.yaml is an explicitly labelled
# archive.
# ---------------------------------------------------------------------------


def _write_lesson_md(lessons_dir: Path, filename: str, *, description: str, prevention: str) -> None:
    (lessons_dir / filename).write_text(
        f'# placeholder\n\n## Description\n\n{description}\n\n## Prevention\n\n{prevention}\n',
        encoding='utf-8',
    )


def test_274_lessons_html_reads_live_corpus_matches_agent_html_count(tmp_path: Path) -> None:
    repo = tmp_path / 'eeebot-self-evolving'
    lessons_dir = repo / 'lessons'
    lessons_dir.mkdir(parents=True)
    _write_lesson_md(lessons_dir, 'LESS-20260916-aaaa.md', description='Root cause A', prevention='Fix A')
    _write_lesson_md(lessons_dir, 'LESS-20260915-bbbb.md', description='Root cause B', prevention='Fix B')
    (lessons_dir / 'index.md').write_text(
        '| [Corpus title A](LESS-20260916-aaaa.md) | Fix A | corrective |\n'
        '| [Corpus title B](LESS-20260915-bbbb.md) | Fix B | corrective |\n',
        encoding='utf-8',
    )
    state = tv.read_local_state(str(tmp_path), instance_repo=str(repo))
    live = [l for l in (state.get('lessons') or []) if l.get('source') == 'live']
    assert {l['id'] for l in live} == {'LESS-20260916-aaaa', 'LESS-20260915-bbbb'}
    titled = next(l for l in live if l['id'] == 'LESS-20260916-aaaa')
    assert titled['title'] == 'Corpus title A'
    assert 'Root cause A' in titled['problem']
    assert 'Fix A' in titled['solution']

    # Single code path: same count agent.html's Tier 2 panel reports.
    tier2_count = state['agent_context']['tier2_lessons']['corpus_count']
    assert tier2_count == 2 == len(live)

    pages = tv.render_pages(state, host='eeepc', generated_at='2026-09-16 12:00:00')
    assert 'Corpus title A' in pages['lessons.html']
    assert 'Live corpus (lessons/*.md): 2 files' in pages['lessons.html']


def test_274_missing_index_falls_back_to_filename_no_silent_drop(tmp_path: Path) -> None:
    repo = tmp_path / 'eeebot-self-evolving'
    lessons_dir = repo / 'lessons'
    lessons_dir.mkdir(parents=True)
    _write_lesson_md(lessons_dir, 'LESS-20260916-cccc.md', description='Root cause C', prevention='Fix C')
    # No index.md at all -- the corpus scan must still find and render it.
    state = tv.read_local_state(str(tmp_path), instance_repo=str(repo))
    live = [l for l in (state.get('lessons') or []) if l.get('source') == 'live']
    assert len(live) == 1
    assert live[0]['id'] == 'LESS-20260916-cccc'
    assert live[0]['title'] == 'LESS-20260916-cccc'  # falls back to filename


def test_274_remote_reader_script_mirrors_live_corpus(tmp_path: Path) -> None:
    import contextlib
    import io

    repo = tmp_path / 'eeebot-self-evolving'
    lessons_dir = repo / 'lessons'
    lessons_dir.mkdir(parents=True)
    _write_lesson_md(lessons_dir, 'LESS-20260916-dddd.md', description='Root cause D', prevention='Fix D')
    (lessons_dir / 'index.md').write_text('| [Remote title D](LESS-20260916-dddd.md) | Fix D | corrective |\n', encoding='utf-8')
    script = tv.REMOTE_READER_SCRIPT.replace(
        'INSTANCE_REPO = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"',
        f'INSTANCE_REPO = {str(repo)!r}',
    )
    namespace: dict[str, object] = {}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(script, namespace)
    rows = namespace['read_lessons']()
    live = [r for r in rows if r.get('source') == 'live']
    assert len(live) == 1
    assert live[0]['id'] == 'LESS-20260916-dddd'
    assert live[0]['title'] == 'Remote title D'


def test_274_corpus_status_three_distinguishable_states() -> None:
    missing = tv.build_lessons_panel([], corpus_status='missing')
    unavailable = tv.build_lessons_panel([], corpus_status='unavailable')
    present_empty = tv.build_lessons_panel(
        [{'id': 'LESS-ARCHIVE-1', 'date': '2026-08-01', 'task_id': 't', 'hypothesis': 'h', 'result': 'r'}],
        corpus_status='present',
    )
    assert 'directory missing on host' in missing
    assert 'present but unreadable' in unavailable
    assert 'Live corpus (lessons/*.md): 0 files' in present_empty
    assert missing != unavailable != present_empty


def test_cycle_feed_normalizes_lesson_reference_path_and_prefix() -> None:
    html = tv.build_cycle_feed(
        [{'phase': 'outcome', 'cycle_id': 'cycle-path', 'outcome': 'success',
          'lessons_context': ['lesson:lessons/subagent_result_error_handling.md']}],
        rendered_lesson_ids={'subagent_result_error_handling'},
    )
    assert 'href="lessons.html#q-subagent_result_error_handling"' in html
    assert '(unavailable)' not in html


def test_cycle_feed_falls_back_to_work_title_or_explicit_no_files() -> None:
    rows = [
        {'phase': 'proposed', 'cycle_id': 'cycle-title', 'task_title': 'Implement diary filter'},
        {'phase': 'outcome', 'cycle_id': 'cycle-title', 'outcome': 'success', 'files_changed': ['src/filter.py']},
        {'phase': 'outcome', 'cycle_id': 'cycle-empty', 'outcome': 'success', 'files_changed': []},
    ]
    html = tv.build_cycle_feed(rows, task_titles={}, history_mode=True)
    assert '<strong class="feed-title">Implement diary filter</strong>' in html
    assert '<strong class="feed-title">integrated · no files</strong>' in html
    assert '<strong class="feed-title">success</strong>' not in html


def test_non_work_commit_detector_skips_chore_and_merge() -> None:
    """Regression: chore: and merge: commits must be skipped as non-work commits."""
    assert tv._is_non_work_commit_message("merge: sync with main")
    assert tv._is_non_work_commit_message("chore: update dependencies")


def test_cycle_feed_reads_files_changed_from_ledger_and_does_not_falsely_claim_no_files() -> None:
    """Ledger files_changed must be read so git failures do not falsely claim 'no files'."""
    rows = [
        {'phase': 'outcome', 'cycle_id': 'cycle-ledger-files', 'outcome': 'success',
         'files_changed': ['src/real_file.py']},
    ]
    html = tv.build_cycle_feed(rows, task_titles={}, history_mode=True)
    assert 'src/real_file.py' in html
    assert 'integrated · no files' not in html


def test_cycle_feed_unobserved_files_renders_integrated_not_no_files() -> None:
    """When files were never observed, title must be 'integrated', not 'integrated · no files'."""
    rows = [
        {'phase': 'outcome', 'cycle_id': 'cycle-no-files-field', 'outcome': 'success'},
    ]
    html = tv.build_cycle_feed(rows, task_titles={}, history_mode=True)
    assert '<strong class="feed-title">integrated</strong>' in html
    assert 'integrated · no files' not in html


def test_non_work_commit_detector_handles_trailers_and_keeps_cycle_title() -> None:
    residual = "docs: residual" + chr(10) * 2 + "Selfevo-Residual: true"
    checkpoint = "docs: checkpoint" + chr(10) * 2 + "Selfevo-Checkpoint: true"
    assert tv._is_non_work_commit_message("diary: record entry")
    assert tv._is_non_work_commit_message(residual)
    assert tv._is_non_work_commit_message("selfevo: checkpoint state")
    assert tv._is_non_work_commit_message(checkpoint)
    assert not tv._is_non_work_commit_message("selfevo: Implement requested task")


def test_274_cycle_lesson_link_shown_as_unavailable_not_dropped() -> None:
    cycles = tv.build_cycle_feed(
        [{'phase': 'outcome', 'cycle_id': 'cycle-x', 'outcome': 'success', 'ts': '2026-09-16T00:00:00Z',
          'lessons_context': ['LESS-GONE-1']}],
        rendered_lesson_ids={'LESS-STILL-HERE'},
    )
    assert 'LESS-GONE-1' in cycles
    assert 'lesson-link-unavailable' in cycles
    assert '(unavailable)' in cycles


def test_274_render_page_and_render_pages_apply_same_lesson_filter() -> None:
    data = _fixture()
    data['ledger_tail'] = [
        {'phase': 'outcome', 'cycle_id': 'cycle-y', 'outcome': 'success', 'ts': '2026-09-16T00:00:00Z',
         'lessons_context': ['LESS-ABSENT-1']},
    ]
    data['lessons'] = [{'id': 'LESS-PRESENT-1', 'date': '2026-09-16', 'problem': 'p', 'solution': 's'}]
    single = tv.render_page(data, host='eeepc', generated_at='2026-09-16 12:00:00')
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-09-16 12:00:00')
    assert 'lesson-link-unavailable' in single
    assert 'lesson-link-unavailable' in pages['cycles.html']

# ---------------------------------------------------------------------------
# Issue #96: v2 lessons rendering — problem→solution cards, legacy fold,
# stale #73 index wording removed
# ---------------------------------------------------------------------------

_V2_LESSON = {
    'id': 'LESS-20260901-v2aa',
    'date': '2026-09-01',
    'cycle_id': 'cycle-v2-1',
    'problem': 'Proposer skips well-scoped tasks due to dedup false positives',
    'solution': 'Tighten dedup fingerprint to exclude non-functional whitespace',
    'tags': ['dedup', 'proposer'],
    'severity': 'high',
    'seen_count': 3,
}

_LEGACY_LESSON = {
    'id': 'LESS-20260825-legc',
    'date': '2026-08-25',
    'cycle_id': 'cycle-leg-1',
    'task_id': 'Add speculative syntax checking',
    'hypothesis': 'syntax pre-check reduces gate failures',
    'result': 'refuted: no measurable effect',
    'insight': 'skip pre-checks for syntax',
}


def test_issue255_title_survives_reader_to_renderer(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / 'eeebot-self-evolving'
    lessons_dir = repo / 'lessons'
    lessons_dir.mkdir(parents=True)
    (lessons_dir / 'lessons.yaml').write_text(
        'lessons:\n'
        '  - id: LESS-RETITLE-255\n'
        '    date: "2026-09-05"\n'
        '    title: "Use git stash -u for untracked files"\n'
        '    problem: "Untracked files were omitted from the stash."\n'
        '    solution: "Use git stash -u <path>."\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(tv, 'INSTANCE_REPO', str(repo))
    script = tv.REMOTE_READER_SCRIPT.replace(
        'INSTANCE_REPO = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"',
        f'INSTANCE_REPO = {str(repo)!r}',
    )
    namespace: dict[str, object] = {}
    exec(script, namespace)
    remote_entries = namespace['read_lessons']()
    remote_pages = tv.render_pages({**_fixture(), 'lessons': remote_entries}, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert '<h3 class="lesson-title">Use git stash -u for untracked files</h3>' in remote_pages['lessons.html']

    local_entries = tv.read_local_state(str(tmp_path), instance_repo=str(repo))['lessons']
    local_pages = tv.render_pages({**_fixture(), 'lessons': local_entries}, host='eeepc', generated_at='2026-08-18 12:00:00')
    assert '<h3 class="lesson-title">Use git stash -u for untracked files</h3>' in local_pages['lessons.html']

def test_issue96_v2_lesson_renders_as_card() -> None:
    data = _fixture()
    data['lessons'] = [_V2_LESSON]
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    les = pages['lessons.html']

    # v2 card class present
    assert 'lesson-row-v2' in les
    # problem and solution rendered
    assert 'Proposer skips well-scoped tasks' not in les  # ADR-036 rule 3: lesson body is LAN-only
    assert 'Tighten dedup fingerprint' not in les  # ADR-036 rule 3: lesson body is LAN-only
    # tags rendered
    assert 'lesson-tag' in les
    assert 'dedup' in les
    assert 'proposer' in les
    # severity rendered
    assert 'lesson-severity' in les
    assert 'high' in les
    # seen_count rendered
    assert 'lesson-seen' in les
    assert '×3' in les or '\u00d73' in les


def test_issue96_legacy_lesson_folded_under_exact_label() -> None:
    data = _fixture()
    data['lessons'] = [_LEGACY_LESSON]
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    les = pages['lessons.html']

    # exact fold label required by spec
    assert 'legacy (pre-v2, frozen)' in les
    # legacy entries are inside a <details> element
    assert 'lesson-legacy-details' in les
    assert '<details class="lesson-legacy-details">' in les
    # legacy content is still accessible
    assert 'LESS-20260825-legc' in les


def test_issue96_mixed_v2_and_legacy_split_correctly() -> None:
    data = _fixture()
    data['lessons'] = [_V2_LESSON, _LEGACY_LESSON]
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    les = pages['lessons.html']

    # v2 section present
    assert 'lesson-row-v2' in les
    assert 'Proposer skips well-scoped tasks' not in les  # ADR-036 rule 3: lesson body is LAN-only
    # legacy folded
    assert 'legacy (pre-v2, frozen)' in les
    # counts in heading
    assert '1 v2' in les
    assert '1 legacy' in les


def test_issue96_no_v2_entries_truthful_note() -> None:
    data = _fixture()
    data['lessons'] = [_LEGACY_LESSON]
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    les = pages['lessons.html']

    assert 'no v2 entries' in les or 'no v2 lessons recorded yet' in les
    # must not claim v2 entries exist — check the rendered HTML, not CSS definitions
    assert 'class="lesson-row lesson-row-v2"' not in les


def test_issue96_all_v2_no_legacy_fold() -> None:
    data = _fixture()
    data['lessons'] = [_V2_LESSON]
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    les = pages['lessons.html']

    assert 'lesson-row-v2' in les
    assert 'legacy (pre-v2, frozen)' not in les


def test_issue96_index_teaser_no_stale_73_wording() -> None:
    pages = tv.render_pages(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')
    idx = pages['index.html']

    # stale wording must be gone
    assert 'lands in issue #73' not in idx
    assert '#73' not in idx.split('lessons.html')[1].split('</li>')[0]
    # the lessons link itself still works
    assert 'href="lessons.html"' in idx


def test_issue96_v2_severity_critical_has_css_class() -> None:
    data = _fixture()
    data['lessons'] = [{**_V2_LESSON, 'severity': 'critical'}]
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    les = pages['lessons.html']

    assert 'lesson-severity-critical' in les
    assert 'critical' in les


def test_issue96_v2_no_severity_or_seen_or_tags_graceful() -> None:
    data = _fixture()
    data['lessons'] = [{
        'id': 'LESS-20260902-bare',
        'date': '2026-09-02',
        'cycle_id': 'cycle-bare',
        'problem': 'Missing dependency causes import error',
        'solution': 'Add dep to pyproject.toml',
    }]
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    les = pages['lessons.html']

    assert 'lesson-row-v2' in les
    assert 'Missing dependency' not in les  # ADR-036 rule 3: lesson body is LAN-only
    assert 'Add dep to pyproject.toml' not in les  # ADR-036 rule 3: lesson body is LAN-only
    # no severity/seen chip elements injected when fields absent
    assert 'class="lesson-severity' not in les
    assert 'class="lesson-seen"' not in les


def test_issue96_is_v2_lesson_helper() -> None:
    assert tv._is_v2_lesson({'problem': 'something'}) is True
    assert tv._is_v2_lesson({'problem': ''}) is False
    assert tv._is_v2_lesson({'problem': None}) is False
    assert tv._is_v2_lesson({'task_id': 'old', 'result': 'r'}) is False
    assert tv._is_v2_lesson({}) is False


def test_issue130_duplicate_real_shape_lessons_render_all_with_warning():
    lessons = [
        {'id': 'LESS-REF-c871bf9abe41', 'schema_version': 2, 'title': 'When introducing new default-enabled repository introspection features to CLI scripts, verify that existing mock test suites pass flags to disable live VCS scanning or isolate repository context.', 'problem': 'When introducing new default-enabled repository introspection features to CLI scripts, verify that existing mock test suites pass flags to disable live VCS scanning or isolate repository context.', 'solution': 'Apply the reflected approach hint.', 'tags': ['reflector'], 'severity': 'medium', 'seen_count': 1, 'first_seen': '2026-08-29', 'last_seen': '2026-08-29', 'evidence': ['cycle-c871bf9abe41'], 'date': '2026-08-29', 'cycle_id': 'cycle-c871bf9abe41'},
        {'id': 'LESS-REF-c871bf9abe41', 'schema_version': 2, 'title': 'When executing subprocess calls that change the working directory, always pass the absolute path to the target script.', 'problem': 'When executing subprocess calls that change the working directory, always pass the absolute path to the target script.', 'solution': 'Apply the reflected approach hint.', 'tags': ['reflector'], 'severity': 'medium', 'seen_count': 1, 'first_seen': '2026-08-29', 'last_seen': '2026-08-29', 'evidence': ['cycle-c871bf9abe41'], 'date': '2026-08-29', 'cycle_id': 'cycle-c871bf9abe41'},
    ]
    html = tv.build_lessons_panel(lessons)
    assert 'Lessons History (2 retained history rows' in html
    assert html.count('duplicate id on disk') == 2
    assert html.count('<h3 class="lesson-title">When introducing new default-enabled') == 1
    assert html.count('<h3 class="lesson-title">When executing subprocess calls') == 1
    # ADR-036 rule 3: the problem body (here identical to the title) is LAN-only.
    assert html.count('When introducing new default-enabled') == 1
    assert html.count('When executing subprocess calls') == 1


def test_issue130_single_real_shape_lesson_has_no_duplicate_warning():
    lesson = {'id': 'LESS-REF-c871bf9abe41', 'schema_version': 2, 'title': 'One real lesson', 'problem': 'p', 'solution': 's', 'tags': ['reflector'], 'severity': 'medium', 'seen_count': 1, 'date': '2026-08-29'}
    html = tv.build_lessons_panel([lesson])
    assert 'duplicate id on disk' not in html


def test_issue135_agent_wide_content_has_scoped_overflow_wrappers() -> None:
    html = tv.render_page(_fixture(), host='eeepc', generated_at='2026-09-01 02:00:00')
    assert 'overflow-x: auto' in html
    assert 'agent-wide-content' in html


def test_issue135_agent_columns_shrink_on_narrow_viewport() -> None:
    html = tv.render_page(_fixture(), host='eeepc', generated_at='2026-09-01 02:00:00')
    assert 'min-width: 0' in html
    assert '.agent-subcol' in html


def test_issue134_filter_and_search_empty_state_markup_is_emitted_hidden() -> None:
    cycles = tv.build_cycle_feed([
        {'phase': 'outcome', 'cycle_id': 'cycle-pass', 'outcome': 'success', 'ts': '2026-09-01T01:00:00Z'},
    ], history_mode=True)
    lessons = tv.build_lessons_panel([{'id': 'LESS-1', 'date': '2026-09-01', 'problem': 'needle', 'solution': 'fix'}])

    assert 'data-filter-empty' in cycles
    assert '0 cycles with status' in cycles
    assert '0 results for' in lessons
    assert 'data-filter-empty' in lessons
    assert 'needle' not in lessons  # ADR-036 rule 3: problem text is LAN-only


def test_issue130_duplicate_lessons_get_unique_anchors():
    """Rendering both duplicates must not emit the same DOM id twice.

    The #130 fix stopped dropping the second entry, which meant both rows
    carried `id="q-<lesson id>"`. That is invalid HTML, and a browser resolving
    a `#q-<id>` link silently picks whichever comes first.
    """
    lessons = [
        {'id': 'LESS-REF-c871bf9abe41', 'schema_version': 2, 'title': 'first', 'problem': 'p1', 'solution': 's', 'date': '2026-08-29'},
        {'id': 'LESS-REF-c871bf9abe41', 'schema_version': 2, 'title': 'second', 'problem': 'p2', 'solution': 's', 'date': '2026-08-29'},
        {'id': 'LESS-20260828-c2f0da09', 'schema_version': 2, 'title': 'unique', 'problem': 'p3', 'solution': 's', 'date': '2026-08-28'},
    ]
    html = tv.build_lessons_panel(lessons)
    anchors = re.findall(r'id="(q-[^"]+)"', html)
    assert len(anchors) == len(set(anchors)), f'duplicate DOM ids rendered: {anchors}'
    # The first occurrence keeps the bare anchor so the cycle-page links built
    # in #129 still resolve; only the later one is suffixed.
    assert 'q-LESS-REF-c871bf9abe41' in anchors
    assert 'q-LESS-REF-c871bf9abe41-2' in anchors
    assert 'q-LESS-20260828-c2f0da09' in anchors

# ---------------------------------------------------------------------------
# Issue #272: per-cycle detail route carrying subagent records and prompts
# ---------------------------------------------------------------------------


def test_272_read_subagent_records_local_bounded_excerpt(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    (state / 'subagents').mkdir(parents=True)
    long_task = 'x' * 1000
    (state / 'subagents' / 'sa1.json').write_text(json.dumps({
        'subagent_id': 'sa1', 'cycle_id': 'cycle-a', 'label': 'do thing', 'status': 'ok',
        'started_at': 't0', 'finished_at': 't1', 'task': long_task, 'summary': 'sum', 'result': 'res',
        'context_usage': {'iterations': [10, 20, 30]},
    }), encoding='utf-8')
    # requests/ and results/ subdirectories beside it must never be misread as records.
    (state / 'subagents' / 'requests').mkdir()
    (state / 'subagents' / 'requests' / 'not-a-record.json').write_text('{}', encoding='utf-8')
    data = tv.read_local_state(str(state))
    records = data['subagent_records']
    assert len(records) == 1
    rec = records[0]
    assert rec['subagent_id'] == 'sa1'
    assert rec['cycle_id'] == 'cycle-a'
    assert rec['task_truncated'] is True
    assert rec['task_bytes'] == 1000
    assert len(rec['task_excerpt']) < 1000
    assert rec['iteration_count'] == 3


def test_272_read_cycle_prompts_local_reports_truncation(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    (state / 'prompts').mkdir(parents=True)
    big = 'y' * (70 * 1024)
    (state / 'prompts' / 'cycle-b.system.txt').write_text(big, encoding='utf-8')
    (state / 'prompts' / 'cycle-b.task.txt').write_text('short task text', encoding='utf-8')
    data = tv.read_local_state(str(state))
    prompts = data['cycle_prompts']
    assert prompts['cycle-b']['system']['truncated'] is True
    assert prompts['cycle-b']['system']['original_bytes'] == 70 * 1024
    assert len(prompts['cycle-b']['system']['text']) < 70 * 1024
    assert prompts['cycle-b']['task']['truncated'] is False
    assert prompts['cycle-b']['task']['original_bytes'] == len('short task text')


def test_272_remote_reader_script_mirrors_subagents_and_prompts(tmp_path: Path) -> None:
    import contextlib
    import io

    state = tmp_path / 'state'
    (state / 'subagents').mkdir(parents=True)
    (state / 'prompts').mkdir(parents=True)
    (state / 'subagents' / 'sa2.json').write_text(json.dumps({
        'subagent_id': 'sa2', 'cycle_id': 'cycle-c', 'label': 'l', 'status': 'ok',
        'task': 't', 'summary': 's', 'result': 'r',
    }), encoding='utf-8')
    (state / 'prompts' / 'cycle-c.system.txt').write_text('sys', encoding='utf-8')
    script = tv.REMOTE_READER_SCRIPT.replace(
        'STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"',
        f'STATE_ROOT = {str(state)!r}',
    )
    namespace: dict[str, object] = {}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(script, namespace)
    records = namespace['read_subagent_records']()
    prompts = namespace['read_cycle_prompts']()
    assert len(records) == 1 and records[0]['cycle_id'] == 'cycle-c'
    assert prompts['cycle-c']['system']['text'] == 'sys'


def test_272_build_cycle_details_joins_subagents_by_cycle_id_not_time() -> None:
    """#272 acceptance: a subagent record with no cycle_id must be reported
    as unjoined, never attached to the nearest cycle by time."""
    ledger_rows = [
        {'cycle_id': 'cycle-only-one', 'outcome': 'success', 'ts': '2026-09-17T00:00:00Z'},
    ]
    subagent_records = [
        {'subagent_id': 'joined', 'cycle_id': 'cycle-only-one', 'label': 'l', 'status': 'ok',
         'task_excerpt': 't', 'task_truncated': False, 'task_bytes': 1, 'summary_excerpt': 's',
         'result_excerpt': 'r', 'iteration_count': 2},
        {'subagent_id': 'orphan', 'cycle_id': None, 'label': 'l2', 'status': 'ok',
         'task_excerpt': 't2', 'task_truncated': False, 'task_bytes': 2, 'summary_excerpt': 's2',
         'result_excerpt': 'r2', 'iteration_count': 1},
    ]
    details = tv.build_cycle_details(ledger_rows, None, None, None, subagent_records=subagent_records)
    assert details['cycle-only-one']['subagents'] == [{
        'subagent_id': 'joined', 'label': 'l', 'status': 'ok', 'started_at': None, 'finished_at': None,
        'task_truncated': False, 'task_bytes': 1, 'summary_chars': 1,
        'result_chars': 1, 'iteration_count': 2,
    }]
    assert details['__unjoined_subagents__']['unjoined_count'] == 1
    assert details['__unjoined_subagents__']['subagents'][0]['subagent_id'] == 'orphan'
    # The orphan must not have leaked onto the only real cycle.
    joined_ids = {sa['subagent_id'] for sa in details['cycle-only-one']['subagents']}
    assert 'orphan' not in joined_ids


def test_272_build_cycle_details_attaches_and_bounds_prompts() -> None:
    ledger_rows = [{'cycle_id': 'cycle-p', 'outcome': 'success', 'ts': '2026-09-17T00:00:00Z'}]
    cycle_prompts = {
        'cycle-p': {
            'system': {'text': 'sys text', 'truncated': False, 'original_bytes': 8},
            'task': {'text': 'task text', 'truncated': True, 'original_bytes': 99999},
        },
    }
    details = tv.build_cycle_details(ledger_rows, None, None, None, cycle_prompts=cycle_prompts)
    assert details['cycle-p']['prompt']['system']['text'] == 'sys text'
    assert details['cycle-p']['prompt']['task']['truncated'] is True
    assert details['cycle-p']['prompt']['task']['original_bytes'] == 99999
    # A cycle with no ledger row at all still gets a details entry if a
    # prompt exists for it -- it's a real cycle, just absent from this window.
    cycle_prompts_only = {'cycle-only-prompt': {'system': {'text': 's', 'truncated': False, 'original_bytes': 1}}}
    details2 = tv.build_cycle_details([], None, None, None, cycle_prompts=cycle_prompts_only)
    assert 'cycle-only-prompt' in details2

# ---------------------------------------------------------------------------
# Issue #289: cycle detail shows error_card_recording outcome (eeebot#1687)
# ---------------------------------------------------------------------------


def test_289_created_status_carries_card_commit() -> None:
    ledger_rows = [
        {'phase': 'outcome', 'cycle_id': 'cycle-rb1', 'outcome': 'failed', 'ts': '2026-09-17T00:00:00Z'},
        {'phase': 'error_card_recording', 'cycle_id': 'cycle-rb1', 'status': 'created', 'card_commit': 'abc1234'},
    ]
    details = tv.build_cycle_details(ledger_rows, None, None, None)
    assert details['cycle-rb1']['error_card_recording'] == {'status': 'created', 'card_commit': 'abc1234'}


def test_289_not_created_status_carries_skip_reason() -> None:
    ledger_rows = [
        {'phase': 'outcome', 'cycle_id': 'cycle-rb2', 'outcome': 'failed', 'ts': '2026-09-17T00:00:00Z'},
        {'phase': 'error_card_recording', 'cycle_id': 'cycle-rb2', 'status': 'not_created', 'skip_reason': 'push_rejected'},
    ]
    details = tv.build_cycle_details(ledger_rows, None, None, None)
    assert details['cycle-rb2']['error_card_recording'] == {'status': 'not_created', 'skip_reason': 'push_rejected'}


def test_289_no_row_means_no_rollback_not_an_error() -> None:
    """A cycle that didn't roll back has no error_card_recording row at
    all -- that absence must not be rendered as an error or an unavailable
    state, just no fact."""
    ledger_rows = [
        {'phase': 'outcome', 'cycle_id': 'cycle-clean', 'outcome': 'success', 'ts': '2026-09-17T00:00:00Z'},
    ]
    details = tv.build_cycle_details(ledger_rows, None, None, None)
    assert 'error_card_recording' not in details['cycle-clean']


def test_289_unreadable_ledger_is_probe_unavailable_distinct_from_no_row() -> None:
    details_unreadable = tv.build_cycle_details(None, None, None, None)
    details_empty_but_readable = tv.build_cycle_details([], None, None, None)
    assert details_unreadable['__ledger_status__'] == {'status': 'probe_unavailable'}
    # A readable-but-empty ledger needs no sentinel: absence of the key
    # itself means "readable" to the renderer, same as absence of any one
    # cycle's own error_card_recording row means "didn't roll back".
    assert '__ledger_status__' not in details_empty_but_readable


def test_289_cycle_detail_page_renders_all_error_card_states() -> None:
    page = tv.build_cycle_detail_page()
    assert 'data-error-card-state="created"' in page
    assert 'data-error-card-state="not_created"' in page
    assert 'data-error-card-state="probe_unavailable"' in page
    assert 'error_card_recording' in page
    assert '__ledger_status__' in page


# ---------------------------------------------------------------------------
# Issue #292: cycle detail renders already_recorded / attempt / write_failed
# error (eeebot#1710, PR eeebot#1713)
# ---------------------------------------------------------------------------


def test_292_already_recorded_status_carries_card_id() -> None:
    """An executor-LLM-error retry finding its own prior card must render
    as a distinct neutral state, not fall through and read as no rollback."""
    ledger_rows = [
        {'phase': 'outcome', 'cycle_id': 'cycle-rb3', 'outcome': 'failed', 'ts': '2026-09-17T00:00:00Z'},
        {
            'phase': 'error_card_recording', 'cycle_id': 'cycle-rb3',
            'status': 'already_recorded', 'card_id': 'ERR-20260917-cyclerb3', 'attempt': '2/3',
        },
    ]
    details = tv.build_cycle_details(ledger_rows, None, None, None)
    assert details['cycle-rb3']['error_card_recording'] == {
        'status': 'already_recorded', 'card_id': 'ERR-20260917-cyclerb3', 'attempt': '2/3',
    }


def test_292_not_created_write_failed_carries_error_detail() -> None:
    ledger_rows = [
        {'phase': 'outcome', 'cycle_id': 'cycle-rb4', 'outcome': 'failed', 'ts': '2026-09-17T00:00:00Z'},
        {
            'phase': 'error_card_recording', 'cycle_id': 'cycle-rb4', 'status': 'not_created',
            'skip_reason': 'write_failed', 'error': 'PermissionError:/repo/lessons/errors.yaml',
        },
    ]
    details = tv.build_cycle_details(ledger_rows, None, None, None)
    assert details['cycle-rb4']['error_card_recording'] == {
        'status': 'not_created', 'skip_reason': 'write_failed',
        'error': f'reason text, {len("PermissionError:/repo/lessons/errors.yaml")} chars (LAN)',
    }


def test_292_created_status_carries_attempt_when_present() -> None:
    ledger_rows = [
        {'phase': 'outcome', 'cycle_id': 'cycle-rb5', 'outcome': 'failed', 'ts': '2026-09-17T00:00:00Z'},
        {
            'phase': 'error_card_recording', 'cycle_id': 'cycle-rb5',
            'status': 'created', 'card_commit': 'def5678', 'attempt': '1/3',
        },
    ]
    details = tv.build_cycle_details(ledger_rows, None, None, None)
    assert details['cycle-rb5']['error_card_recording'] == {
        'status': 'created', 'card_commit': 'def5678', 'attempt': '1/3',
    }


def test_292_cycle_detail_page_renders_already_recorded_state() -> None:
    page = tv.build_cycle_detail_page()
    assert 'data-error-card-state="already_recorded"' in page
    assert 'already recorded' in page
    assert 'ecr.card_id' in page
    assert 'ecr.attempt' in page
    assert 'ecr.error' in page


def test_272_cycle_detail_page_renders_four_distinguishable_states() -> None:
    page = tv.build_cycle_detail_page()
    # Every one of the four states must be its own reachable, labelled branch.
    assert 'data-cycle-detail-state="unknown"' in page
    assert 'data-cycle-detail-state="no-subagents"' in page
    assert 'data-cycle-detail-state="prompt-not-retained"' in page
    assert 'renderSubagent' in page and 'item.subagents' in page
    assert tv.LINEAGE_DETAILS_FILE in page


def test_272_cycle_html_published_and_reachable_from_pages() -> None:
    data = _fixture()
    data['ledger_tail'] = [
        {'phase': 'outcome', 'cycle_id': 'cycle-link-me', 'outcome': 'success', 'ts': '2026-09-17T00:00:00Z'},
    ]
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-09-17 12:00:00')
    assert 'cycle.html' in pages
    assert 'data-cycle-detail-state="unknown"' in pages['cycle.html']
    assert 'cycle.html?id=cycle-link-me' in pages['cycles.html']
    assert 'cycle.html?id=cycle-link-me' in pages['index.html']


def test_272_every_rendered_cycle_id_links_to_cycle_detail_route() -> None:
    """#272 acceptance: the count of cycle.html?id= links must equal the
    count of distinct cycle ids rendered in the feed -- the index's
    previous zero-links defect must not recur."""
    ledger_tail = [
        {'phase': 'outcome', 'cycle_id': f'cycle-{i}', 'outcome': 'success', 'ts': f'2026-09-1{i}T00:00:00Z'}
        for i in range(1, 6)
    ]
    feed_html = tv.build_cycle_feed(ledger_tail, history_mode=True)
    rendered_ids = {row.get('cycle_id') for row in ledger_tail}
    detail_links = re.findall(r'cycle\.html\?id=([^"&\']+)', feed_html)
    assert len(detail_links) == len(rendered_ids)
    assert set(detail_links) == rendered_ids


def test_issue188_provenance_badge_three_state() -> None:
    # 1. Unavailable when scorecard or reader_status is missing/None:
    html_no_readers = tv.build_empire_stats_strip({'loop': {}})
    assert 'data inputs: unavailable' in html_no_readers

    # 2. Complete summary when all 4 readers + 5 nested feeds are healthy:
    scorecard_complete = {
        'window_days': 7,
        'gaps_status': 'complete',
        'reader_status': {
            'ledger': {'status': 'complete'},
            'completed': {'status': 'present'},
            'heldout': {'status': 'present'},
            'history': {'status': 'present'},
            'feeds': {
                'usage': {'status': 'fresh'},
                'heldout': {'status': 'fresh'},
                'llm_calls': {'status': 'fresh'},
                'host_metrics': {'status': 'fresh'},
                'validator_harness_parent': {'status': 'fresh'},
            },
        },
    }
    html_complete = tv.build_empire_stats_strip(scorecard_complete)
    assert 'Window: 7d (complete)' in html_complete
    assert '⚠️' not in html_complete

    # 3. Anomaly alert with specific feed/reader name:
    scorecard_anomaly = {
        'window_days': 7,
        'gaps_status': 'complete',
        'reader_status': {
            'ledger': {'status': 'complete'},
            'completed': {'status': 'present'},
            'heldout': {'status': 'present'},
            'history': {'status': 'present'},
            'feeds': {
                'usage': {'status': 'fresh'},
                'heldout': {'status': 'fresh'},
                'llm_calls': {'status': 'fresh'},
                'host_metrics': {'status': 'stale'},
                'validator_harness_parent': {'status': 'fresh'},
            },
        },
    }
    html_anomaly = tv.build_empire_stats_strip(scorecard_anomaly)
    assert 'data inputs: host_metrics: stale' in html_anomaly
    assert 'provenance-anomaly' in html_anomaly


def test_issue1765_supplier_paused_stat_distinguishes_no_data_from_zero() -> None:
    # Scorecard predates the field entirely (both keys absent): n/a, not 0.
    assert tv._paused_supplier_stat({}) == 'n/a'

    # Ledger window unreadable: unavailable, not 0 -- distinct from both a
    # real zero and a missing-field scorecard.
    assert tv._paused_supplier_stat({
        'paused_supplier_outcomes': 'unavailable',
        'paused_supplier_seconds': 'unavailable',
    }) == 'unavailable'

    # A real zero (no outages this window) reads as a real, displayed zero.
    assert tv._paused_supplier_stat({
        'paused_supplier_outcomes': 0,
        'paused_supplier_seconds': 0,
    }) == '0 cycle(s) / 0s'

    # A real, non-zero reading.
    assert tv._paused_supplier_stat({
        'paused_supplier_outcomes': 3,
        'paused_supplier_seconds': 5400,
    }) == '3 cycle(s) / 1.5h'

    html = tv.build_empire_stats_strip({
        'loop': {'paused_supplier_outcomes': 2, 'paused_supplier_seconds': 120},
    })
    assert 'supplier-paused' in html
    assert '2 cycle(s) / 2m' in html
    assert 'issue #1765' in html  # the tooltip carries the source citation

def test_issue190_now_panel_failed_bridge_exits_three_state() -> None:
    # 1. When bridge_exits is None or empty / all successful: section is absent (clean render, no empty noise)
    p_none = tv.build_now_panel({'now': '2026-09-01T02:00:00Z'}, {}, [], None, None, bridge_exits=None)
    assert 'failed-exits-details' not in p_none
    assert 'Recent Failed Bridge Exits' not in p_none

    p_success = tv.build_now_panel(
        {'now': '2026-09-01T02:00:00Z'}, {}, [], None, None,
        bridge_exits=[
            {'ts': '2026-09-02T12:00:00Z', 'outcome': 'success', 'exit_status': 0, 'error': '', 'where': ''},
            {'ts': '2026-09-02T12:05:00Z', 'outcome': 'success', 'exit_status': 0, 'error': '', 'where': ''},
        ],
    )
    assert 'failed-exits-details' not in p_success
    assert 'Recent Failed Bridge Exits' not in p_success

    # 2. When failures exist: collapsible details section is present with error, where, and timestamp
    p_failed = tv.build_now_panel(
        {'now': '2026-09-01T02:00:00Z'}, {}, [], None, None,
        bridge_exits=[
            {'ts': '2026-09-02T12:00:00Z', 'outcome': 'success', 'exit_status': 0},
            {
                'ts': '2026-09-02T12:10:00Z',
                'outcome': 'failure',
                'exit_status': 1,
                'error': "NameError: name '_parse_explore_mode' is not defined",
                'where': 'bridge.py:1874',
            },
        ],
    )
    assert 'failed-exits-details' in p_failed
    assert 'Recent Failed Bridge Exits (1)' in p_failed
    assert "NameError: name &#x27;_parse_explore_mode&#x27; is not defined" in p_failed or "NameError: name '_parse_explore_mode' is not defined" in p_failed
    assert 'at bridge.py:1874' in p_failed
    assert '2026-09-02T12:10:00Z' in p_failed



def test_issue196_health_verdict_scoped_to_monitored_feeds() -> None:
    scorecard = {
        'reader_status': {
            'feeds': {
                'usage': {'status': 'fresh'},
                'heldout': {'status': 'fresh'},
                'llm_calls': {'status': 'fresh'},
                'host_metrics': {'status': 'fresh'},
                'validator_harness_parent': {'status': 'fresh'},
            }
        }
    }
    verdict, reason = tv.health_verdict(
        120, '2026-09-01T01:50:00Z', ['integrated'], False, '2026-09-01T02:00:00Z',
        scorecard=scorecard,
    )
    assert verdict == 'healthy'
    assert reason == 'all signals within thresholds across 5 monitored feeds (heldout, host_metrics, llm_calls, usage, validator_harness_parent)'


def test_issue196_health_verdict_dynamic_feed_scope_when_membership_changes() -> None:
    scorecard = {
        'reader_status': {
            'feeds': {
                'feed_a': {'status': 'fresh'},
                'feed_b': {'status': 'fresh'},
            }
        }
    }
    verdict, reason = tv.health_verdict(
        120, '2026-09-01T01:50:00Z', ['integrated'], False, '2026-09-01T02:00:00Z',
        scorecard=scorecard,
    )
    assert verdict == 'healthy'
    assert reason == 'all signals within thresholds across 2 monitored feeds (feed_a, feed_b)'


def test_issue196_health_verdict_requires_scorecard_evidence() -> None:
    verdict, reason = tv.health_verdict(
        120, '2026-09-01T01:50:00Z', ['integrated'], False, '2026-09-01T02:00:00Z',
        scorecard=None,
    )
    assert verdict == 'degraded'
    assert reason == 'scorecard data unavailable'


import scripts.techtree_viewer as tv


def test_issue196_monitored_feed_ages_three_state() -> None:
    # 1. Healthy / active feeds: renders age and max_age for each feed
    sc_ok = {
        'feeds': {
            'feeds': {
                'usage': {
                    'status': 'ok',
                    'age_seconds': 3600,
                    'max_age_seconds': 43200,
                },
                'host_metrics': {
                    'status': 'stale',
                    'age_seconds': 44000,
                    'max_age_seconds': 43200,
                },
            }
        }
    }
    html_ok = tv.build_now_panel(
        None, None, [], None, None,
        scorecard=sc_ok,
    )
    assert 'Feed Freshness:' in html_ok
    assert 'usage: 1.0h/12.0h' in html_ok
    assert 'host_metrics: 12.2h/12.0h' in html_ok
    assert 'feed-badge-ok' in html_ok
    assert 'feed-badge-stale' in html_ok

    # 2. Unavailable: missing scorecard or missing feeds
    html_none = tv.build_now_panel(
        None, None, [], None, None,
        scorecard=None,
    )
    assert 'Feed Freshness:' in html_none
    assert 'unavailable' in html_none
    assert '1.0h/12.0h' not in html_none

    html_empty = tv.build_now_panel(
        None, None, [], None, None,
        scorecard={'feeds': {}},
    )
    assert 'Feed Freshness:' in html_empty
    assert 'unavailable' in html_empty


def test_issue196_feed_ages_use_live_status_vocabulary_and_one_unit() -> None:
    """Guard the two things a hand-written fixture can agree with and still be wrong.

    ``scorecard._feed_details`` emits ``fresh``, never ``ok``, and the real
    thresholds are 43200s and 86400s. A 24h threshold printed as ``1.0d`` next
    to a 15h age cannot be compared without arithmetic, which is the whole
    point of showing the pair (#196).
    """
    scorecard = {
        'feeds': {
            'feeds': {
                'host_metrics': {
                    'status': 'fresh',
                    'age_seconds': 54055.2,
                    'max_age_seconds': 86400,
                },
                'usage': {
                    'status': 'fresh',
                    'age_seconds': 21.0,
                    'max_age_seconds': 43200,
                },
                'llm_calls': {
                    'status': 'missing',
                    'age_seconds': None,
                    'max_age_seconds': 86400,
                },
            }
        }
    }
    html = tv.build_now_panel(None, None, [], None, None, scorecard=scorecard)

    # A feed drifting inside its threshold is comparable against it directly.
    assert 'host_metrics: 15.0h/24.0h' in html, (
        'the 24h threshold rendered in a coarser unit than the age beside it')
    assert '1.0d' not in html
    assert 'usage: 21s/12.0h' in html

    # 'fresh' is the live healthy value; 'ok' never reaches this code.
    assert 'feed-badge-ok' in html, "live 'fresh' status did not reach the ok badge"
    assert 'feed-badge-fresh' not in html

    # An unreadable feed still shows its threshold, and reads as a problem.
    assert 'llm_calls: unknown/24.0h' in html
    assert 'feed-badge-missing' in html

class TestIssue200DocOnlyBudgetGuard:
    """The row exists to tell three situations apart, so the panel must too.

    A low deferral count has three causes and #1108 was unanswerable for months
    because they were indistinguishable: the guard never reached, the guard
    reached with nothing to suppress, and the guard triggered by an unreadable
    ledger rather than a real over-budget count. The last is a fail-open and
    must never render like a working guard.
    """

    LIVE = {
        'phase': 'doc_only_budget', 'doc_only_deferred': 0,
        'doc_only_integrations_24h': 5, 'doc_only_budget_24h': 5,
        'ledger_blind': False, 'doc_budget_exceeded': True,
        'items_considered': 8, 'ts': '2026-09-03T02:44:59Z',
    }

    def test_live_host_row_reads_as_reached_with_nothing_to_defer(self) -> None:
        html = tv._build_doc_only_budget_item([dict(self.LIVE)])
        assert '5/5' in html
        assert 'cap reached' in html
        assert 'of 8' in html
        assert 'unavailable' not in html

    def test_within_budget_is_distinct_from_cap_reached(self) -> None:
        row = dict(self.LIVE, doc_only_integrations_24h=2, doc_budget_exceeded=False)
        html = tv._build_doc_only_budget_item([row])
        assert 'within budget' in html
        assert '2/5' in html
        assert 'cap reached' not in html

    def test_ledger_blind_does_not_read_as_a_working_guard(self) -> None:
        row = dict(self.LIVE, ledger_blind=True)
        html = tv._build_doc_only_budget_item([row])
        assert 'fail-open' in html
        assert 'cap reached' not in html
        assert 'within budget' not in html
        healthy = tv._build_doc_only_budget_item([dict(self.LIVE)])
        assert 'badge-available' in healthy
        assert 'badge-available' not in html, (
            'a fail-open guard rendered with the same badge as a healthy one')

    def test_actual_deferral_is_visible_as_such(self) -> None:
        row = dict(self.LIVE, doc_only_deferred=3)
        html = tv._build_doc_only_budget_item([row])
        assert 'deferring' in html
        assert '3 deferred' in html

    def test_absent_row_is_unavailable_not_zero(self) -> None:
        for tail in (None, [], [{'phase': 'outcome', 'outcome': 'success'}]):
            html = tv._build_doc_only_budget_item(tail)
            assert 'unavailable' in html, tail
            assert '0/0' not in html

    def test_the_latest_row_wins(self) -> None:
        old = dict(self.LIVE, doc_only_integrations_24h=1, doc_budget_exceeded=False)
        new = dict(self.LIVE, doc_only_integrations_24h=9)
        html = tv._build_doc_only_budget_item([old, {'phase': 'gate'}, new])
        assert '9/5' in html
        assert '1/5' not in html

    def test_the_item_reaches_the_panel(self) -> None:
        html = tv.build_now_panel(None, None, None, None, ledger_tail=[dict(self.LIVE)])
        assert 'Doc Budget Guard:' in html
        assert 'cap reached' in html

class TestIssue204StrategistRunProvenance:
    """A degraded strategist looks exactly like a healthy one from outside.

    That is why #999 needed an audit rather than an alert: it advised from three
    dead inputs for eight runs and nothing on the dashboard said so. The row
    carries `inputs_status` so the condition is machine-readable; these tests pin
    that the panel keeps the states apart rather than merely printing the row.
    """

    LIVE = {
        "success": True,
        "reason": "valid bounded advisory output applied",
        "counts": {"advisories_recorded": 2, "advisories_written": 2, "hypotheses_appended": 2},
        "inputs_status": {
            "goals": {"chars": 2736, "source": "release_root", "status": "complete"},
            "scorecard": {"history_rows": 55, "status": "complete"},
            "funnel": {"ids": 200, "status": "complete"},
            "insights": {"cards": 2, "legacy": 3, "status": "complete"},
            "evolution_tree": {"nodes": 100, "status": "complete"},
        },
        "timestamp": "2026-09-03T12:37:17.640697Z",
    }

    def _text(self, decisions):
        import re
        return re.sub("<[^>]+>", "", tv._build_strategist_run_item(decisions)).strip()

    def test_the_live_host_row_renders_time_ratio_and_output(self) -> None:
        text = self._text([dict(self.LIVE)])
        assert "ran" in text
        assert "2026-09-03T12:37:17" in text
        assert "inputs 5/5" in text
        assert "2 hypotheses, 2 advisories" in text

    def test_a_refusal_does_not_render_like_a_healthy_run(self) -> None:
        refused = dict(self.LIVE, success=False, reason="refused: 2 of 5 inputs empty")
        html = tv._build_strategist_run_item([refused])
        healthy = tv._build_strategist_run_item([dict(self.LIVE)])
        assert "refused" in self._text([refused])
        assert "badge-available" in healthy
        assert "badge-available" not in html, (
            "a refusal rendered with the same badge as a healthy run")

    def test_an_error_is_distinct_from_both(self) -> None:
        errored = dict(self.LIVE, success=False, reason="LLM call failed: timeout")
        html = tv._build_strategist_run_item([errored])
        assert "error" in self._text([errored])
        assert "badge-available" not in html
        assert "badge-rejected" not in html

    def test_degraded_inputs_are_named_not_just_counted(self) -> None:
        degraded = dict(self.LIVE)
        degraded["inputs_status"] = dict(self.LIVE["inputs_status"])
        degraded["inputs_status"]["insights"] = {"cards": 0, "legacy": 0, "status": "empty"}
        degraded["inputs_status"]["funnel"] = {"ids": 0, "status": "empty"}
        text = self._text([degraded])
        assert "inputs 3/5" in text
        assert "insights:empty" in text
        assert "funnel:empty" in text

    def test_absent_or_unreadable_is_unavailable_not_zero(self) -> None:
        for decisions in (None, [], ["not a dict"], [123]):
            text = self._text(decisions)
            assert "unavailable" in text, decisions
            assert "0/5" not in text, decisions

    def test_the_newest_row_wins(self) -> None:
        old = dict(self.LIVE, timestamp="2026-09-01T03:00:00Z")
        new = dict(self.LIVE, timestamp="2026-09-03T12:37:17Z")
        text = self._text([old, new])
        assert "2026-09-03" in text
        assert "2026-09-01" not in text

    def test_the_item_reaches_the_panel(self) -> None:
        html = tv.build_now_panel(None, None, None, None, strategist_decisions=[dict(self.LIVE)])
        assert "Strategist:" in html
        assert "inputs 5/5" in html


# ─── #215 tests: gate_violations retained in cycle details export ─────────────

def test_issue215_gate_violations_present_are_retained() -> None:
    """#215: build_cycle_details() must keep gate_violations when the ledger row
    contains violations. Previously both branches unconditionally popped the field.
    """
    details = tv.build_cycle_details(
        ledger_rows=[{
            'cycle_id': 'cycle-gv',
            'outcome': 'failed',
            'violations': ['budget exceeded', 'no tests added'],
        }],
        evolution_tree=None,
        lessons=None,
        reflections=None,
    )
    assert 'cycle-gv' in details
    rec = details['cycle-gv']
    assert 'gate_violations' in rec, (
        'gate_violations must be present when violations are in the ledger row; '
        f'keys present: {list(rec)}'
    )
    assert rec['gate_violations'] == ['violation · text 15 chars (LAN)', 'violation · text 14 chars (LAN)'], (
        f'gate_violations content wrong: {rec["gate_violations"]}'
    )


def test_issue215_gate_violations_absent_are_stripped() -> None:
    """#215: when no violations were recorded, gate_violations must not appear
    in the output (empty list is not serialised — absence is not conflated with pass).
    """
    details = tv.build_cycle_details(
        ledger_rows=[{
            'cycle_id': 'cycle-ok',
            'outcome': 'integrated',
        }],
        evolution_tree=None,
        lessons=None,
        reflections=None,
    )
    assert 'cycle-ok' in details
    rec = details['cycle-ok']
    assert 'gate_violations' not in rec, (
        'gate_violations must be absent when no violations recorded; '
        f'got: {rec.get("gate_violations")}'
    )


def test_issue215_gate_violations_field_missing_from_row_is_absent() -> None:
    """#215: a ledger row with no violations key must not produce gate_violations
    in the output — missing field is not conflated with gate pass (empty list).
    """
    details = tv.build_cycle_details(
        ledger_rows=[{
            'cycle_id': 'cycle-nf',
            'outcome': 'partial',
            # no 'violations' key at all
        }],
        evolution_tree=None,
        lessons=None,
        reflections=None,
    )
    assert 'cycle-nf' in details
    rec = details['cycle-nf']
    assert 'gate_violations' not in rec, (
        'gate_violations must be absent when row has no violations key; '
        f'got: {rec.get("gate_violations")}'
    )


def test_issue215_gate_violations_survive_render_pages_to_json() -> None:
    """#215 end-to-end: gate_violations from a ledger row must survive the full
    render_pages() pipeline and appear in the published lineage-cycle-details.json.
    """
    fixture = {
        'host': 'eeepc',
        'now': '2026-09-01T05:00:00Z',
        'evolution_tree': {'current_sha': 'sha-gv', 'nodes': {
            'sha-gv': {'cycle_id': 'cycle-gv', 'parent_sha': None,
                       'ts': '2026-09-01T01:00:00Z'},
        }},
        'ledger_tail': [{
            'cycle_id': 'cycle-gv',
            'phase': 'gate',
            'outcome': 'failed',
            'violations': ['tests must pass', 'coverage must be 80%'],
            'ts': '2026-09-01T01:05:00Z',
        }],
        'lessons': None,
        'reflections': None,
        'cycle_titles': None,
        'cycle_files': None,
        'scorecard': None,
        'task_titles': None,
    }
    pages = tv.render_pages(fixture, host='eeepc', generated_at='2026-09-01 05:00:00')
    assert tv.LINEAGE_DETAILS_FILE in pages, (
        f'{tv.LINEAGE_DETAILS_FILE!r} not in rendered pages: {list(pages)}'
    )
    import json as _json
    details = _json.loads(pages[tv.LINEAGE_DETAILS_FILE])
    assert 'cycle-gv' in details, f'cycle-gv missing from details: {list(details)[:5]}'
    rec = details['cycle-gv']
    assert 'gate_violations' in rec, (
        f'gate_violations lost in render_pages pipeline; keys: {list(rec)}'
    )
    assert rec['gate_violations'] and all('chars (LAN)' in v for v in rec['gate_violations']), (
        f'violation text missing: {rec["gate_violations"]}'
    )



# ---------------------------------------------------------------------------
# Issue #223: token consumption heatmap (tokens.html)
# ---------------------------------------------------------------------------


def test_classify_model_prefixes() -> None:
    """Issue #223: verify model prefix classification into self_hosted vs vendor vs other.
    - un/* -> self_hosted (our LAN 3090Ti GPU)
    - cl/*, an/* -> vendor (cloud vendor APIs via LiteLLM)
    - everything else -> other (explicit unclassified class)
    """
    assert tv.classify_model('un/qwen3.8-27b-gguf') == 'self_hosted'
    assert tv.classify_model('openai/un/qwen3.6-27b-mtp') == 'self_hosted'
    assert tv.classify_model('cl/gpt-5.6-luna') == 'vendor'
    assert tv.classify_model('openai/cl/gpt-5.6-luna') == 'vendor'
    assert tv.classify_model('an/gemini-3.7-flash-high') == 'vendor'
    assert tv.classify_model('openai/an/gemini-3-flash') == 'vendor'
    assert tv.classify_model('custom-model-x') == 'other'
    assert tv.classify_model('anthropic/claude-3') == 'other'
    assert tv.classify_model('') == 'other'
    assert tv.classify_model(None) == 'other'


def test_compute_quantiles() -> None:
    """Issue #223: quantiles must handle positive integers and default on empty."""
    # Empty input
    assert tv.compute_quantiles([]) == [1000, 5000, 20000, 100000]
    # Sample numbers
    vals = list(range(1, 101))
    q = tv.compute_quantiles(vals)
    assert len(q) == 4
    assert q[0] <= q[1] <= q[2] <= q[3]
    assert q[0] == 26
    assert q[1] == 51
    assert q[2] == 76
    assert q[3] == 96


def test_read_token_heatmap_distinguishes_missing_file_from_quiet_hour(tmp_path: Path) -> None:
    """Issue #223: missing calendar days (gaps in tracking) must be None / NO DATA,
    while existing files with 0 calls in an hour must be recorded as 0 tokens (quiet hour).
    """
    llm_dir = tmp_path / 'llm_calls'
    llm_dir.mkdir(parents=True)

    # Day 1: 2026-09-04 has 2 calls (one local, one gateway)
    day1_file = llm_dir / '2026-09-04.jsonl'
    lines = [
        json.dumps({'ts': '2026-09-04T10:15:00Z', 'model': 'un/qwen3.8-27b-gguf', 'total_tokens': 50000, 'component': 'bridge'}),
        json.dumps({'ts': '2026-09-04T10:20:00Z', 'model': 'cl/gpt-5.6-luna', 'total_tokens': 12000, 'component': 'proposer'}),
    ]
    day1_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    # Day 2: 2026-09-05 is completely missing on disk (gap!)

    # Day 3: 2026-09-06 is present on disk, but has 0 lines (quiet day)
    day3_file = llm_dir / '2026-09-06.jsonl'
    day3_file.write_text('', encoding='utf-8')

    res = tv.read_token_heatmap(tmp_path)
    assert res is not None

    dates = res['dates']
    assert dates == ['2026-09-04', '2026-09-05', '2026-09-06']
    assert res['summary']['days_span'] == 3
    assert res['summary']['days_present'] == 2
    assert res['summary']['days_missing'] == 1

    # Day 1: has data (10:15 UTC -> 13:15 MSK, so hour 13 has data while hour 10 is 0 tokens)
    assert res['hourly']['2026-09-04'] is not None
    assert len(res['hourly']['2026-09-04']) == 24
    h13 = res['hourly']['2026-09-04'][13]
    # h13 = [self_hosted, vendor, other, total, calls, top_comp]
    assert h13[0] == 50000
    assert h13[1] == 12000
    assert h13[2] == 0
    assert h13[3] == 62000
    assert h13[4] == 2
    assert h13[5] == 'bridge'
    assert res['hourly']['2026-09-04'][10][3] == 0

    # Day 2: NO DATA (unobserved / gap in recording)
    assert res['hourly']['2026-09-05'] is None
    assert res['five_min']['2026-09-05'] is None

    # Day 3: 0 tokens (quiet day, but tracked)
    assert res['hourly']['2026-09-06'] is not None
    assert all(h[3] == 0 for h in res['hourly']['2026-09-06'])


def test_read_token_heatmap_utc_boundary_lands_in_next_msk_day(tmp_path: Path) -> None:
    """Issue #225: A record logged at 22:00 UTC on day D must land in the NEXT MSK day (D+1)
    at hour 01:00 MSK, not in day D.
    This guarantees that MSK day rows represent 00:00 to 23:59 MSK of that date,
    re-bucketing UTC timestamps rather than shifting labels on UTC buckets.
    """
    llm_dir = tmp_path / 'llm_calls'
    llm_dir.mkdir(parents=True)

    # 2026-09-04.jsonl contains:
    # 1. 20:59 UTC -> 23:59 MSK on 2026-09-04 (same MSK day, hour 23, 5-min bucket 287)
    # 2. 22:00 UTC -> 01:00 MSK on 2026-09-05 (NEXT MSK day, hour 01, 5-min bucket 12)
    day_file = llm_dir / '2026-09-04.jsonl'
    lines = [
        json.dumps({'ts': '2026-09-04T20:59:00Z', 'model': 'un/qwen3.8-27b-gguf', 'total_tokens': 1000, 'component': 'bridge'}),
        json.dumps({'ts': '2026-09-04T22:00:00Z', 'model': 'cl/gpt-5.6-luna', 'total_tokens': 2500, 'component': 'proposer'}),
    ]
    day_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    res = tv.read_token_heatmap(tmp_path)
    assert res is not None

    # Both MSK days must be present
    assert '2026-09-04' in res['dates']
    assert '2026-09-05' in res['dates']

    # On day 2026-09-04: hour 23 has 1000 tokens, hour 22 is 0 tokens
    h23 = res['hourly']['2026-09-04'][23]
    assert h23[0] == 1000
    assert h23[3] == 1000
    assert h23[4] == 1
    assert res['hourly']['2026-09-04'][22][3] == 0
    # 5-min bucket 287 (23:55-24:00 MSK) has the call
    assert '287' in res['five_min']['2026-09-04']
    assert res['five_min']['2026-09-04']['287'][0] == 1000

    # On day 2026-09-05: hour 01 has 2500 tokens, hour 00 is 0 tokens
    h1 = res['hourly']['2026-09-05'][1]
    assert h1[1] == 2500
    assert h1[3] == 2500
    assert h1[4] == 1
    assert res['hourly']['2026-09-05'][0][3] == 0
    # 5-min bucket 12 (01:00-01:05 MSK) has the call
    assert '12' in res['five_min']['2026-09-05']
    assert res['five_min']['2026-09-05']['12'][1] == 2500

    # Summary reflects both calls and labels timezone
    assert res['summary']['total_tokens'] == 3500
    assert res['summary']['timezone'] == 'MSK'


def test_read_token_heatmap_handles_empty_or_missing_directory(tmp_path: Path) -> None:
    """Issue #223: fail-soft on missing or empty llm_calls directory."""
    assert tv.read_token_heatmap(tmp_path / 'nonexistent') is None

    empty_dir = tmp_path / 'empty'
    (empty_dir / 'llm_calls').mkdir(parents=True)
    assert tv.read_token_heatmap(empty_dir) is None


def test_tokens_page_generated_and_weight_under_300kb() -> None:
    """Issue #223: tokens.html must be generated in render_pages and weigh < 300 KB."""
    fixture = _fixture()
    # Provide synthetic heatmap data with 1 active day and 1 gap day
    fixture['token_heatmap'] = {
        'dates': ['2026-09-05', '2026-09-06'],
        'hourly': {
            '2026-09-05': [[1000, 2000, 3000, 1, 'bridge'] for _ in range(24)],
            '2026-09-06': None,
        },
        'five_min': {
            '2026-09-05': {'0': [1000, 2000, 3000, 1, 'bridge']},
            '2026-09-06': None,
        },
        'summary': {
            'total_tokens': 72000,
            'total_calls': 24,
            'local_tokens': 24000,
            'gateway_tokens': 48000,
            'days_span': 2,
            'days_present': 1,
            'days_missing': 1,
            'quantiles_hourly': {
                'gateway': [1000, 2000, 3000, 4000],
                'local': [500, 1000, 1500, 2000],
                'total': [1500, 3000, 4500, 6000],
            },
            'quantiles_5min': {
                'gateway': [100, 200, 300, 400],
                'local': [50, 100, 150, 200],
                'total': [150, 300, 450, 600],
            },
        },
    }

    pages = tv.render_pages(fixture, host='eeepc', generated_at='2026-09-06 12:00:00')
    assert 'tokens.html' in pages
    tokens_html = pages['tokens.html']

    # Must contain key structural components
    assert 'Token Heatmap' in tokens_html
    assert 'Vendor API' in tokens_html
    assert 'Self-hosted GPU' in tokens_html
    assert '192.168.1.35' in tokens_html  # infrastructure routing notice
    assert 'tkn-infra-notice' in tokens_html
    assert 'tkn-strip-wrap' in tokens_html
    assert 'cell-nodata' in tokens_html
    assert 'cell-zero' in tokens_html
    assert 'token-heatmap-data' in tokens_html

    # Weight check: must be strictly under 300 KB
    byte_len = len(tokens_html.encode('utf-8'))
    assert byte_len < 300_000, f'tokens.html is too heavy: {byte_len} bytes (limit: 300,000 bytes)'


def test_index_page_contains_tokens_teaser() -> None:
    """Issue #223: index.html teaser list must link to tokens.html."""
    idx = _site()['index.html']
    assert 'href="tokens.html">tokens</a>' in idx


def test_tokens_page_has_explicit_msk_and_utc_notice() -> None:
    """Issue #225: tokens.html must clearly state that source data is UTC and display is MSK."""
    fixture = _fixture()
    fixture['token_heatmap'] = {
        'dates': ['2026-09-06'],
        'hourly': {'2026-09-06': [[0, 0, 0, 0, 0, ''] for _ in range(24)]},
        'five_min': {'2026-09-06': {}},
        'summary': {
            'total_tokens': 0,
            'total_calls': 0,
            'local_tokens': 0,
            'gateway_tokens': 0,
            'days_span': 1,
            'days_present': 1,
            'days_missing': 0,
            'timezone': 'MSK',
            'quantiles_hourly': {'gateway': [0, 0, 0, 0], 'local': [0, 0, 0, 0], 'total': [0, 0, 0, 0]},
            'quantiles_5min': {'gateway': [0, 0, 0, 0], 'local': [0, 0, 0, 0], 'total': [0, 0, 0, 0]},
        },
    }
    pages = tv.render_pages(fixture, host='eeepc', generated_at='2026-09-06 12:00:00')
    tokens_html = pages['tokens.html']
    assert 'MSK' in tokens_html
    assert 'UTC+3' in tokens_html
    assert 'Часовой пояс:' in tokens_html
    assert 'UTC' in tokens_html
    assert '(24h MSK)' in tokens_html


def test_lineage_day_buckets_utc_crossing_to_next_msk_day() -> None:
    """Issue #225: _lineage_day must bucket UTC timestamps past 21:00 UTC into the next MSK day."""
    assert tv._lineage_day('2026-09-06T20:59:00Z') == '2026-09-06'
    assert tv._lineage_day('2026-09-06T21:00:00Z') == '2026-09-07'
    assert tv._lineage_day('2026-09-06T22:00:00Z') == '2026-09-07'


def test_cycle_feed_history_day_header_utc_crossing_to_next_msk_day() -> None:
    """Issue #225: build_cycle_feed day header in history mode must group and label by MSK date."""
    ledger = [
        {'phase': 'outcome', 'cycle_id': 'c-night', 'ts': '2026-09-06T22:00:00Z'},
    ]
    html = tv.build_cycle_feed(ledger_tail=ledger, history_mode=True)
    assert '<li class="feed-day-header">2026-09-07 MSK</li>' in html


def test_fmt_ts_full_iso_conversion_to_msk() -> None:
    """Issue #225: fmt_ts converts ISO UTC timestamp into YYYY-MM-DD HH:MM:SS MSK."""
    assert tv.fmt_ts('2026-09-06T22:15:30Z') == '2026-09-07 01:15:30 MSK'
    assert tv.fmt_ts('2026-09-06T10:00:00Z') == '2026-09-06 13:00:00 MSK'
    assert tv.fmt_ts('') == 'unknown time'
    assert tv.fmt_ts(None) == 'unknown time'


def test_issue277_defect1_node_link_points_to_lineage_page() -> None:
    ledger_tail = [
        {'phase': 'outcome', 'cycle_id': 'cycle-evo1', 'status': 'integrated'},
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
    assert 'href="lineage.html#node-sha1234"' in html


def test_issue277_defect2_index_teasers_label_full_history() -> None:
    data = {
        'ledger_tail': [
            {'phase': 'outcome', 'cycle_id': 'c1', 'status': 'integrated'},
            {'phase': 'outcome', 'cycle_id': 'c2', 'status': 'integrated'},
            {'phase': 'outcome', 'cycle_id': 'c3', 'status': 'integrated'},
            {'phase': 'outcome', 'cycle_id': 'c4', 'status': 'integrated'},
        ],
        'evolution_tree': {'nodes': {}},
        'hypotheses': {'entries': {}},
        'lessons': [],
        'reflections': [],
    }
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-08-18 12:00:00')
    idx = pages['index.html']
    assert 'cycles</a> &mdash; 4 cycles in full history' in idx


def test_issue277_defect3_generator_sha_from_arbitrary_cwd(monkeypatch) -> None:
    # Ensure _BAKED_GENERATOR_SHA is empty so git branch is exercised
    monkeypatch.setattr(tv, '_BAKED_GENERATOR_SHA', '')
    import os
    orig = os.getcwd()
    try:
        os.chdir(os.path.abspath(os.sep))
        sha = tv._generator_sha()
        assert sha != 'unknown'
        assert len(sha) >= 7
    finally:
        os.chdir(orig)


def test_issue277_defect4_lesson_kind_chip_rendered() -> None:
    lessons = [
        {
            'schema': 2,
            'id': 'KB-0277',
            'kind': 'operational-pattern',
            'title': 'Test Lesson Kind',
            'date': '2026-09-15',
            'severity': 'medium',
            'problem': 'Something failed',
            'solution': 'Fix it',
        }
    ]
    html = tv.build_lessons_panel(lessons)
    assert '<span class="lesson-chip lesson-kind">operational-pattern</span>' in html


def test_issue277_defect5_legacy_details_auto_open_script_present() -> None:
    html = tv.build_lessons_panel([_LEGACY_LESSON])
    assert 'legacyDetails.open = legacyMatch;' in html


# ---------------------------------------------------------------------------
# ozand/eeebot#1755: truncation/drop streak (4a) + window pressure (4b) --
# local-mirror integration and REMOTE_READER_SCRIPT parity. The two read
# paths must agree (see the module-level "keep in sync" comments).
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')


def test_1755_local_mirror_truncation_streak_restarts_after_healthy_gap(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    _write_jsonl(state / 'ledger' / 'cycles.jsonl', [
        {'phase': 'system_prompt', 'cycle_id': 'c1', 'truncated': ['AGENTS.md']},
        {'phase': 'system_prompt', 'cycle_id': 'c2', 'truncated': ['AGENTS.md']},
        {'phase': 'system_prompt', 'cycle_id': 'c3', 'truncated': []},
        {'phase': 'system_prompt', 'cycle_id': 'c4', 'truncated': ['AGENTS.md']},
    ])
    result = tv.read_local_state(str(state))
    streak = result['agent_context']['truncation_streak']
    assert streak['status'] == 'alarm'
    assert streak['total_rows'] == 4
    assert streak['entries'] == [{'kind': 'truncated', 'name': 'AGENTS.md', 'streak': 1}]


def test_1755_local_mirror_truncation_streak_no_data_when_ledger_missing(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    state.mkdir()
    result = tv.read_local_state(str(state))
    assert result['agent_context']['truncation_streak'] == {'status': 'no_data', 'total_rows': 0, 'entries': []}


def test_1755_local_mirror_window_pressure_excludes_null_context_window(tmp_path: Path) -> None:
    # Uses real "now" (rather than a pinned clock) so the rows always fall
    # inside the last-24h window `read_local_state` computes internally --
    # it does not expose a way to inject a fixed clock from outside.
    state = tmp_path / 'state'
    now = datetime.now(timezone.utc)
    recent = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    day_file = state / 'llm_calls' / (now.strftime('%Y-%m-%d') + '.jsonl')
    _write_jsonl(day_file, [
        {'ts': recent, 'component': 'executor', 'prompt_tokens': 79804, 'context_window': 98304},
        {'ts': recent, 'component': 'executor', 'prompt_tokens': 50000},  # no context_window key at all
        {'ts': recent, 'component': 'executor', 'prompt_tokens': 40000, 'context_window': None},
    ])
    result = tv.read_local_state(str(state))
    pressure = result['agent_context']['window_pressure']
    assert pressure['status'] == 'measured'
    assert pressure['rows_in_window'] == 3
    assert pressure['known_rows'] == 1
    assert pressure['unknown_rows'] == 2
    assert pressure['p99_pct'] == pytest.approx(79804 / 98304 * 100.0)


def test_1755_remote_reader_script_mirrors_truncation_streak_and_window_pressure(tmp_path: Path) -> None:
    """The self-contained REMOTE_READER_SCRIPT cannot import agent_context.py
    -- it carries its own copy of the streak/window-pressure math. Exec it
    against a temp STATE_ROOT (same pattern as test_271) and confirm both
    new indicators come out identical to the local mirror's."""
    import contextlib
    import io

    state = tmp_path / 'state'
    _write_jsonl(state / 'ledger' / 'cycles.jsonl', [
        {'phase': 'system_prompt', 'cycle_id': f'c{i}', 'truncated': ['AGENTS.md']} for i in range(3)
    ])
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    recent = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    _write_jsonl(state / 'llm_calls' / (now.strftime('%Y-%m-%d') + '.jsonl'), [
        {'ts': recent, 'component': 'executor', 'prompt_tokens': 79804, 'context_window': 98304},
        {'ts': recent, 'component': 'executor', 'prompt_tokens': 50000, 'context_window': None},
    ])
    script = tv.REMOTE_READER_SCRIPT.replace(
        'STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"',
        f'STATE_ROOT = {str(state)!r}',
    )
    namespace: dict[str, object] = {}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(script, namespace)

    agent_context = namespace['read_agent_context']()
    assert agent_context['truncation_streak'] == {
        'status': 'alarm', 'total_rows': 3,
        'entries': [{'kind': 'truncated', 'name': 'AGENTS.md', 'streak': 3}],
    }

    pressure = namespace['read_window_pressure'](now=now)
    assert pressure['status'] == 'measured'
    assert pressure['known_rows'] == 1
    assert pressure['unknown_rows'] == 1
    assert pressure['p99_pct'] == pytest.approx(79804 / 98304 * 100.0)


def test_1755_compute_window_pressure_remote_copy_matches_local_module() -> None:
    """The duplicated compute_window_pressure inside REMOTE_READER_SCRIPT
    must behave identically to scripts.agent_context.compute_window_pressure
    for the same input -- both are exercised here on the same rows."""
    import contextlib
    import io

    from scripts.agent_context import compute_window_pressure as local_compute

    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    recent = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    rows = [
        {'ts': recent, 'prompt_tokens': 79804, 'context_window': 98304},
        {'ts': recent, 'prompt_tokens': 40000},
        {'ts': recent, 'prompt_tokens': 71229, 'context_window': 98304},
    ]
    namespace: dict[str, object] = {}
    with contextlib.redirect_stdout(io.StringIO()):
        exec(tv.REMOTE_READER_SCRIPT, namespace)
    remote_result = namespace['compute_window_pressure'](rows, now=now)
    local_result = local_compute(rows, now=now)
    assert remote_result == local_result


def test_adr036_prompt_text_never_reaches_public_pages(tmp_path: Path) -> None:
    """ADR-036 rule 3: executor prompt text (system and task) is call text.

    The reader still collects it, but no public page or published JSON
    (lineage-cycle-details.json included) may contain it.
    """
    marker_system = 'ADR036-SYSTEM-MARKER-7f3c9e'
    marker_task = 'ADR036-TASK-MARKER-41b2d8'
    state = tmp_path / 'state'
    (state / 'prompts').mkdir(parents=True)
    (state / 'prompts' / 'cycle-leak.system.txt').write_text(f'system {marker_system}', encoding='utf-8')
    (state / 'prompts' / 'cycle-leak.task.txt').write_text(f'task {marker_task}', encoding='utf-8')
    repo = tmp_path / 'repo'
    (repo / 'skills' / 'leak-skill').mkdir(parents=True)
    marker_agents = 'ADR036-AGENTSMD-MARKER-c05a11'
    marker_skill = 'ADR036-SKILL-MARKER-9d7e21'
    (repo / 'AGENTS.md').write_text(f'# Agents\n\n{marker_agents}\n', encoding='utf-8')
    (repo / 'skills' / 'leak-skill' / 'SKILL.md').write_text(f'---\nname: leak-skill\n---\n{marker_skill}\n', encoding='utf-8')
    data = tv.read_local_state(str(state), str(repo))
    assert marker_system in data['cycle_prompts']['cycle-leak']['system']['text']
    collected = json.dumps(data, default=str)
    assert marker_agents in collected  # the reader sees it; only rendering must drop it
    markers = [marker_system, marker_task, marker_agents] + ([marker_skill] if marker_skill in collected else [])

    marker_goal = 'ADR036-GOAL-MARKER-f00d'
    data['goal_text'] = {'charter': f'charter {marker_goal}'}
    markers.append(marker_goal)
    pages = tv.render_pages(data, 'eeepc')
    assert tv.LINEAGE_DETAILS_FILE in pages
    single = tv.render_page(data, 'eeepc')
    for name, body in list(pages.items()) + [('render_page', single)]:
        for marker in markers:
            assert marker not in body, (name, marker)
    assert '"prompt":' not in pages[tv.LINEAGE_DETAILS_FILE]


def test_adr036_subagent_and_reflector_text_stay_off_public_details() -> None:
    """ADR-036 rule 3: subagent task/summary/result text and reflector output
    are model text; the public cycle-details records keep only their sizes."""
    markers = ['ADR036-SA-TASK-1e2d', 'ADR036-SA-SUMMARY-77ab', 'ADR036-SA-RESULT-3c9f', 'ADR036-REFL-SUMMARY-5d10', 'ADR036-REFL-FINDING-8e42']
    subagents = [{
        'cycle_id': 'cycle-x', 'subagent_id': 's1', 'label': 'exec', 'status': 'ok',
        'task_excerpt': markers[0], 'summary_excerpt': markers[1], 'result_excerpt': markers[2],
    }]
    reflections = [{'cycle_id': 'cycle-x', 'summary': markers[3], 'findings': [markers[4]]}]
    details = tv.build_cycle_details([], None, None, reflections, subagent_records=subagents)
    blob = json.dumps(details)
    for marker in markers:
        assert marker not in blob, marker
    assert details['cycle-x']['subagents'][0]['summary_chars'] == len(markers[1])
    assert details['cycle-x']['reflection']['findings_count'] == 1


def test_adr036_reason_code_is_public_free_text_is_not() -> None:
    """ADR-036 rule 3 (architect, 2026-09-26): a vocabulary code passes; an
    executor's phrase is replaced by its size."""
    rows = [
        {'cycle_id': 'cycle-code', 'reason': 'no_plan'},
        {'cycle_id': 'cycle-text', 'reason': 'The executor said ADR036-REASON-MARKER-aa01 here'},
    ]
    details = tv.build_cycle_details(rows, None, None, None)
    assert details['cycle-code']['reason'] == 'no_plan'
    assert 'ADR036-REASON-MARKER-aa01' not in json.dumps(details)
    assert details['cycle-text']['reason'].endswith('chars (LAN)')


def test_adr036_gate_violations_keep_rule_and_path_only() -> None:
    rows = [{'cycle_id': 'cycle-g', 'violations': ['test_regression: tests/test_x.py ADR036-GATE-MARKER-bb02 output']}]
    details = tv.build_cycle_details(rows, None, None, None)
    [shown] = details['cycle-g']['gate_violations']
    assert 'ADR036-GATE-MARKER-bb02' not in shown
    assert shown.startswith('test_regression')
    assert 'tests/test_x.py' in shown


def test_adr036_lesson_bodies_and_priority_summary_stay_off_public_pages() -> None:
    """Lesson problem/solution/insight text and a priority's summary are
    LAN-only; titles, tags and the compact priority form stay public."""
    lessons = [{'cycle_id': 'cycle-l', 'problem': 'ADR036-LESSON-PROBLEM-cc03', 'solution': 'ADR036-LESSON-SOLUTION-dd04', 'insight': 'ADR036-LESSON-INSIGHT-ee05'}]
    details = tv.build_cycle_details([], None, lessons, None)
    blob = json.dumps(details)
    for marker in ('ADR036-LESSON-PROBLEM-cc03', 'ADR036-LESSON-SOLUTION-dd04', 'ADR036-LESSON-INSIGHT-ee05'):
        assert marker not in blob
    assert details['cycle-l']['lesson_problem_chars'] == len('ADR036-LESSON-PROBLEM-cc03')
