"""Issue #436: current-node orientation and same-graph mobile cycle list."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tests'))
from scripts import techtree_viewer as tv  # noqa: E402
from test_issue208_lineage import _serve_lineage  # noqa: E402
from test_issue250_lineage_viewport import _open  # noqa: E402
from test_techtree_viewer import _fixture  # noqa: E402


def _pages(current='sha-current'):
    rows = []
    for sha, title, ts, outcome in [
        ('sha-old', 'An unusually long retained-history title that must wrap on a phone without clipping', '2025-12-31T23:00:00Z', 'failed'),
        ('sha-current', 'Current useful result', '2026-01-02T00:30:00Z', 'integrated'),
    ]:
        rows.extend([
            {'phase': 'evolution_tree', 'cycle_id': 'cycle-' + sha, 'sha': sha,
             'parent_sha': 'sha-old' if sha == 'sha-current' else '', 'ts': ts},
            {'phase': 'outcome', 'cycle_id': 'cycle-' + sha, 'outcome': outcome, 'ts': ts},
        ])
    data = _fixture()
    data['evolution_tree'] = {'nodes': {}, 'current_sha': current}
    data['ledger_tail'] = rows
    data['ledger_history'] = rows
    data['cycle_titles'] = {'cycle-sha-old': 'An unusually long retained-history title that must wrap on a phone without clipping', 'cycle-sha-current': 'Current useful result'}
    return tv.render_pages(data, host='eeepc', generated_at='2026-01-02 01:00:00')


def test_issue436_current_control_list_and_retention_copy_are_emitted():
    html = _pages()['lineage.html']
    assert 'data-lineage-current' in html and 'aria-label="Locate current node"' in html
    assert 'data-lineage-cycle-list' in html
    assert 'Date filters; visibility only, independent from retention and ancestry' in html
    assert '<dialog class="lineage-list-dialog"' in html
    assert 'older or missing ancestry may be unavailable' in html
    assert 'data-lineage-default-mode="today"' in html
    graph = re.search(r'<svg id="lineage-svg"[^>]+>', html)
    assert graph and '{width}' not in graph.group() and '{height}' not in graph.group()


@pytest.mark.parametrize('viewport', [(390, 844), (1440, 1000)])
def test_issue436_current_and_list_select_same_emitted_node(viewport):
    page, close = _open(_pages(), *viewport)
    try:
        opener = page.locator('[data-lineage-list-open]')
        opener.click()
        dialog = page.locator('.lineage-list-dialog')
        assert dialog.evaluate('(d) => d.open')
        page.keyboard.press('Escape')
        assert not dialog.evaluate('(d) => d.open')
        assert page.evaluate('document.activeElement === document.querySelector("[data-lineage-list-open]")')
        coverage = page.locator('.lineage-coverage-note')
        assert 'Date filters change visibility, not retained history.' in coverage.inner_text()
        page.locator('[data-lineage-filter="all"]').click()
        assert 'Date filters change visibility, not retained history.' in coverage.inner_text()
        page.locator('[data-lineage-filter="today"]').click()
        assert 'Date filters change visibility, not retained history.' in coverage.inner_text()
        page.locator('[data-lineage-filter="all"]').click()
        page.locator('[data-lineage-current]').click()
        page.wait_for_selector('.cycle-details-body h3')
        assert page.locator('#lineage-svg .lineage-node.cycle-node-selected').get_attribute('data-sha') == 'sha-current'
        assert page.locator('[data-lineage-cycle-list] [aria-pressed="true"]').get_attribute('data-lineage-list-node') == 'c:sha-current'
        assert 'Current useful result' in page.locator('.cycle-details-body').inner_text()
        graph = page.locator('[data-lineage-graph-scroll]')
        assert page.locator('#lineage-svg .cycle-node-selected').evaluate('''node => {
            const graph = document.querySelector('[data-lineage-graph-scroll]').getBoundingClientRect();
            const box = node.getBoundingClientRect();
            return box.left >= graph.left && box.right <= graph.right && box.top >= graph.top && box.bottom <= graph.bottom;
        }''')
        page.locator('#cycle-details-close').click()
        page.locator('[data-lineage-list-open]').click()
        page.locator('[data-lineage-list-node="c:sha-old"]').click()
        assert not page.locator('.lineage-list-dialog').evaluate('(d) => d.open')
        assert page.locator('#lineage-svg .lineage-node.cycle-node-selected').get_attribute('data-sha') == 'sha-old'
        assert page.locator('[data-lineage-cycle-list] [aria-pressed="true"]').get_attribute('data-lineage-list-node') == 'c:sha-old'
    finally:
        close()


def test_issue436_current_locator_reveals_offscreen_node_without_page_scroll():
    page, close = _open(__import__('test_issue250_lineage_viewport')._deep_chain_pages(), 390, 844)
    try:
        graph = page.locator('[data-lineage-graph-scroll]')
        node = page.locator('#lineage-svg .lineage-node[data-sha="sha-39"]')
        initial_page_y = page.evaluate('window.scrollY')
        assert not node.evaluate('''node => {
            const graph = document.querySelector('[data-lineage-graph-scroll]').getBoundingClientRect();
            const box = node.getBoundingClientRect();
            return box.left >= graph.left && box.right <= graph.right && box.top >= graph.top && box.bottom <= graph.bottom;
        }''')
        page.locator('[data-lineage-current]').click()
        assert node.evaluate('''node => {
            const graph = document.querySelector('[data-lineage-graph-scroll]').getBoundingClientRect();
            const panel = document.querySelector('#cycle-details-panel').getBoundingClientRect();
            const box = node.getBoundingClientRect();
            const contained = box.left >= graph.left && box.right <= graph.right && box.top >= graph.top && box.bottom <= graph.bottom;
            const unobscured = panel.bottom <= box.top || panel.top >= box.bottom || panel.right <= box.left || panel.left >= box.right;
            const hit = document.elementFromPoint((box.left + box.right) / 2, (box.top + box.bottom) / 2);
            return contained && unobscured && (hit === node || node.contains(hit));
        }''')
        assert page.evaluate('window.scrollY') == initial_page_y
        assert graph.evaluate('(e) => e.clientHeight') >= 100
        assert page.locator('.cycle-details-body').is_visible()
    finally:
        close()


@pytest.mark.parametrize('viewport', [(320, 568), (390, 844), (1440, 1000)])
def test_issue436_current_locator_does_not_occlude_shallow_node(viewport):
    data = _fixture()
    data['evolution_tree'] = {'nodes': {}, 'current_sha': 'sha-current'}
    data['ledger_tail'] = [{'phase': 'evolution_tree', 'cycle_id': 'current', 'sha': 'sha-current', 'parent_sha': '', 'ts': '2026-01-02T00:30:00Z'}]
    data['ledger_history'] = data['ledger_tail']
    pages = tv.render_pages(data, host='eeepc', generated_at='2026-01-02 01:00:00')
    page, close = _open(pages, *viewport)
    try:
        node = page.locator('#lineage-svg .lineage-node[data-sha="sha-current"]')
        page.locator('[data-lineage-current]').click()
        page.wait_for_function("document.querySelector('#cycle-details-panel .cycle-details-body h3')")
        assert node.evaluate('''node => {
            const graph = document.querySelector('[data-lineage-graph-scroll]').getBoundingClientRect();
            const panel = document.querySelector('#cycle-details-panel').getBoundingClientRect();
            const box = node.getBoundingClientRect();
            const inside = box.left >= graph.left && box.right <= graph.right && box.top >= graph.top && box.bottom <= graph.bottom;
            const clear = panel.bottom <= box.top || panel.top >= box.bottom || panel.right <= box.left || panel.left >= box.right;
            const hit = document.elementFromPoint((box.left + box.right) / 2, (box.top + box.bottom) / 2);
            return inside && clear && (hit === node || node.contains(hit));
        }''')
    finally:
        close()


def test_issue436_invalid_timestamp_list_node_explains_date_filter_exclusion():
    timestamp = 'not-a-timestamp'
    data = _fixture()
    data['evolution_tree'] = {'nodes': {}, 'current_sha': 'sha-untimed'}
    row = {'phase': 'evolution_tree', 'cycle_id': 'untimed', 'sha': 'sha-untimed', 'parent_sha': '', 'ts': timestamp}
    data['ledger_tail'] = [row]
    data['ledger_history'] = [row]
    page, close = _open(tv.render_pages(data, host='eeepc', generated_at='2026-01-02 01:00:00'), 390, 844)
    try:
        item = page.locator('[data-lineage-list-node="c:sha-untimed"]')
        assert 'time unavailable' in item.inner_text()
        assert 'outside selected date filter' in item.inner_text()
        page.locator('[data-lineage-filter="all"]').click()
        assert 'outside selected date filter' not in item.inner_text()
        page.locator('[data-lineage-filter="today"]').click()
        assert 'outside selected date filter' in item.inner_text()
    finally:
        close()


def test_issue436_missing_current_is_explicit_and_does_not_select_newest(tmp_path):
    pages = _pages(current='')
    page, close = _open(pages, 390, 844)
    try:
        assert page.locator('[data-lineage-current]').is_disabled()
        assert page.locator('[data-lineage-current]').get_attribute('title') == 'Current node unavailable.'
        assert page.locator('#lineage-svg .lineage-node.cycle-node-selected').count() == 0
    finally:
        close()


def test_issue436_unknown_deep_link_is_not_confused_with_missing_current(tmp_path):
    html = _pages()['lineage.html']
    details = json.loads(_pages()['lineage-cycle-details.json'])
    srv, base = _serve_lineage(html, details)
    try:
        pytest.importorskip('playwright')
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': 390, 'height': 844})
            page.goto(base + '/lineage.html#node-c%3Aunknown')
            page.wait_for_timeout(100)
            assert 'not found in loaded history' in page.locator('.lineage-filter-note').inner_text()
            assert page.locator('[data-lineage-current]').get_attribute('title') == 'Locate current node'
            browser.close()
    finally:
        srv.shutdown()
