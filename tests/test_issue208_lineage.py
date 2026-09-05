"""#208: the lineage page draws a tree, labels it, keeps edge provenance, and fits the budget.

Two halves. The renderer half executes ``assets/vendor/lineage-renderer.js`` under
Node through ``tests/lineage_renderer_harness.js`` and asserts on the rendered
geometry and classes (the #126 layout gates that were listed and never built).
The generator half asserts on the per-day payload and the page assembly.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import techtree_viewer as tv  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / 'tests' / 'lineage_renderer_harness.js'
NODE = shutil.which('node')

# ─── renderer half: execute the JavaScript ─────────────────────────────────────


def _render(payload: dict, tmp_path: Path, filter_probe: list[dict] | None = None) -> dict:
    if not NODE:
        # Review on PR #209: a silent skip reads as a pass under `pytest -q`, and
        # there is no CI to run this elsewhere. Fail loudly; skipping must be a
        # deliberate, visible choice.
        if os.environ.get('LINEAGE_SKIP_NODE_TESTS') == '1':
            pytest.skip('LINEAGE_SKIP_NODE_TESTS=1: the #208 geometry tests were deliberately not executed')
        pytest.fail('node is not installed, so the #208 renderer geometry tests did NOT run; '
                    'install Node.js or set LINEAGE_SKIP_NODE_TESTS=1 to skip them explicitly')
    payload_file = tmp_path / 'payload.json'
    payload_file.write_text(json.dumps(payload), encoding='utf-8')
    args = [NODE, str(HARNESS), str(payload_file)]
    if filter_probe is not None:
        probe_file = tmp_path / 'probe.json'
        probe_file.write_text(json.dumps(filter_probe), encoding='utf-8')
        args.append(str(probe_file))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def _elements(svg: dict, tag: str) -> list[dict]:
    return [child for child in svg['children'] if child['tag'] == tag]


def _circles(result: dict) -> dict[str, dict]:
    return {c['attrs']['data-cycle-id']: c for c in _elements(result['svg'], 'circle')}


def _viewbox(result: dict) -> tuple[float, float, float, float]:
    parts = [float(v) for v in result['svg']['attrs']['viewBox'].split()]
    return parts[0], parts[1], parts[2], parts[3]


def _path_points(d: str) -> list[tuple[float, float]]:
    numbers = [float(v) for v in re.findall(r'-?\d+(?:\.\d+)?', d)]
    return list(zip(numbers[0::2], numbers[1::2]))


def _segments(points: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return list(zip(points, points[1:]))


def _proper_crossing(a, b) -> bool:
    """Two segments cross if they intersect at a point interior to both (touching at an endpoint is not a crossing)."""
    (x1, y1), (x2, y2) = a
    (x3, y3), (x4, y4) = b
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-9:
        return False
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / den
    eps = 1e-6
    return eps < t < 1 - eps and eps < u < 1 - eps


def _edge_crossings(result: dict) -> int:
    polylines = [_segments(_path_points(p['attrs']['d'])) for p in _elements(result['svg'], 'path')]
    crossings = 0
    for i in range(len(polylines)):
        for j in range(i + 1, len(polylines)):
            if any(_proper_crossing(a, b) for a in polylines[i] for b in polylines[j]):
                crossings += 1
    return crossings


def _node(sha: str, parent: str | None, ts: str, *, basis: str | None = None, kind: str = 'trunk',
          outcome: str = 'integrated', title: str | None = None, **extra) -> dict:
    node = {'sha': sha, 'cycle_id': 'cycle-' + sha, 'parent': parent, 'ts': ts, 'outcome': outcome, 'kind': kind}
    if parent is not None:
        node['parent_basis'] = basis or 'recorded'
    if title is not None:
        node['title'] = title
    node.update(extra)
    return node


def _payload(nodes: list[dict], current_sha: str = '') -> dict:
    edges = [{'source': n['parent'], 'target': n['sha'], 'basis': n.get('parent_basis') or 'recorded'} for n in nodes if n.get('parent')]
    return {'day': '2026-09-01', 'current_sha': current_sha, 'nodes': nodes, 'edges': edges}


def test_linear_chain_renders_as_one_column(tmp_path: Path) -> None:
    nodes = [_node('a', None, '2026-09-01T00:00:00Z'), _node('b', 'a', '2026-09-01T01:00:00Z'),
             _node('c', 'b', '2026-09-01T02:00:00Z'), _node('d', 'c', '2026-09-01T03:00:00Z')]
    result = _render(_payload(nodes), tmp_path)
    assert result['successes'] == 1, result['error']
    circles = _circles(result)
    xs = {float(circles[f'cycle-{s}']['attrs']['cx']) for s in 'abcd'}
    ys = [float(circles[f'cycle-{s}']['attrs']['cy']) for s in 'abcd']
    assert len(xs) == 1, f'a chain must be a single column, got x = {sorted(xs)}'
    assert ys == sorted(ys) and len(set(ys)) == 4


def test_fork_children_share_depth_have_distinct_x_and_edges_do_not_cross(tmp_path: Path) -> None:
    nodes = [
        _node('root', None, '2026-09-01T00:00:00Z'),
        _node('c1', 'root', '2026-09-01T01:00:00Z'), _node('c2', 'root', '2026-09-01T02:00:00Z'), _node('c3', 'root', '2026-09-01T03:00:00Z'),
        _node('g1', 'c1', '2026-09-01T04:00:00Z'), _node('g2', 'c1', '2026-09-01T05:00:00Z'), _node('g3', 'c3', '2026-09-01T06:00:00Z'),
    ]
    result = _render(_payload(nodes), tmp_path)
    assert result['successes'] == 1, result['error']
    circles = _circles(result)
    children = [circles[f'cycle-{s}'] for s in ('c1', 'c2', 'c3')]
    assert len({c['attrs']['cy'] for c in children}) == 1, 'siblings must sit at the same depth'
    assert len({c['attrs']['cx'] for c in children}) == 3, 'siblings must have distinct x'
    grandchildren = [circles[f'cycle-{s}'] for s in ('g1', 'g2', 'g3')]
    assert len({c['attrs']['cy'] for c in grandchildren}) == 1
    assert float(grandchildren[0]['attrs']['cy']) > float(children[0]['attrs']['cy']) > float(circles['cycle-root']['attrs']['cy'])
    assert len(_elements(result['svg'], 'path')) == 6
    assert _edge_crossings(result) == 0


def test_every_coordinate_lies_inside_the_viewbox(tmp_path: Path) -> None:
    nodes = [_node('t0', None, '2026-09-01T00:00:00Z', parent_day='2026-08-31', current=True), _node('t1', 't0', '2026-09-01T00:10:00Z')]
    nodes += [_node(f'l{i}', 't1', f'2026-09-01T01:{i:02d}:00Z', basis='inferred', kind='leaf', outcome='failed') for i in range(20)]
    result = _render(_payload(nodes, current_sha='t0'), tmp_path)
    assert result['successes'] == 1, result['error']
    x0, y0, w, h = _viewbox(result)
    assert float(result['svg']['attrs']['width']) == w and float(result['svg']['attrs']['height']) == h
    for circle in _elements(result['svg'], 'circle'):
        cx, cy, r = (float(circle['attrs'][k]) for k in ('cx', 'cy', 'r'))
        assert x0 <= cx - r and cx + r <= x0 + w and y0 <= cy - r and cy + r <= y0 + h, circle['attrs']
    for path in _elements(result['svg'], 'path'):
        for x, y in _path_points(path['attrs']['d']):
            assert x0 <= x <= x0 + w and y0 <= y <= y0 + h, path['attrs']['d']
    for text in _elements(result['svg'], 'text'):
        assert x0 <= float(text['attrs']['x']) <= x0 + w and y0 <= float(text['attrs']['y']) <= y0 + h, text['attrs']
    assert len(_elements(result['svg'], 'text')) == 2, 'the stub and the star are part of the bounds check'
    assert len(_elements(result['svg'], 'circle')) == 22, 'a wide fan-out is drawn, not dropped'


def test_inferred_edges_never_get_the_recorded_treatment(tmp_path: Path) -> None:
    nodes = [_node('t0', None, '2026-09-01T00:00:00Z'), _node('t1', 't0', '2026-09-01T01:00:00Z', basis='recorded'),
             _node('leaf', 't1', '2026-09-01T02:00:00Z', basis='inferred', kind='leaf', outcome='failed'),
             _node('t2', 't1', '2026-09-01T03:00:00Z', basis='inferred')]
    result = _render(_payload(nodes), tmp_path)
    assert result['successes'] == 1, result['error']
    paths = _elements(result['svg'], 'path')
    assert len(paths) == 3
    recorded = [p for p in paths if p['attrs'].get('data-basis') == 'recorded']
    inferred = [p for p in paths if p['attrs'].get('data-basis') == 'inferred']
    assert len(recorded) == 1 and len(inferred) == 2, [p['attrs'] for p in paths]
    for p in inferred:
        assert 'lineage-edge-chronological' in p['attrs']['class'].split()
        assert 'arch-edge' not in p['attrs']['class'].split()
        assert p['attrs'].get('stroke-dasharray')
    for p in recorded:
        assert 'lineage-edge-chronological' not in p['attrs']['class'].split()
        assert 'stroke-dasharray' not in p['attrs']


def test_node_title_comes_from_the_payload_not_a_placeholder(tmp_path: Path) -> None:
    nodes = [_node('a', None, '2026-09-01T00:00:00Z', title='Add harness detour warning check'),
             _node('b', 'a', '2026-09-01T01:00:00Z')]
    result = _render(_payload(nodes), tmp_path)
    assert result['successes'] == 1, result['error']
    circles = _circles(result)
    titles = {cid: c['children'][0]['text'] for cid, c in circles.items() if c['children']}
    assert titles['cycle-a'] == 'Add harness detour warning check'
    assert titles['cycle-b'] == 'cycle-b', 'a node without a title shows its cycle id, not "(untitled cycle)"'
    assert '(untitled cycle)' not in json.dumps(result['svg'])


def test_cross_day_parent_renders_a_stub_and_leaves_no_isolated_node(tmp_path: Path) -> None:
    nodes = [_node('first', None, '2026-09-01T00:00:00Z', parent_day='2026-08-31'),
             _node('second', 'first', '2026-09-01T01:00:00Z'),
             # a parent pointer at a node that is not in the payload (truncated away): drawn as a root, not dropped
             _node('orphan', 'truncated-away', '2026-09-01T02:00:00Z'),
             # a two-node parent cycle in a corrupt payload: both nodes must still be drawn
             _node('cyc-a', 'cyc-b', '2026-09-01T03:00:00Z'), _node('cyc-b', 'cyc-a', '2026-09-01T04:00:00Z')]
    result = _render(_payload(nodes), tmp_path)
    assert result['successes'] == 1, result['error']
    assert len(_circles(result)) == 5, 'every node is drawn, whatever its parent pointer says'
    stubs = [t for t in _elements(result['svg'], 'text') if 'lineage-hidden-parent' in t['attrs'].get('class', '').split()]
    assert len(stubs) == 1 and 'Aug 31' in stubs[0]['text']
    assert float(stubs[0]['attrs']['y']) == float(_circles(result)['cycle-first']['attrs']['cy']) - 14
    linked = {p['attrs']['data-target'] for p in _elements(result['svg'], 'path')}
    stubbed = {t['attrs']['data-target'] for t in stubs}
    isolated = sorted(n['sha'] for n in nodes if n['sha'] not in linked and n['sha'] not in stubbed)
    # the orphan is an honest root (no edge invented); the cycle's two recorded pointers are both drawn as given
    assert isolated == ['orphan'], isolated
    assert {(p['attrs']['data-source'], p['attrs']['data-target']) for p in _elements(result['svg'], 'path')} >= {('cyc-b', 'cyc-a'), ('cyc-a', 'cyc-b')}


def test_current_node_star_survives_the_client_render(tmp_path: Path) -> None:
    nodes = [_node('a', None, '2026-09-01T00:00:00Z'), _node('b', 'a', '2026-09-01T01:00:00Z', current=True)]
    result = _render(_payload(nodes, current_sha='b'), tmp_path)
    assert result['successes'] == 1, result['error']
    stars = [t for t in _elements(result['svg'], 'text') if 'arch-star' in t['attrs'].get('class', '').split()]
    assert len(stars) == 1
    assert float(stars[0]['attrs']['x']) == float(_circles(result)['cycle-b']['attrs']['cx'])


def test_day_filter_is_calendar_based_and_says_when_today_has_no_data(tmp_path: Path) -> None:
    days = ['2026-09-02', '2026-09-03', '2026-09-04', '2026-09-05']
    probes = [
        {'mode': '24h', 'days': days, 'now': '2026-09-05T10:00:00Z'},
        {'mode': '24h', 'days': days, 'now': '2026-09-07T01:00:00Z'},
        {'mode': 'today', 'days': days, 'now': '2026-09-05T10:00:00Z'},
        {'mode': 'today', 'days': days, 'now': '2026-09-06T10:00:00Z'},
        {'mode': 'yesterday-today', 'days': days, 'now': '2026-09-06T10:00:00Z'},
    ]
    result = _render(_payload([_node('a', None, '2026-09-01T00:00:00Z')]), tmp_path, filter_probe=probes)
    assert result['filter'] is not None, 'the renderer must expose lineageDayFilter.select'
    last24_now, last24_stale, today, today_stale, yt_stale = result['filter']
    assert last24_now['keep'] == ['2026-09-04', '2026-09-05']
    assert last24_stale['keep'] == [], last24_stale  # 01:00 on the 7th: no day overlaps the last 24 hours
    assert '2026-09-05' in last24_stale['note']
    assert today['keep'] == ['2026-09-05'] and not today['note']
    assert today_stale['keep'] == []
    assert '2026-09-06' in today_stale['note'] and '2026-09-05' in today_stale['note']
    assert yt_stale['keep'] == ['2026-09-05'] and '2026-09-06' in yt_stale['note']


# ─── generator half ───────────────────────────────────────────────────────────

ROWS = [
    {'phase': 'evolution_tree', 'cycle_id': 'cycle-r', 'sha': 'r', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
    {'phase': 'evolution_tree', 'cycle_id': 'cycle-a', 'sha': 'a', 'parent_sha': 'r', 'ts': '2026-09-01T01:00:00Z'},
    {'phase': 'evolution_tree', 'cycle_id': 'cycle-b', 'sha': 'b', 'parent_sha': 'a', 'ts': '2026-09-01T02:00:00Z'},
    # a failed attempt after both a and b: the old code hung it off r (first of day) in nodes[] and off b (latest) in edges[]
    {'phase': 'outcome', 'cycle_id': 'cycle-fail', 'outcome': 'failed', 'ts': '2026-09-01T03:00:00Z'},
    # a trunk node whose recorded parent is unknown to the ledger: chronological guess, must be marked
    {'phase': 'evolution_tree', 'cycle_id': 'cycle-x', 'sha': 'x', 'parent_sha': 'not-in-ledger', 'ts': '2026-09-01T04:00:00Z'},
]
DETAILS = {
    'cycle-r': {'cycle_id': 'cycle-r', 'title': 'Root task'},
    'cycle-a': {'cycle_id': 'cycle-a', 'title': 'Alpha task', 'task_title': 'Alpha task'},
    'cycle-fail': {'cycle_id': 'cycle-fail', 'title': 'Failed attempt'},
}


def _day_payload(html: str, day: str = '2026-09-01') -> dict:
    match = re.search(r'<script type="application/json" class="lineage-day-data" data-day="' + day + r'"[^>]*>(.*?)</script>', html, re.S)
    assert match is not None
    return json.loads(match.group(1))


def test_payload_nodes_carry_the_title_from_cycle_details() -> None:
    html = tv.build_archive_tree({'nodes': {}}, ROWS, task_titles={'cycle-b': 'From git'}, cycle_details=DETAILS,
                                 ledger_history=ROWS, now='2026-09-01T05:00:00Z')
    nodes = {n['sha']: n for n in _day_payload(html)['nodes']}
    assert nodes['r']['title'] == 'Root task'
    assert nodes['a']['title'] == 'Alpha task'
    assert nodes['b']['title'] == 'From git', 'task_titles remains the fallback when details have no title'
    assert nodes['leaf:cycle-fail']['title'] == 'Failed attempt'
    assert 'title' not in nodes['x'], 'no known title: omit the key, the renderer shows the cycle id'
    assert '<title>Root task</title>' in html and '<title>(untitled cycle)</title>' not in html


def test_one_parent_expression_feeds_nodes_edges_and_server_svg() -> None:
    html = tv.build_archive_tree({'nodes': {}}, ROWS, ledger_history=ROWS, now='2026-09-01T05:00:00Z')
    payload = _day_payload(html)
    nodes = {n['sha']: n for n in payload['nodes']}
    by_target = {e['target']: e for e in payload['edges']}
    for sha, node in nodes.items():
        if node['parent'] is None:
            assert sha not in by_target
            assert node.get('parent_basis') is None
        else:
            assert by_target[sha]['source'] == node['parent'], f'{sha}: nodes[].parent and edges[] disagree'
            assert by_target[sha]['basis'] == node['parent_basis']
    assert nodes['a']['parent_basis'] == 'recorded' and nodes['b']['parent_basis'] == 'recorded'
    assert nodes['leaf:cycle-fail']['parent'] == 'b' and nodes['leaf:cycle-fail']['parent_basis'] == 'inferred'
    assert nodes['x']['parent'] == 'b' and nodes['x']['parent_basis'] == 'inferred'
    assert nodes['r']['parent'] is None
    # Two trunk rows with the same timestamp and no usable parent must not become each other's parent (review on PR #209).
    twins = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-p', 'sha': 'p', 'parent_sha': 'gone-1', 'ts': '2026-09-02T00:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-q', 'sha': 'q', 'parent_sha': 'gone-2', 'ts': '2026-09-02T00:00:00Z'},
    ]
    twin_nodes = {n['sha']: n for n in _day_payload(tv.build_archive_tree({'nodes': {}}, twins, ledger_history=twins, now='2026-09-02T05:00:00Z'), day='2026-09-02')['nodes']}
    parents = {twin_nodes['p']['parent'], twin_nodes['q']['parent']}
    assert None in parents and len(parents) == 2, twin_nodes
    # the server SVG (noscript fallback) uses the same basis: inferred edges are dashed, recorded are not
    svg = re.search(r'<svg class="lineage-day-svg[^>]*>(.*?)</svg>', html, re.S).group(1)
    lines = re.findall(r'<line [^>]*>', svg)
    inferred = [ln for ln in lines if 'lineage-edge-chronological' in ln]
    recorded = [ln for ln in lines if 'lineage-edge-chronological' not in ln]
    assert len(inferred) == 2 and all('stroke-dasharray' in ln for ln in inferred)
    assert len(recorded) == 2 and not any('stroke-dasharray' in ln for ln in recorded)


def test_cross_day_recorded_parent_is_a_stub_in_the_payload_not_a_guess() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-y', 'sha': 'y', 'parent_sha': '', 'ts': '2026-08-31T23:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-t', 'sha': 't', 'parent_sha': 'y', 'ts': '2026-09-01T01:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-u', 'sha': 'u', 'parent_sha': 't', 'ts': '2026-09-01T02:00:00Z'},
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T05:00:00Z')
    nodes = {n['sha']: n for n in _day_payload(html)['nodes']}
    assert nodes['t']['parent'] is None and nodes['t'].get('parent_basis') is None
    assert nodes['t']['parent_day'] == '2026-08-31'
    assert nodes['u']['parent'] == 't' and nodes['u']['parent_basis'] == 'recorded'
    section = re.search(r'<section class="lineage-day-group" data-day="2026-09-01">(.*?)</section>', html, re.S).group(1)
    svg = re.search(r'<svg class="lineage-day-svg[^>]*>(.*?)</svg>', section, re.S).group(1)
    stub = re.search(r'<text class="lineage-hidden-parent" x="(\d+)" y="(\d+)"[^>]*>&#8617; from Aug 31</text>', svg)
    node_t = re.search(r'data-cycle-id="cycle-t" cx="(\d+)" cy="(\d+)"', svg)
    assert stub and node_t
    assert stub.group(1) == node_t.group(1) and int(stub.group(2)) == int(node_t.group(2)) - 14, 'the noscript stub sits on its node'


def test_payload_marks_the_current_node() -> None:
    html = tv.build_archive_tree({'nodes': {}, 'current_sha': 'b'}, ROWS, ledger_history=ROWS, now='2026-09-01T05:00:00Z')
    payload = _day_payload(html)
    assert payload['current_sha'] == 'b'
    flagged = [n['sha'] for n in payload['nodes'] if n.get('current')]
    assert flagged == ['b']


def _pages() -> dict[str, str]:
    from test_techtree_viewer import _fixture  # pytest puts tests/ on sys.path (no __init__.py)
    data = _fixture()
    data['ledger_history'] = list(data['ledger_tail']) + ROWS
    return tv.render_pages(data, host='eeepc', generated_at='2026-09-01 05:00:00')


def test_cycle_details_ship_as_a_separate_json_file_without_losing_records() -> None:
    pages = _pages()
    html = pages['lineage.html']
    assert 'lineage-cycle-details.json' in pages, sorted(pages)
    details = json.loads(pages['lineage-cycle-details.json'])
    assert 'id="cycle-details-data"' not in html
    assert 'data-cycle-details-src="lineage-cycle-details.json"' in html
    # every cycle the ledger knows keeps its record, whether or not it has a node on the page
    rendered = set(re.findall(r'data-cycle-id="([^"]+)"', html))
    assert {'cycle-r', 'cycle-a', 'cycle-b', 'cycle-fail', 'cycle-x'} <= set(details)
    assert set(details) >= rendered - {''}
    assert any(cid not in rendered for cid in details), 'records without a node are kept, not filtered'
    assert all(rec.get('title') for rec in details.values())


def test_dead_archive_tree_branch_is_gone_and_the_fallback_tree_uses_the_day_lineage() -> None:
    assert not hasattr(tv, '_cycle_details_panel')
    tree = {'current_sha': 'k2', 'nodes': {
        'k1': {'parent_sha': None, 'cycle_id': 'cycle-k1', 'ts': '2026-09-01T00:00:00Z'},
        'k2': {'parent_sha': 'k1', 'cycle_id': 'cycle-k2', 'ts': '2026-09-01T01:00:00Z'},
    }}
    html = tv.build_archive_tree(tree, [], now='2026-09-01T05:00:00Z')
    assert 'lineage-day-data' in html, 'without evolution_tree ledger rows the tree.json nodes go through the same day lineage'
    assert 'class="tech-canvas arch-tree"' not in html and 'arch-legend' not in html
    src = Path(tv.__file__).read_text(encoding='utf-8')
    assert 'def _cycle_details_panel' not in src
    assert src.count('data-lineage-filter="24h"') == 1, 'one lineage implementation, one set of controls'


def test_d3_dag_is_gone_and_the_page_ships_only_the_renderer() -> None:
    vendor = REPO / 'assets' / 'vendor'
    assert not (vendor / 'd3-dag.iife.min.js').exists() and not (vendor / 'd3.min.js').exists()
    assert set(tv._load_lineage_vendor_scripts()) == {'lineage-renderer.js'}
    html = tv.build_archive_tree({'nodes': {}}, ROWS, ledger_history=ROWS, now='2026-09-01T05:00:00Z')
    assert 'sugiyama' not in html and 'graphStratify' not in html
    assert 'function renderDay' in html and 'lineageDayFilter' in html
    assert not re.search(r'<script[^>]+src=', html, re.I)
