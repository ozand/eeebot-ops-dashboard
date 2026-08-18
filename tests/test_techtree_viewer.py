from __future__ import annotations

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
    assert 'Never Visited Direction' in html_out
    assert 'RESEARCHING' in html_out
    assert 'PLATEAUED' in html_out
    assert 'AVAILABLE' in html_out
    assert 'MINTED' in html_out
    assert 'Great Library' in html_out
    assert 'EEEBOT EMPIRE' in html_out
    assert 'http://' not in html_out
    assert 'https://' not in html_out


def test_canvas_is_one_wide_svg_with_lane_labels() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')

    assert 'class="tech-canvas"' in html_out
    assert 'RESEARCH</text>' in html_out
    assert 'WORLD HISTORY</text>' in html_out

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

    # Hypothesis-minted direction gets a dashed gold Great Library edge.
    assert 'class="mint-elbow"' in html_out
    assert '&#127979;' in html_out


def test_world_history_renders_branching_boxes_with_elbows() -> None:
    html_out = tv.render_page(_fixture(), host='eeepc', generated_at='2026-08-18 12:00:00')

    # Real branching boxes (not bare circles), joined by orthogonal elbows.
    assert 'class="evo-box' in html_out
    assert 'class="evo-elbow"' in html_out
    # Both fork children (from the same parent) must have visible labels.
    assert 'cycle-a-child1s' in html_out
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
