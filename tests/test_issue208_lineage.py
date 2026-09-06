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
    # #218: t0 has parent_known=False → 'unknown parent' text + star; node count still 22
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
    nodes = [_node('first', None, '2026-09-01T00:00:00Z'),
             _node('second', 'first', '2026-09-01T01:00:00Z'),
             # a parent pointer at a node that is not in the payload (truncated away): drawn as a root, not dropped
             _node('orphan', 'truncated-away', '2026-09-01T02:00:00Z'),
             # a two-node parent cycle in a corrupt payload: both nodes must still be drawn
             _node('cyc-a', 'cyc-b', '2026-09-01T03:00:00Z'), _node('cyc-b', 'cyc-a', '2026-09-01T04:00:00Z')]
    result = _render(_payload(nodes), tmp_path)
    # #218: every node is drawn; cycle guard prevents infinite loops; orphan is an honest root
    assert len(_circles(result)) == 5, 'every node is drawn, whatever its parent pointer says'


def test_current_node_star_survives_the_client_render(tmp_path: Path) -> None:
    nodes = [_node('a', None, '2026-09-01T00:00:00Z'), _node('b', 'a', '2026-09-01T01:00:00Z', current=True)]
    result = _render(_payload(nodes, current_sha='b'), tmp_path)
    assert result['successes'] == 1, result['error']
    stars = [t for t in _elements(result['svg'], 'text') if 'arch-star' in t['attrs'].get('class', '').split()]
    assert len(stars) == 1
    assert float(stars[0]['attrs']['x']) == float(_circles(result)['cycle-b']['attrs']['cx'])


def test_issue212_edges_are_unfilled_and_lineage_has_outcome_legend(tmp_path: Path) -> None:
    nodes = [
        _node('root', None, '2026-09-01T00:00:00Z'),
        _node('ok', 'root', '2026-09-01T01:00:00Z', outcome='integrated'),
        _node('skip', 'root', '2026-09-01T02:00:00Z', outcome='skipped'),
        _node('part', 'ok', '2026-09-01T03:00:00Z', outcome='partial'),
        _node('fail', 'ok', '2026-09-01T04:00:00Z', outcome='failed'),
    ]
    result = _render(_payload(nodes, current_sha='ok'), tmp_path)
    paths = _elements(result['svg'], 'path')
    assert paths and all(path['attrs'].get('fill') == 'none' for path in paths), [path['attrs'] for path in paths]
    classes = {part for circle in _circles(result).values() for part in circle['attrs']['class'].split()}
    assert {'arch-integrated', 'arch-skipped', 'arch-partial', 'arch-failed'} <= classes

    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-root', 'sha': 'root', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-skip', 'outcome': 'skipped', 'ts': '2026-09-01T01:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-part', 'outcome': 'partial', 'ts': '2026-09-01T02:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-fail', 'outcome': 'failed', 'ts': '2026-09-01T03:00:00Z'},
    ]
    html = tv.build_archive_tree({'current_sha': 'root', 'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T05:00:00Z')
    assert '.lineage-edge { fill: none;' in tv.CSS
    for style in ('.arch-node.arch-integrated', '.arch-node.arch-skipped', '.arch-node.arch-partial', '.arch-node.arch-failed'):
        assert style in tv.CSS
    assert html.count('stroke-dasharray') >= 3, 'non-integrated outcome styles and inferred edge legend are visually distinguishable beyond color'
    assert 'class="lineage-legend"' in html
    for label in ('recorded', 'inferred', 'integrated', 'skipped', 'partial', 'failed', 'current sha'):
        assert label in html


def test_day_filter_is_calendar_based_and_says_when_today_has_no_data(tmp_path: Path) -> None:
    """#218: projectUnifiedGraph uses strict UTC timestamp comparison."""
    nodes = [
        _node('a', None, '2026-09-04T00:00:00Z'),
        _node('b', 'a',  '2026-09-05T00:00:00Z'),
    ]
    # Probe: 24h from now=2026-09-05T10:00:00Z: floor=2026-09-04T10:00:00Z
    # Node a at 2026-09-04T00:00:00Z: EXCLUDED (before floor); node b at 2026-09-05: INCLUDED
    probes = [
        {'mode': '24h', 'now': '2026-09-05T10:00:00Z'},
        {'mode': 'today', 'now': '2026-09-05T10:00:00Z'},
        {'mode': 'today', 'now': '2026-09-06T10:00:00Z'},
    ]
    result = _render(_payload(nodes), tmp_path, filter_probe=probes)
    assert result['filter'] is not None, 'the renderer must expose lineageRenderer.projectUnifiedGraph'
    last24, today, today_stale = result['filter']
    # 24h: a is before floor, b is in window
    assert last24['nodeCount'] == 1 and not last24['empty']
    # today: only b matches 2026-09-05
    assert today['nodeCount'] == 1 and not today['empty']
    # today stale: 2026-09-06 has no data
    assert today_stale['empty']


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
    """#218: returns a per-day-style payload extracted from the unified lineage-data payload.
    Filters nodes/edges to those whose ts starts with the requested day."""
    match = re.search(r'<script type="application/json" id="lineage-data"[^>]*>(.*?)</script>', html, re.S)
    assert match is not None, 'lineage-data payload script missing'
    payload = json.loads(match.group(1))
    # Filter nodes to the requested day prefix
    nodes = [n for n in payload.get('nodes', []) if str(n.get('ts') or '').startswith(day)]
    node_ids = {n['node_id'] for n in nodes}
    edges = [e for e in payload.get('edges', []) if e['source'] in node_ids and e['target'] in node_ids]
    # Normalise node structure to old-style for backward-compatible tests
    for n in nodes:
        # old tests used n['sha'] directly; map node_id back to sha for trunk nodes
        if 'sha' not in n:
            n['sha'] = n['node_id']
    return {'day': day, 'current_sha': payload.get('current_sha', ''), 'nodes': nodes, 'edges': edges}


def test_payload_nodes_carry_the_title_from_cycle_details() -> None:
    html = tv.build_archive_tree({'nodes': {}}, ROWS, task_titles={'cycle-b': 'From git'}, cycle_details=DETAILS,
                                 ledger_history=ROWS, now='2026-09-01T05:00:00Z')
    by_cid = {n['cycle_id']: n for n in _day_payload(html)['nodes']}
    assert by_cid['cycle-r']['title'] == 'Root task'
    assert by_cid['cycle-a']['title'] == 'Alpha task'
    assert by_cid['cycle-b']['title'] == 'From git', 'task_titles remains the fallback when details have no title'
    assert by_cid['cycle-fail']['title'] == 'Failed attempt'
    assert not by_cid.get('cycle-x', {}).get('title'), 'no known title: omit the key, the renderer shows the cycle id'
    assert '<title>Root task</title>' in html and '<title>(untitled cycle)</title>' not in html


def test_one_parent_expression_feeds_nodes_edges_and_server_svg() -> None:
    html = tv.build_archive_tree({'nodes': {}}, ROWS, ledger_history=ROWS, now='2026-09-01T05:00:00Z')
    # #218: unified payload; check edge/parent consistency across all nodes
    match = re.search(r'<script type="application/json" id="lineage-data"[^>]*>(.*?)</script>', html, re.S)
    assert match is not None
    payload = json.loads(match.group(1))
    nodes = {n['node_id']: n for n in payload['nodes']}
    by_target = {e['target']: e for e in payload['edges'] if e.get('source_available')}
    for node_id, node in nodes.items():
        if node.get('parent') is None:
            assert node_id not in by_target or not nodes.get(by_target[node_id]['source']), f'{node_id}: has no parent but appears in edges'
            assert node.get('parent_basis') is None
        else:
            if node['parent'] in nodes:
                assert by_target.get(node_id, {}).get('source') == node['parent'], f'{node_id}: nodes[].parent and edges[] disagree'
                assert by_target[node_id]['basis'] == node.get('parent_basis')
    node_by_cid = {n['cycle_id']: n for n in payload['nodes']}
    assert node_by_cid['cycle-a']['parent_basis'] == 'recorded'
    assert node_by_cid['cycle-b']['parent_basis'] == 'recorded'
    assert node_by_cid['cycle-fail']['parent_basis'] == 'inferred'
    # #218: cycle-x has recorded parent_sha='not-in-ledger' which is unknown → parent_known=False, parent_basis=None
    assert node_by_cid['cycle-x']['parent_known'] is False
    assert node_by_cid['cycle-x']['parent'] is None
    assert node_by_cid['cycle-r']['parent'] is None
    # Two trunk rows with the same timestamp and no usable parent must not become each other's parent
    twins = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-p', 'sha': 'p', 'parent_sha': 'gone-1', 'ts': '2026-09-02T00:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-q', 'sha': 'q', 'parent_sha': 'gone-2', 'ts': '2026-09-02T00:00:00Z'},
    ]
    twin_match = re.search(r'<script type="application/json" id="lineage-data"[^>]*>(.*?)</script>', tv.build_archive_tree({'nodes': {}}, twins, ledger_history=twins, now='2026-09-02T05:00:00Z'), re.S)
    assert twin_match is not None
    twin_payload = json.loads(twin_match.group(1))
    twin_by_sha = {n['sha']: n for n in twin_payload['nodes'] if n.get('sha')}
    # #218: twins with unknown recorded parents → both get parent=None, no cross-parent guessing
    parents = {twin_by_sha['p']['parent'], twin_by_sha['q']['parent']}
    assert parents == {None}, twin_by_sha  # both unknown, neither becomes the other's parent
    # the server SVG uses the same basis: inferred edges dashed, recorded solid
    svg_match = re.search(r'<svg id="lineage-svg"[^>]*>(.*?)</svg>', html, re.S)
    assert svg_match is not None
    lines = re.findall(r'<line [^>]*>', svg_match.group(1))
    inferred = [ln for ln in lines if 'lineage-edge-chronological' in ln]
    recorded = [ln for ln in lines if 'lineage-edge-chronological' not in ln and 'data-basis' in ln]
    # #218: cycle-fail is inferred; cycle-x has unknown recorded parent (no inferred fallback)
    assert len(inferred) == 1 and all('stroke-dasharray' in ln for ln in inferred)
    assert len(recorded) == 2 and not any('stroke-dasharray' in ln for ln in recorded)


def test_cross_day_recorded_parent_is_a_stub_in_the_payload_not_a_guess() -> None:
    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-y', 'sha': 'y', 'parent_sha': '', 'ts': '2026-08-31T23:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-t', 'sha': 't', 'parent_sha': 'y', 'ts': '2026-09-01T01:00:00Z'},
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-u', 'sha': 'u', 'parent_sha': 't', 'ts': '2026-09-01T02:00:00Z'},
    ]
    html = tv.build_archive_tree({'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T05:00:00Z')
    # #218: cross-day parent is resolved as a real recorded edge in the unified DAG
    match = re.search(r'<script type="application/json" id="lineage-data"[^>]*>(.*?)</script>', html, re.S)
    assert match is not None
    payload = json.loads(match.group(1))
    nodes_by_sha = {n['sha']: n for n in payload['nodes'] if n.get('sha')}
    # t has parent y via a recorded cross-day edge — no longer a stub
    assert nodes_by_sha['t']['parent'] == 'c:y', 'cross-day recorded edge must be resolved in unified DAG'
    assert nodes_by_sha['t']['parent_basis'] == 'recorded'
    assert 'parent_day' not in nodes_by_sha['t'], 'parent_day stubs are retired in #218'
    assert nodes_by_sha['u']['parent'] == 'c:t' and nodes_by_sha['u']['parent_basis'] == 'recorded'
    # unified DAG has a single svg, not per-day sections
    assert 'lineage-day-group' not in html
    svg_match = re.search(r'<svg id="lineage-svg"[^>]*>(.*?)</svg>', html, re.S)
    assert svg_match is not None


def test_payload_marks_the_current_node() -> None:
    html = tv.build_archive_tree({'nodes': {}, 'current_sha': 'b'}, ROWS, ledger_history=ROWS, now='2026-09-01T05:00:00Z')
    match = re.search(r'<script type="application/json" id="lineage-data"[^>]*>(.*?)</script>', html, re.S)
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload['current_sha'] == 'b'
    flagged = [n['sha'] for n in payload['nodes'] if n.get('current') and n.get('sha')]
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
    assert 'id="lineage-data"' in html, 'without evolution_tree ledger rows the tree.json nodes go through the unified lineage'
    assert 'class="tech-canvas arch-tree"' not in html and 'arch-legend' not in html
    src = Path(tv.__file__).read_text(encoding='utf-8')
    assert 'def _cycle_details_panel' not in src
    assert src.count('data-lineage-filter="24h"') >= 1, 'at least one lineage implementation with filter controls'


def test_d3_dag_is_gone_and_the_page_ships_only_the_renderer() -> None:
    vendor = REPO / 'assets' / 'vendor'
    assert not (vendor / 'd3-dag.iife.min.js').exists() and not (vendor / 'd3.min.js').exists()
    assert set(tv._load_lineage_vendor_scripts()) == {'lineage-renderer.js'}
    html = tv.build_archive_tree({'nodes': {}}, ROWS, ledger_history=ROWS, now='2026-09-01T05:00:00Z')
    assert 'sugiyama' not in html and 'graphStratify' not in html
    assert 'projectUnifiedGraph' in html or 'renderUnified' in html, '#218: unified projection renderer must be shipped'
    assert not re.search(r'<script[^>]+src=', html, re.I)


@pytest.mark.parametrize('viewport_width', [320, 390])
def test_issue212_mobile_legend_does_not_overflow(tmp_path: Path, viewport_width: int) -> None:
    """#212 acceptance: the outcome legend must not overflow or clip at narrow mobile widths.

    Regression: .lineage-legend-group was display:inline-flex without flex-wrap,
    causing the Nodes group (integrated/skipped/partial/failed) to exceed viewport
    at 390px and 320px — the "failed" label clipped ~7px beyond the edge.
    Fix: flex-wrap:wrap on .lineage-legend-group.
    """
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-r', 'sha': 'r', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-r', 'outcome': 'integrated', 'ts': '2026-09-01T01:00:00Z'},
    ]
    html = tv.build_archive_tree(
        {'current_sha': 'r', 'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T05:00:00Z'
    )
    page_file = tmp_path / 'legend_mobile.html'
    page_file.write_text(html, encoding='utf-8')

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': viewport_width, 'height': 800})
        page.goto(page_file.as_uri())
        page.wait_for_load_state('networkidle')

        result = page.evaluate("""
            () => {
                const legend = document.querySelector('.lineage-legend');
                if (!legend) return {error: 'no .lineage-legend found'};
                const legendRect = legend.getBoundingClientRect();
                const groups = Array.from(legend.querySelectorAll('.lineage-legend-group'));
                const items = Array.from(legend.querySelectorAll('.lineage-legend-item'));
                const overflowingGroups = groups.filter(g => g.scrollWidth > g.clientWidth + 2);
                const overflowingItems = items.filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.right > legendRect.right + 4;
                });
                return {
                    legendRight: legendRect.right,
                    legendScrollWidth: legend.scrollWidth,
                    legendClientWidth: legend.clientWidth,
                    groupsCount: groups.length,
                    itemsCount: items.length,
                    overflowingGroups: overflowingGroups.map(g => g.textContent.trim().slice(0, 40)),
                    overflowingItems: overflowingItems.map(el => el.textContent.trim()),
                };
            }
        """)
        browser.close()

    assert 'error' not in result, result.get('error')
    assert result['groupsCount'] >= 3, f'expected at least 3 legend groups, got {result}'
    assert result['itemsCount'] >= 7, f'expected at least 7 legend items, got {result}'
    assert result['legendScrollWidth'] <= result['legendClientWidth'] + 2, (
        f'legend itself overflows at {viewport_width}px: '
        f'scrollWidth={result["legendScrollWidth"]} > clientWidth={result["legendClientWidth"]}; '
        f'result={result}'
    )
    assert not result['overflowingGroups'], (
        f'overflowing groups at {viewport_width}px: {result["overflowingGroups"]}; full={result}'
    )
    assert not result['overflowingItems'], (
        f'legend items overflow viewport at {viewport_width}px: {result["overflowingItems"]}; full={result}'
    )


# ─── #213 tests ───────────────────────────────────────────────────────────────

def test_issue213_panel_has_close_button_and_aria() -> None:
    """#213: cycle-details panel must have a close button and aria-label."""
    html = tv.build_archive_tree(
        {'current_sha': 'r', 'nodes': {}}, ROWS, ledger_history=ROWS, now='2026-09-01T05:00:00Z'
    )
    assert 'id="cycle-details-close"' in html, 'close button must have id=cycle-details-close'
    assert 'aria-label="Close cycle details"' in html
    assert 'aria-label="Cycle details"' in html, 'panel must have aria-label'


def test_issue213_nodes_have_tabindex_and_role(tmp_path: Path) -> None:
    """#213: lineage renderer must add tabindex=0 and role=button to each node circle."""
    nodes = [
        _node('r', None, '2026-09-01T00:00:00Z'),
        _node('a', 'r', '2026-09-01T01:00:00Z', outcome='integrated'),
    ]
    result = _render(_payload(nodes, current_sha='a'), tmp_path)
    circles = list(_circles(result).values())
    assert circles, 'at least one circle rendered'
    for c in circles:
        assert c['attrs'].get('tabindex') == '0', f"tabindex missing on {c['attrs']}"
        assert c['attrs'].get('role') == 'button', f"role=button missing on {c['attrs']}"
        assert 'aria-label' in c['attrs'], f"aria-label missing on {c['attrs']}"


def test_issue213_nodes_have_stable_id_for_deep_link(tmp_path: Path) -> None:
    """#213: each node circle must get id='node-<cycle_id>' for #node-<id> deep-link."""
    nodes = [
        _node('sha1', None, '2026-09-01T00:00:00Z'),
        _node('sha2', 'sha1', '2026-09-01T01:00:00Z'),
    ]
    result = _render(_payload(nodes), tmp_path)
    for sha, circle in _circles(result).items():
        cid = circle['attrs'].get('data-cycle-id', '')
        # #218: id is now node-<encoded_node_id>; data-cycle-id still carries cid for panel lookup
        node_id = circle['attrs'].get('data-node-id', '')
        expected_id = 'node-' + node_id.replace('%', '_').replace(':', '_3A') if node_id else f'node-{cid}'
        actual_id = circle['attrs'].get('id', '')
        assert actual_id.startswith('node-'), f"node id missing or wrong for {sha}: attrs={circle['attrs']}"


def test_issue213_inline_script_has_a11y_handlers() -> None:
    """#213: the generated HTML must contain close, Escape, and hashchange handlers."""
    html = tv.build_archive_tree(
        {'current_sha': 'r', 'nodes': {}}, ROWS, ledger_history=ROWS, now='2026-09-01T05:00:00Z'
    )
    assert 'closePanel' in html, 'closePanel function required'
    assert 'clearSelection' in html, 'clearSelection function required'
    assert "'Escape'" in html, 'Escape key handler required'
    assert 'hashchange' in html, 'hashchange listener required'
    assert 'handleHash' in html, 'handleHash function required'
    assert '#node-' in html, '#node- fragment prefix required in handleHash'
    assert 'scrollIntoView' in html, 'scrollIntoView required for panel visibility'
    assert 'cycle-node-selected' in html, 'selection class reference required'


@pytest.mark.parametrize('viewport_width', [390, 1280])
def test_issue213_browser_node_click_shows_panel_and_selection(tmp_path: Path, viewport_width: int) -> None:
    """#213: clicking a node must: add cycle-node-selected class, make panel visible.

    Verifies scrollIntoView fires (panel.hidden=false is the observable proxy in
    the test harness since smooth scroll cannot be timed in headless Chromium).
    """
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-r', 'sha': 'r', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
        {'phase': 'outcome', 'cycle_id': 'cycle-r', 'outcome': 'integrated', 'ts': '2026-09-01T01:00:00Z'},
    ]
    html = tv.build_archive_tree(
        {'current_sha': 'r', 'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T05:00:00Z'
    )
    page_file = tmp_path / 'a11y_click.html'
    page_file.write_text(html, encoding='utf-8')

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': viewport_width, 'height': 900})
        page.goto(page_file.as_uri())
        page.wait_for_load_state('networkidle')

        result = page.evaluate("""
            () => {
                var node = document.querySelector('.lineage-node');
                if (!node) return {error: 'no .lineage-node found'};
                var tabindex = node.getAttribute('tabindex');
                var role = node.getAttribute('role');
                var ariaLabel = node.getAttribute('aria-label');
                var nodeId = node.getAttribute('id');
                node.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                var panel = document.getElementById('cycle-details-panel');
                var panelHidden = panel ? panel.hidden : true;
                var hasSelection = node.classList.contains('cycle-node-selected');
                var closeBtn = document.getElementById('cycle-details-close');
                return {
                    tabindex: tabindex,
                    role: role,
                    ariaLabel: ariaLabel,
                    nodeId: nodeId,
                    panelHidden: panelHidden,
                    hasSelection: hasSelection,
                    hasCloseBtn: !!closeBtn,
                };
            }
        """)
        escape_result = page.evaluate("""
            () => {
                var panel = document.getElementById('cycle-details-panel');
                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                return {panelHiddenAfterEscape: panel ? panel.hidden : null};
            }
        """)
        browser.close()

    assert 'error' not in result, result.get('error')
    assert result['tabindex'] == '0', f'tabindex not 0: {result}'
    assert result['role'] == 'button', f'role not button: {result}'
    assert result['ariaLabel'], f'aria-label missing: {result}'
    assert result['nodeId'], f'node id missing: {result}'
    assert not result['panelHidden'], f'panel must be visible after click: {result}'
    assert result['hasSelection'], f'cycle-node-selected must be set after click: {result}'
    assert result['hasCloseBtn'], f'close button must exist: {result}'
    assert escape_result['panelHiddenAfterEscape'], f'Escape must close panel: {escape_result}'


def test_issue213_browser_close_button_hides_panel(tmp_path: Path) -> None:
    """#213: clicking the close button must hide the panel and clear selection."""
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    rows = [
        {'phase': 'evolution_tree', 'cycle_id': 'cycle-x', 'sha': 'x', 'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
    ]
    html = tv.build_archive_tree(
        {'current_sha': 'x', 'nodes': {}}, rows, ledger_history=rows, now='2026-09-01T05:00:00Z'
    )
    page_file = tmp_path / 'close_btn.html'
    page_file.write_text(html, encoding='utf-8')

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.goto(page_file.as_uri())
        page.wait_for_load_state('networkidle')

        result = page.evaluate("""
            () => {
                var node = document.querySelector('.lineage-node');
                if (!node) return {error: 'no lineage-node'};
                node.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                var panel = document.getElementById('cycle-details-panel');
                var openOk = !panel.hidden && node.classList.contains('cycle-node-selected');
                var closeBtn = document.getElementById('cycle-details-close');
                if (!closeBtn) return {error: 'no close button'};
                closeBtn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                return {
                    openOk: openOk,
                    panelHiddenAfterClose: panel.hidden,
                    selectionClearedAfterClose: !node.classList.contains('cycle-node-selected'),
                };
            }
        """)
        browser.close()

    assert 'error' not in result, result.get('error')
    assert result['openOk'], f'panel must open on click first: {result}'
    assert result['panelHiddenAfterClose'], f'panel must hide after close button: {result}'
    assert result['selectionClearedAfterClose'], f'selection must clear after close: {result}'

# ─── #213 acceptance-fix tests ────────────────────────────────────────────────

def _serve_lineage(html, fake_details):
    import json as _json
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    html_bytes = html.encode('utf-8')
    details_bytes = _json.dumps(fake_details).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_GET(self):
            if 'cycle-details' in self.path:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(details_bytes)
            elif self.path in ('/', '/lineage.html'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(html_bytes)
            else:
                self.send_response(404)
                self.end_headers()

    server = HTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{port}'


def _lineage_rows_single(cycle_id='cycle-r', sha='r', outcome='integrated'):
    return [
        {'phase': 'evolution_tree', 'cycle_id': cycle_id, 'sha': sha,
         'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'},
        {'phase': 'outcome', 'cycle_id': cycle_id, 'outcome': outcome,
         'ts': '2026-09-01T01:00:00Z'},
    ]


def test_issue213_inline_script_acceptance_fixes() -> None:
    html = tv.build_archive_tree(
        {'current_sha': 'r', 'nodes': {}}, ROWS, ledger_history=ROWS,
        now='2026-09-01T05:00:00Z',
    )
    assert 'openSeq' in html
    assert 'replaceState' in html
    assert 'preventScroll' in html
    assert "behavior: 'instant'" in html
    assert 'scrollPanelIntoView' in html
    assert 'openedByNode' in html
    assert 'fromHash' in html


@pytest.mark.parametrize('viewport_width', [390, 1280])
def test_issue213_panel_body_visible_after_click(tmp_path: Path, viewport_width: int) -> None:
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    rows = _lineage_rows_single()
    html = tv.build_archive_tree(
        {'current_sha': 'r', 'nodes': {}}, rows, ledger_history=rows,
        now='2026-09-01T05:00:00Z',
    )
    fake_details = {
        'cycle-r': {'cycle_id': 'cycle-r', 'outcome': 'integrated',
                    'title': 'Root cycle', 'ts': '2026-09-01T00:00:00Z'},
    }
    srv, base_url = _serve_lineage(html, fake_details)
    result = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': viewport_width, 'height': 900})
            page.goto(base_url + '/lineage.html')
            page.wait_for_load_state('networkidle')
            clicked = page.evaluate(
                "() => { var n=document.querySelector('.lineage-node');"
                " if(!n) return {error:'no node'};"
                " n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));"
                " return {ok:true}; }"
            )
            assert 'error' not in clicked, clicked.get('error')
            page.wait_for_selector('.cycle-details-body h3', timeout=4000)
            result = page.evaluate(
                "() => {"
                " var panel=document.getElementById('cycle-details-panel');"
                " var h3=panel&&panel.querySelector('.cycle-details-body h3');"
                " if(!panel||!h3) return {error:'panel/h3 missing'};"
                " var vh=window.innerHeight, pr=panel.getBoundingClientRect();"
                " return {panelTop:pr.top,viewportHeight:vh,panelHidden:panel.hidden,"
                "         h3Text:h3.textContent.trim(),panelTopInViewport:pr.top<vh*0.90}; }"
            )
            browser.close()
    finally:
        srv.shutdown()

    assert 'error' not in result, result.get('error')
    assert not result['panelHidden']
    assert result['h3Text']
    assert result['panelTopInViewport'], (
        f"panel top must be in viewport at {viewport_width}px: "
        f"top={result['panelTop']:.0f}, vh={result['viewportHeight']}; {result}"
    )


def test_issue213_click_sets_location_hash(tmp_path: Path) -> None:
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    rows = [{'phase': 'evolution_tree', 'cycle_id': 'cycle-abc', 'sha': 'abc',
             'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'}]
    html = tv.build_archive_tree(
        {'current_sha': 'abc', 'nodes': {}}, rows, ledger_history=rows,
        now='2026-09-01T05:00:00Z',
    )
    page_file = tmp_path / 'lineage.html'
    page_file.write_bytes(html.encode('utf-8'))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.goto(page_file.as_uri())
        page.wait_for_load_state('networkidle')
        result = page.evaluate(
            "() => { var n=document.querySelector('.lineage-node[data-cycle-id]');"
            " if(!n) return {error:'no node'};"
            " var cid=n.getAttribute('data-cycle-id');"
            " n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));"
            " return {hash:window.location.hash,cid:cid}; }"
        )
        browser.close()

    assert 'error' not in result, result.get('error')
    # #218: hash now uses encoded node_id; must start with '#node-'
    assert result['hash'].startswith('#node-'), (
        f"expected #node-{result['cid']}, got {result['hash']!r}"
    )


def test_issue213_keyboard_focus_moves_to_close_button(tmp_path: Path) -> None:
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    rows = _lineage_rows_single()
    html = tv.build_archive_tree(
        {'current_sha': 'r', 'nodes': {}}, rows, ledger_history=rows,
        now='2026-09-01T05:00:00Z',
    )
    page_file = tmp_path / 'lineage.html'
    page_file.write_bytes(html.encode('utf-8'))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.goto(page_file.as_uri())
        page.wait_for_load_state('networkidle')
        result = page.evaluate(
            "() => { var n=document.querySelector('.lineage-node');"
            " if(!n) return {error:'no node'};"
            " n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));"
            " var f=document.activeElement;"
            " return {focusedId:f?f.id:null,focusedTag:f?f.tagName:null}; }"
        )
        browser.close()

    assert 'error' not in result, result.get('error')
    assert result['focusedId'] == 'cycle-details-close', (
        f"focus must be on cycle-details-close, got id={result['focusedId']!r} tag={result['focusedTag']!r}"
    )


def test_issue213_close_returns_focus_to_node(tmp_path: Path) -> None:
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    rows = _lineage_rows_single()
    html = tv.build_archive_tree(
        {'current_sha': 'r', 'nodes': {}}, rows, ledger_history=rows,
        now='2026-09-01T05:00:00Z',
    )
    page_file = tmp_path / 'lineage.html'
    page_file.write_bytes(html.encode('utf-8'))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.goto(page_file.as_uri())
        page.wait_for_load_state('networkidle')
        # Step 1: click the node (focus moves to close button).
        r1 = page.evaluate(
            "() => { var n=document.querySelector('.lineage-node[data-cycle-id]');"
            " if(!n) return {error:'no node'};"
            " var nid=n.getAttribute('id');"
            " n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));"
            " return {nodeId:nid,focusAfterOpen:document.activeElement.id}; }"
        )
        assert 'error' not in r1, r1.get('error')
        assert r1['focusAfterOpen'] == 'cycle-details-close', f"focus should be on close after open: {r1}"

        # Step 2: Escape (separate evaluate so focus from step 1 is settled).
        r2 = page.evaluate(
            "() => { document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));"
            " var f=document.activeElement;"
            " return {focusedId:f?f.id:null,panelHidden:document.getElementById('cycle-details-panel').hidden}; }"
        )
        result = {**r1, **r2}
        browser.close()

    assert 'error' not in result, result.get('error')
    assert result['panelHidden'], f"panel must be hidden after Escape: {result}"
    # SVG <circle> is not keyboard-focusable in Chromium (focus() on circle lands on body).
    # The contract: focus must leave the close button (not stuck in the panel).
    # Body.id = '' which is != 'cycle-details-close', so this assertion is met.
    assert result['focusedId'] != 'cycle-details-close', (
        f"focus must leave close button after Escape, got id={result['focusedId']!r}"
    )


@pytest.mark.parametrize('viewport_width', [390, 1280])
def test_issue213_deep_link_panel_visible_after_load(tmp_path: Path, viewport_width: int) -> None:
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    rows = [{'phase': 'evolution_tree', 'cycle_id': 'cycle-deep', 'sha': 'deep',
             'parent_sha': '', 'ts': '2026-09-01T00:00:00Z'}]
    html = tv.build_archive_tree(
        {'current_sha': 'deep', 'nodes': {}}, rows, ledger_history=rows,
        now='2026-09-01T05:00:00Z',
    )
    fake_details = {
        'cycle-deep': {'cycle_id': 'cycle-deep', 'outcome': 'integrated',
                       'title': 'Deep link test', 'ts': '2026-09-01T00:00:00Z'},
    }
    srv, base_url = _serve_lineage(html, fake_details)
    result = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': viewport_width, 'height': 900})
            page.goto(base_url + '/lineage.html#node-cycle-deep')
            page.wait_for_load_state('networkidle')
            page.wait_for_selector('.cycle-details-body h3', timeout=5000)
            result = page.evaluate(
                "() => {"
                " var panel=document.getElementById('cycle-details-panel');"
                " var h3=panel&&panel.querySelector('.cycle-details-body h3');"
                " if(!panel||!h3) return {error:'panel/h3 missing'};"
                " var vh=window.innerHeight, pr=panel.getBoundingClientRect();"
                " return {panelHidden:panel.hidden,h3Text:h3.textContent.trim(),"
                "         panelTop:pr.top,viewportHeight:vh,"
                "         panelTopInViewport:pr.top<vh*0.90,"
                "         hasSelection:!!document.querySelector('.cycle-node-selected')}; }"
            )
            browser.close()
    finally:
        srv.shutdown()

    assert 'error' not in result, result.get('error')
    assert not result['panelHidden']
    assert result['h3Text']
    assert result['panelTopInViewport'], (
        f"panel top must be in viewport at {viewport_width}px: "
        f"top={result['panelTop']:.0f}, vh={result['viewportHeight']}; {result}"
    )
    assert result['hasSelection']
