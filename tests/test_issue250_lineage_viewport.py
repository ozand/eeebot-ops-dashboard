"""#250: lineage.html fits the viewport, scrolls inside the graph, and zooms.

The operator's complaint was that the page itself scrolls: the server SVG is
published at its natural size (3158 x 19412 on the live snapshot), so the
document grows to the height of the whole tree. These tests pin the three
properties that were asked for, and each one is written so that it fails when
its own half of the change is removed:

  * the document does not scroll while the graph still overflows -- both
    halves are asserted, so an empty graph cannot make the test pass;
  * the scrolling happens in .lineage-graph-scroll, on both axes;
  * + / - / Fit to window change the rendered box and leave the viewBox --
    the layout box -- alone.
"""
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
from test_techtree_viewer import _fixture  # noqa: E402

NOW = '2026-01-02 01:00:00'
NOW_ISO = '2026-01-02T01:00:00Z'


def _deep_chain_pages(depth: int = 40) -> dict[str, str]:
    """A chain deeper than any viewport: height grows 54px per generation."""
    rows = []
    for i in range(depth):
        rows.append({
            'phase': 'evolution_tree',
            'cycle_id': f'cycle-{i}',
            'sha': f'sha-{i}',
            'parent_sha': f'sha-{i - 1}' if i else '',
            'ts': f'2026-01-02T00:{i % 60:02d}:{i % 60:02d}Z',
        })
    data = _fixture()
    data['evolution_tree'] = {'nodes': {}, 'current_sha': f'sha-{depth - 1}'}
    data['ledger_tail'] = rows
    data['ledger_history'] = rows
    return tv.render_pages(data, host='eeepc', generated_at=NOW)


def _open(pages: dict[str, str], width: int = 1280, height: int = 800):
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    srv, base_url = _serve_lineage(pages['lineage.html'], json.loads(pages['lineage-cycle-details.json']))
    ctx = sync_playwright().start()
    browser = ctx.chromium.launch()
    page = browser.new_page(viewport={'width': width, 'height': height})
    page.clock.install(time=NOW_ISO)
    page.goto(base_url + '/lineage.html')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(150)

    def close():
        browser.close()
        ctx.stop()
        srv.shutdown()

    return page, close


# --- generator half ---------------------------------------------------------


def test_only_the_lineage_page_gets_the_viewport_body_class() -> None:
    pages = tv.render_pages(_fixture(), host='eeepc', generated_at=NOW)
    assert '<body class="page-lineage">' in pages['lineage.html']
    # The stylesheet is shared, so the rules are present everywhere; what must
    # be exclusive is the class on <body> that switches them on.
    for name, html in pages.items():
        if not name.endswith('.html') or name == 'lineage.html':
            continue
        assert '<body>' in html or '<body class=""' in html, f'{name}: unexpected body tag'
        assert '<body class="page-lineage">' not in html, (
            f'{name} must keep the document-shaped chrome')


def test_zoom_controls_are_published_on_the_lineage_page() -> None:
    pages = tv.render_pages(_fixture(), host='eeepc', generated_at=NOW)
    html = pages['lineage.html']
    for action in ('in', 'out', 'fit', 'reset'):
        assert f'data-lineage-zoom="{action}"' in html, f'missing zoom control: {action}'
    assert 'data-lineage-zoom-level' in html


# --- browser half -----------------------------------------------------------


@pytest.mark.parametrize('viewport', [(1280, 800), (390, 844)])
def test_document_does_not_scroll_while_the_graph_does(viewport) -> None:
    pages = _deep_chain_pages()
    page, close = _open(pages, *viewport)
    try:
        m = page.evaluate("""() => {
            const doc = document.documentElement;
            const g = document.querySelector('[data-lineage-graph-scroll]');
            return {
                docOverflowY: doc.scrollHeight - doc.clientHeight,
                docOverflowX: doc.scrollWidth - doc.clientWidth,
                bodyOverflowY: document.body.scrollHeight - document.body.clientHeight,
                graphOverflowY: g.scrollHeight - g.clientHeight,
                graphClientH: g.clientHeight,
                overflowY: getComputedStyle(g).overflowY,
            };
        }""")
    finally:
        close()
    # The point of the change: the tree is taller than the box that holds it,
    # and that box -- not the page -- is what scrolls.
    assert m['graphOverflowY'] > 0, f'graph must overflow, otherwise this proves nothing: {m}'
    assert m['overflowY'] in ('auto', 'scroll'), m
    assert m['docOverflowY'] <= 1, f'the page itself must not scroll vertically: {m}'
    assert m['docOverflowX'] <= 1, f'the page itself must not scroll horizontally: {m}'
    assert m['bodyOverflowY'] <= 1, m
    assert m['graphClientH'] > 100, f'the graph box must keep usable height: {m}'


def test_details_panel_floats_over_the_graph_without_reintroducing_page_scroll() -> None:
    """The panel used to sit in the document flow below the graph.

    With the graph pinned to the viewport there is no 'below', so on this page
    the panel overlays it -- and opening one must not start the page scrolling
    again, nor push the graph box out of the viewport.
    """
    pages = _deep_chain_pages()
    page, close = _open(pages)
    try:
        page.evaluate(
            "() => document.querySelector('.lineage-node')"
            ".dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))"
        )
        page.wait_for_selector('.cycle-details-body h3', timeout=4000)
        m = page.evaluate("""() => {
            const doc = document.documentElement;
            const panel = document.getElementById('cycle-details-panel');
            const g = document.querySelector('[data-lineage-graph-scroll]');
            const pr = panel.getBoundingClientRect();
            const gr = g.getBoundingClientRect();
            return {
                docOverflowY: doc.scrollHeight - doc.clientHeight,
                hidden: panel.hidden,
                position: getComputedStyle(panel).position,
                panelBottom: pr.bottom, panelTop: pr.top,
                graphBottom: gr.bottom,
                viewportHeight: window.innerHeight,
            };
        }""")
    finally:
        close()

    assert not m['hidden'], m
    assert m['position'] == 'fixed', m
    assert m['panelTop'] >= 0 and m['panelBottom'] <= m['viewportHeight'] + 1, m
    assert m['graphBottom'] <= m['viewportHeight'] + 1, m
    assert m['docOverflowY'] <= 1, f'opening the panel must not restart page scroll: {m}'


def test_zoom_in_and_out_rescale_the_rendered_box_only() -> None:
    pages = _deep_chain_pages()
    page, close = _open(pages)
    try:
        read = (
            "() => { const s = document.getElementById('lineage-svg');"
            " return {w: +s.getAttribute('width'), h: +s.getAttribute('height'),"
            " box: s.getAttribute('viewBox'),"
            " label: document.querySelector('[data-lineage-zoom-level]').textContent}; }"
        )
        page.click('[data-lineage-filter="all"]')
        before = page.evaluate(read)
        page.click('[data-lineage-zoom="in"]')
        zoomed_in = page.evaluate(read)
        page.click('[data-lineage-zoom="out"]')
        page.click('[data-lineage-zoom="out"]')
        zoomed_out = page.evaluate(read)
        page.click('[data-lineage-zoom="reset"]')
        reset = page.evaluate(read)
    finally:
        close()

    assert before['label'] == '100%', before
    assert zoomed_in['w'] > before['w'] and zoomed_in['h'] > before['h'], (before, zoomed_in)
    assert zoomed_out['w'] < before['w'] and zoomed_out['h'] < before['h'], (before, zoomed_out)
    # The viewBox is the layout box; zoom must never redraw the geometry.
    assert zoomed_in['box'] == before['box'] == zoomed_out['box'], (before, zoomed_in, zoomed_out)
    assert reset['w'] == before['w'] and reset['label'] == '100%', (before, reset)


def test_fit_to_window_brings_the_whole_graph_into_the_visible_area() -> None:
    pages = _deep_chain_pages()
    page, close = _open(pages)
    try:
        page.click('[data-lineage-filter="all"]')
        before = page.evaluate("""() => {
            const g = document.querySelector('[data-lineage-graph-scroll]');
            return {overflowY: g.scrollHeight - g.clientHeight};
        }""")
        page.click('[data-lineage-zoom="fit"]')
        after = page.evaluate("""() => {
            const g = document.querySelector('[data-lineage-graph-scroll]');
            const s = document.getElementById('lineage-svg');
            return {
                svgW: +s.getAttribute('width'), svgH: +s.getAttribute('height'),
                clientW: g.clientWidth, clientH: g.clientHeight,
                overflowY: g.scrollHeight - g.clientHeight,
                overflowX: g.scrollWidth - g.clientWidth,
                zoom: window.lineageRenderer.getZoom(),
            };
        }""")
    finally:
        close()

    assert before['overflowY'] > 0, f'nothing to fit, the test would be vacuous: {before}'
    assert after['svgH'] <= after['clientH'], after
    assert after['svgW'] <= after['clientW'], after
    assert after['overflowY'] <= 1 and after['overflowX'] <= 1, after
    assert 0 < after['zoom'] < 1, after


def test_fit_to_window_fits_a_live_scale_graph() -> None:
    """The published history is the case that broke the first zoom floor.

    The live payload carries the full 1500-node budget and lays out at
    48316 x 21831; against a 1402 x 612 graph box that needs a scale of 0.029,
    below the 0.05 floor the control originally clamped to -- so 'Fit to
    window' still overflowed. This fixture reproduces that width with 1400
    roots and asserts the graph really lands inside the box.
    """
    roots = 1400
    rows = [{'phase': 'evolution_tree', 'cycle_id': f'cycle-{i}', 'sha': f'sha-{i}',
             'parent_sha': '', 'ts': f'2026-01-02T00:{i % 60:02d}:{i % 60:02d}Z'}
            for i in range(roots)]
    data = _fixture()
    data['evolution_tree'] = {'nodes': {}, 'current_sha': f'sha-{roots - 1}'}
    data['ledger_tail'] = rows
    data['ledger_history'] = rows
    pages = tv.render_pages(data, host='eeepc', generated_at=NOW)

    page, close = _open(pages, 1440, 900)
    try:
        page.click('[data-lineage-filter="all"]')
        page.wait_for_timeout(400)
        wide = page.evaluate("""() => {
            const g = document.querySelector('[data-lineage-graph-scroll]');
            const s = document.getElementById('lineage-svg');
            return {svgW: +s.getAttribute('width'), clientW: g.clientWidth,
                    overflowX: g.scrollWidth - g.clientWidth};
        }""")
        page.click('[data-lineage-zoom="fit"]')
        page.wait_for_timeout(200)
        fitted = page.evaluate("""() => {
            const g = document.querySelector('[data-lineage-graph-scroll]');
            const s = document.getElementById('lineage-svg');
            return {svgW: +s.getAttribute('width'), svgH: +s.getAttribute('height'),
                    clientW: g.clientWidth, clientH: g.clientHeight,
                    overflowX: g.scrollWidth - g.clientWidth,
                    overflowY: g.scrollHeight - g.clientHeight,
                    zoom: window.lineageRenderer.getZoom(),
                    label: document.querySelector('[data-lineage-zoom-level]').textContent};
        }""")
    finally:
        close()

    # A graph this wide needs a scale well under the old 0.05 floor.
    assert wide['overflowX'] > 20000, f'fixture is not live-scale: {wide}'
    assert fitted['zoom'] < 0.05, f'this case only bites below the old floor: {fitted}'
    assert fitted['svgW'] <= fitted['clientW'], fitted
    assert fitted['svgH'] <= fitted['clientH'], fitted
    assert fitted['overflowX'] <= 1 and fitted['overflowY'] <= 1, fitted
    # Whole percent would render every such fit as '0%'.
    assert fitted['label'] != '0%', fitted
