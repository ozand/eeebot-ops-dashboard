from __future__ import annotations

import runpy
from pathlib import Path
from threading import Thread
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest


def test_issue434_shared_shell_routes_responsive_controls_and_evidence(tmp_path: Path) -> None:
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    fixture = runpy.run_path('tests/test_techtree_viewer.py')
    pages = fixture['_site']()
    fixture_data = fixture['_fixture']()
    fixture_data['token_heatmap'] = {
        'dates': ['2026-09-05'], 'hourly': {'2026-09-05': [[1, 2, 3, 1, 'bridge'] for _ in range(24)]},
        'five_min': {'2026-09-05': {'0': [1, 2, 3, 1, 'bridge']}},
        'summary': {'total_tokens': 72, 'vendor_tokens': 48, 'self_hosted_tokens': 24, 'total_calls': 24,
                    'days_span': 1, 'days_present': 1, 'days_missing': 0,
                    'quantiles_hourly': {'gateway': [1, 2, 3, 4], 'local': [1, 2, 3, 4], 'total': [1, 2, 3, 4]},
                    'quantiles_5min': {'gateway': [1, 2, 3, 4], 'local': [1, 2, 3, 4], 'total': [1, 2, 3, 4]}},
    }
    pages = fixture['tv'].render_pages(fixture_data, host='fixture', generated_at='2026-08-18 12:00:00')
    route_names = [name for name, _label in fixture['tv'].SITE_PAGES] + ['cycle.html']
    from scripts import techtree_viewer as viewer
    css = viewer.CSS
    assert css.count('grid-template-columns: repeat(2, minmax(0, 1fr))') == 1
    site_root = (tmp_path / 'site').resolve()
    site_root.mkdir(parents=True, exist_ok=True)
    for name in route_names:
        (site_root / name).write_text(pages[name], encoding='utf-8')
    (site_root / 'lineage-cycle-details.json').write_text(pages['lineage-cycle-details.json'], encoding='utf-8')

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site_root), **kwargs)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    evidence = (tmp_path / 'evidence').resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for width in (390, 1440):
                    for name in route_names:
                        page = browser.new_page(viewport={'width': width, 'height': 900}, device_scale_factor=1)
                        page.goto(f'http://127.0.0.1:{server.server_port}/{name}', wait_until='networkidle')
                        nav = page.locator('nav.site-nav')
                        assert nav.locator('[aria-current="page"]').count() == 1, (name, width)
                        assert page.locator('.empire-fresh').is_visible(), (name, width)
                        if name == 'index.html':
                            assert page.locator('.empire-stats-context').count() == 1, (name, width)
                            summary = page.locator('.empire-stats-context summary')
                            assert summary.is_visible() and summary.evaluate('(e) => e.getBoundingClientRect().height >= 44'), (name, width)
                            assert page.locator('.empire-stats-context[open]').count() == 0, (name, width)
                            assert page.locator('.empire-stats-context').evaluate('(e) => e.previousElementSibling?.id === "panel-now"'), (name, width, 'scorecard disclosure must follow the outer Now panel')
                            now_box = page.locator('#panel-now').bounding_box()
                            metrics_box = page.locator('.empire-stats-context').bounding_box()
                            assert now_box and metrics_box and metrics_box['y'] >= now_box['y'] + now_box['height'], (name, width, now_box, metrics_box)
                            summary.focus()
                            page.keyboard.press('Enter')
                            metrics = page.locator('.empire-stats-context .empire-stat')
                            assert page.locator('.empire-stats-context[open]').count() == 1
                            assert metrics.count() >= 1 and all(metrics.nth(i).is_visible() for i in range(metrics.count())), (name, width)
                            columns = page.locator('.empire-stats').evaluate('(e) => getComputedStyle(e).gridTemplateColumns.split(" ").length')
                            assert columns == 2, (name, width, columns)
                            assert all(metrics.nth(i).evaluate('(e) => e.getBoundingClientRect().right <= innerWidth') for i in range(metrics.count())), (name, width)
                            page.keyboard.press('Enter')
                        assert page.locator('.empire-strip').evaluate('(e) => e.scrollWidth <= e.clientWidth'), (name, width)
                        assert nav.evaluate('(e) => e.scrollWidth <= e.clientWidth'), (name, width)
                        overflow = page.evaluate('''() => {
                            const root = document.documentElement;
                            if (root.scrollWidth <= window.innerWidth) return null;
                            const describe = (element) => {
                                const rect = element.getBoundingClientRect();
                                const style = getComputedStyle(element);
                                return {
                                    tag: element.tagName,
                                    className: typeof element.className === 'string' ? element.className : '',
                                    text: (element.innerText || '').trim().slice(0, 80),
                                    x: Math.round(rect.x), right: Math.round(rect.right), width: Math.round(rect.width),
                                    scrollWidth: element.scrollWidth, clientWidth: element.clientWidth,
                                    minWidth: style.minWidth, widthStyle: style.width,
                                    whiteSpace: style.whiteSpace, overflowWrap: style.overflowWrap,
                                    font: style.fontFamily, fontSize: style.fontSize,
                                    beforeContent: getComputedStyle(element, '::before').content,
                                    afterContent: getComputedStyle(element, '::after').content,
                                    children: [...element.children].map((child) => ({tag: child.tagName, className: typeof child.className === 'string' ? child.className : '', scrollWidth: child.scrollWidth, clientWidth: child.clientWidth, text: (child.innerText || '').trim().slice(0, 60)})),
                                };
                            };
                            const overflowing = [...document.querySelectorAll('body *')].filter((element) => {
                                const rect = element.getBoundingClientRect();
                                return rect.right > window.innerWidth + 1 || element.scrollWidth > element.clientWidth + 1;
                            }).map(describe).slice(0, 20);
                            return {
                                viewport: window.innerWidth,
                                document: {scrollWidth: root.scrollWidth, clientWidth: root.clientWidth, bodyScrollWidth: document.body.scrollWidth, bodyClientWidth: document.body.clientWidth},
                                elements: overflowing,
                            };
                        }''')
                        assert overflow is None, (name, width, f'route content overflows viewport: {overflow!r}')
                        for link in nav.locator('a').all():
                            box = link.bounding_box()
                            assert box and box['height'] >= 44, (name, width, box)
                        page.locator('nav.site-nav a').first.focus()
                        assert page.evaluate('getComputedStyle(document.activeElement).outlineStyle') != 'none', (name, width)
                        page.keyboard.press('Tab')
                        focused = page.evaluate('''() => ({tag: document.activeElement.tagName, outline: getComputedStyle(document.activeElement).outlineStyle})''')
                        assert focused['tag'] == 'A' and focused['outline'] != 'none', (name, width, focused)
                        if name in ('cycles.html', 'tokens.html', 'lineage.html'):
                            buttons = page.locator('.filter-btn, .tkn-btn, .lineage-day-controls button, .lineage-zoom-controls button')
                            for button in buttons.all():
                                box = button.bounding_box()
                                assert box and box['width'] >= 44 and box['height'] >= 44, (name, width, box)
                        if name == 'lineage.html' and width == 390:
                            graph = page.locator('.lineage-graph-scroll')
                            assert graph.evaluate('(e) => e.clientHeight >= 40'), (name, width)
                        if name == 'cycles.html':
                            selected = page.locator('.filter-btn[data-filter="failed"]')
                            selected.focus()
                            assert page.evaluate('getComputedStyle(document.activeElement).outlineStyle') != 'none'
                            page.keyboard.press('Enter')
                            assert selected.get_attribute('aria-pressed') == 'true'
                        if name == 'tokens.html':
                            selected = page.locator('.tkn-btn[data-view="total"]')
                            selected.focus()
                            assert page.evaluate('getComputedStyle(document.activeElement).outlineStyle') != 'none'
                            page.keyboard.press('Enter')
                            assert selected.get_attribute('aria-pressed') == 'true'
                        page.screenshot(path=str(evidence / f'{name[:-5]}-{width}.png'), full_page=True)
                        page.close()
                assert page.url.endswith('/cycle.html')
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
