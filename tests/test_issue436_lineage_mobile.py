"""Issue #436: current-node orientation and same-graph mobile cycle list."""
from __future__ import annotations

import json
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
        page.locator('[data-lineage-current]').click()
        page.wait_for_selector('.cycle-details-body h3')
        assert page.locator('#lineage-svg .lineage-node.cycle-node-selected').get_attribute('data-sha') == 'sha-current'
        assert page.locator('[data-lineage-cycle-list] [aria-pressed="true"]').get_attribute('data-lineage-list-node') == 'c:sha-current'
        assert 'Current useful result' in page.locator('.cycle-details-body').inner_text()
        page.locator('#cycle-details-close').click()
        page.locator('[data-lineage-list-open]').click()
        page.locator('[data-lineage-list-node="c:sha-old"]').click()
        assert not page.locator('.lineage-list-dialog').evaluate('(d) => d.open')
        assert page.locator('#lineage-svg .lineage-node.cycle-node-selected').get_attribute('data-sha') == 'sha-old'
        assert page.locator('[data-lineage-cycle-list] [aria-pressed="true"]').get_attribute('data-lineage-list-node') == 'c:sha-old'
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
