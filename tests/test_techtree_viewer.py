from __future__ import annotations

from scripts import techtree_viewer as tv


def _fixture() -> dict[str, object]:
    return {
        'portfolio': {
            'current': 'proposer-quality',
            'nodes': {
                'proposer-quality': {
                    'lever_metric': 'loop.repeat_failure_rate',
                    'gain_history': [0.02, -0.01, 0.03],
                    'status': 'active',
                    'minted_by': 'hypothesis',
                    'created_ts': '2026-08-10T00:00:00Z',
                    'cooldown_until_ts': None,
                },
                'cycle-cost': {
                    'lever_metric': 'cost.tokens_per_integration',
                    'gain_history': [],
                    'status': 'plateaued',
                    'minted_by': 'product',
                    'created_ts': '2026-08-01T00:00:00Z',
                    'cooldown_until_ts': '2026-08-19T00:00:00Z',
                },
            },
            'switches': [
                {'ts': '2026-08-18T04:04:22Z', 'from': 'cycle-cost', 'to': 'proposer-quality', 'reason': 'plateau_switch'},
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
                    'ts': '2026-08-15T00:00:00Z',
                },
                'child1sha0000000000000000000000000000000': {
                    'parent_sha': 'rootsha00000000000000000000000000000000',
                    'branch': 'selfevo/cycle-a',
                    'ts': '2026-08-16T00:00:00Z',
                },
                'child2sha0000000000000000000000000000000': {
                    'parent_sha': 'rootsha00000000000000000000000000000000',
                    # No embedded "/" in the malicious part -- the branch
                    # tail extraction (rsplit on "/") must not eat it, so
                    # this exercises html.escape on the actual tag text.
                    'branch': 'selfevo/cycle-b<script>bad',
                    'ts': '2026-08-17T00:00:00Z',
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
        'hypotheses': {'entries': {}},
        'ledger_tail': [],
    }


def test_render_page_includes_node_cards_and_panels() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')

    assert 'Proposer Quality' in html_out
    assert 'Cycle Cost' in html_out
    assert 'RESEARCHING' in html_out
    assert 'PLATEAUED' in html_out
    assert 'MINTED BY HYPOTHESIS' in html_out
    assert 'Great Library' in html_out
    assert 'World History' in html_out
    assert 'EEEBOT EMPIRE' in html_out
    assert 'http://' not in html_out
    assert 'https://' not in html_out


def test_world_history_renders_branching_svg_tree() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')

    # A real inline SVG tree, not a flat card/list timeline.
    assert '<svg' in html_out
    # Both fork children (from the same parent) must have visible labels.
    assert 'cycle-a-child1s' in html_out
    # The second child's branch name is untrusted (LLM-authored) and
    # deliberately carries a script-injection attempt -- must be escaped,
    # never executed, and the sha7+tail label must still render.
    assert '<script>bad' not in html_out
    assert '&lt;script&gt;' in html_out
    assert 'child2s' in html_out
    # current-sha highlight + switch marker glyphs.
    assert '&#9672;' in html_out or 'evo-node-current' in html_out
    assert '&#8634;' in html_out


def test_research_switch_path_is_numbered() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')

    # The switches list (cycle-cost -> proposer-quality) must be traced as
    # a numbered path across cards, not just prose in the chronicle.
    assert '①' in html_out
    assert 'civ-switch-edge' in html_out


def test_render_page_fails_soft_on_missing_sources() -> None:
    empty = {'portfolio': None, 'scorecard': None, 'evolution_tree': None, 'hypotheses': None, 'ledger_tail': None}
    html_out = tv.render_page(empty, host='eeepc', generated_at='2026-08-18 12:00:00')

    assert 'unavailable' in html_out.lower()
    assert '<html' in html_out


def test_world_history_falls_back_to_list_below_two_nodes() -> None:
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

    assert '<svg' not in html_out
    assert 'timeline-list' in html_out
