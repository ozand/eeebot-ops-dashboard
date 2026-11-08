from __future__ import annotations

import runpy
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest


def test_issue442_about_tables_anchors_and_mobile_reachability(tmp_path: Path) -> None:
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    fixture = runpy.run_path('tests/test_techtree_viewer.py')
    pages = fixture['tv'].render_pages(
        fixture['_fixture'](), host='synthetic fixture', generated_at='2026-11-08 12:00:00'
    )
    site = tmp_path / 'site'
    site.mkdir()
    for name in ('about.html', 'cycles.html', 'lineage.html', 'index.html'):
        (site / name).write_text(pages[name], encoding='utf-8')

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site), **kwargs)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for width, height in ((390, 844), (1440, 1000)):
                    page = browser.new_page(viewport={'width': width, 'height': height})
                    page.goto(f'http://127.0.0.1:{server.server_port}/about.html', wait_until='networkidle')
                    assert page.locator('.about-system-map li').count() == 4
                    assert page.locator('.about-toc').get_by_role('link', name='Repositories').is_visible()
                    assert page.locator('.about-term-links a').count() == 4
                    assert page.locator('.about-scroll-cue:visible').count() == 4
                    for cue in page.locator('.about-scroll-cue:visible').all():
                        assert cue.inner_text() == 'Scroll horizontally to see all columns.'
                        assert cue.evaluate('(e) => getComputedStyle(e).display !== "none"')
                    assert page.locator('.about-table-scroll[aria-describedby]').count() == 4
                    assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
                    if width == 390:
                        page.screenshot(path=str(tmp_path / 'about-mobile-tables.png'), full_page=False)
                    regions = page.locator('.about-table-scroll')
                    assert regions.count() == 4
                    for i in range(regions.count()):
                        region = regions.nth(i)
                        assert region.is_visible()
                        assert region.get_attribute('aria-label') in {
                            'Repository reference table', 'Cycle stages reference table',
                            'Schedule reference table', 'Model reference table',
                        }
                        assert page.locator('#' + region.get_attribute('aria-describedby')).is_visible()
                        assert region.evaluate('(e) => e.scrollWidth > e.clientWidth') == (width == 390)
                        region.focus()
                        assert region.evaluate('(e) => document.activeElement === e') is True
                        if width == 390:
                            page.keyboard.press('ArrowRight')
                            assert region.evaluate('(e) => e.scrollLeft > 0')
                            for _ in range(100):
                                if region.evaluate('(e) => e.scrollLeft + e.clientWidth >= e.scrollWidth'):
                                    break
                                page.keyboard.press('ArrowRight')
                            assert region.evaluate('(e) => e.scrollLeft + e.clientWidth >= e.scrollWidth')
                            assert region.locator('thead th').last.evaluate('(e) => e.getBoundingClientRect().right <= e.closest(".about-table-scroll").getBoundingClientRect().right')
                    page.goto(f'http://127.0.0.1:{server.server_port}/cycles.html', wait_until='networkidle')
                    integration_link = page.locator('.cycle-glossary-link[href="about.html#term-integration"]')
                    assert integration_link.is_visible()
                    integration_link.click()
                    assert page.url.endswith('/about.html#term-integration')
                    assert page.locator('#term-integration').is_visible()
                    page.goto(f'http://127.0.0.1:{server.server_port}/lineage.html', wait_until='networkidle')
                    inferred_link = page.locator('.lineage-legend-item a[href="about.html#term-inferred"]')
                    incomplete_link = page.locator('.lineage-legend-item a[href="about.html#term-incomplete"]')
                    assert inferred_link.is_visible() and incomplete_link.count() == 2
                    assert all(incomplete_link.nth(i).is_visible() for i in range(incomplete_link.count()))
                    inferred_link.click()
                    assert page.url.endswith('/about.html#term-inferred')
                    assert page.locator('#term-inferred').is_visible()
                    page.goto(f'http://127.0.0.1:{server.server_port}/index.html', wait_until='networkidle')
                    page.locator('.empire-stats-context summary').click()
                    heldout_link = page.locator('.empire-stat a[href="about.html#term-held-out"]')
                    assert heldout_link.count() == 1 and heldout_link.is_visible()
                    heldout_link.click()
                    assert page.url.endswith('/about.html#term-held-out')
                    assert page.locator('#term-held-out').is_visible()
                    page.goto(f'http://127.0.0.1:{server.server_port}/about.html', wait_until='networkidle')
                    page.get_by_role('link', name='integration', exact=True).click()
                    assert page.url.endswith('#term-integration')
                    assert page.locator('#term-integration').is_visible()
                    page.locator('.about-toc').get_by_role('link', name='Cycle').click()
                    assert page.url.endswith('#about-cycle')
                    page.locator('.about-toc a').first.focus()
                    page.keyboard.press('Tab')
                    assert page.evaluate('document.activeElement.tagName') == 'A'
                    page.screenshot(path=str(tmp_path / f'about-{width}.png'), full_page=True)
                    page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
