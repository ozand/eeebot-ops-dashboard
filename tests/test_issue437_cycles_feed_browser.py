from __future__ import annotations

import runpy
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import sleep

import pytest


def test_issue437_search_filter_coverage_and_incremental_archive(tmp_path: Path) -> None:
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright

    fixture = runpy.run_path('tests/test_techtree_viewer.py')
    tv = fixture['tv']
    data = fixture['_fixture']()
    data['ledger_tail'] = [
        row
        for i in reversed(range(260))
        for row in (
            {'phase': 'started', 'cycle_id': f'cycle-{i:03d}', 'task_title': f'Task {i:03d}',
             'ts': f'2026-09-{(i % 28) + 1:02d}T00:00:00Z'},
            {'phase': 'outcome', 'cycle_id': f'cycle-{i:03d}',
             'status': 'fail' if i % 7 == 0 else 'success',
             'delta': 0.25 if i == 251 else None,
             'ts': f'2026-09-{(i % 28) + 1:02d}T00:01:00Z'},
            *([{'phase': 'planning_session', 'cycle_id': f'cycle-{i:03d}', 'iterations_used': None,
                'plan': 'PRIVATE_PLAN_SHOULD_NOT_RENDER'}] if i == 251 else []),
        )
    ]
    pages = tv.render_pages(data, host='synthetic fixture', generated_at='2026-09-30 12:00:00')
    empty_html = tv.build_cycle_feed([{'phase': 'outcome', 'cycle_id': 'empty-day', 'status': 'success', 'ts': '2026-09-01T00:00:00Z'}], history_mode=True, archive_out=[])
    assert 'No older archive chunks are available.' in empty_html
    assert 'No matches in the loaded cycle window.' in empty_html
    proposal_html = tv.build_cycle_feed([{'phase': 'proposed', 'cycle_id': 'proposal-only', 'task_title': 'Proposed only'}], history_mode=True, archive_out=[])
    assert 'data-outcome="proposed"' in proposal_html
    assert 'PROPOSED · execution not observed' in proposal_html
    assert 'data-outcome="running"' not in proposal_html
    root = tmp_path / 'site'
    root.mkdir()
    for name in ('cycles.html', tv.CYCLES_ARCHIVE_INDEX_FILE, *[n for n in pages if n.startswith('cycles-archive-')]):
        (root / name).write_text(pages[name], encoding='utf-8')

    class Handler(SimpleHTTPRequestHandler):
        chunk_requests = 0
        archive_requests: list[str] = []
        served_chunks: list[str] = []

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self):
            if 'cycles-archive-' in self.path and self.path.endswith('.json'):
                Handler.archive_requests.append(self.path)
            if self.path.endswith('cycles-archive-2.json') and Handler.chunk_requests == 0:
                Handler.chunk_requests += 1
                sleep(0.1)
                self.send_error(503, 'synthetic first-attempt archive failure')
                return
            if self.path.endswith('cycles-archive-2.json'):
                Handler.chunk_requests += 1
            if self.path.rsplit('-', 1)[-1].removesuffix('.json').isdigit():
                Handler.served_chunks.append(self.path)
            super().do_GET()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': 390, 'height': 844})
            try:
                page.goto(f'http://127.0.0.1:{server.server_port}/cycles.html#f-integrated', wait_until='networkidle')
                assert page.locator('.filter-btn[data-filter="integrated"]').get_attribute('aria-pressed') == 'true'
                search = page.get_by_label('Search task or cycle ID')
                search.fill('Task 251')
                assert page.locator('[data-feed-coverage]').inner_text().startswith('1 matching of 50 loaded cycles for integrated')
                assert page.url.endswith('?q=Task+251#f-integrated')
                page.locator('#cycle-from').fill('2026-09-01')
                page.locator('#cycle-to').fill('2026-09-28')
                assert 'from=2026-09-01' in page.url and 'to=2026-09-28' in page.url
                assert page.locator('[data-feed-coverage]').inner_text().startswith('1 matching of 50 loaded cycles for integrated')
                assert 'Dates use recorded timestamp day' in page.locator('[data-feed-coverage]').inner_text()
                assert 'PRIVATE_PLAN_SHOULD_NOT_RENDER' not in page.content()
                assert '0.25' in page.locator('#cycle-cycle-251 .feed-card-summary').inner_text()
                assert 'PRIVATE_PLAN_SHOULD_NOT_RENDER' not in pages['cycles.html']
                search.press('Tab')
                assert page.locator('.feed-card-summary').first.evaluate('(e)=>e.getBoundingClientRect().height >= 44')
                button = page.locator('.feed-show-all')
                button.focus()
                page.keyboard.press('Enter')
                page.wait_for_function("document.querySelector('.feed-show-all').textContent === 'Loading older cycles…'")
                page.wait_for_function("document.querySelector('.feed-archive-status').textContent.includes('Archive chunk unavailable')")
                assert button.is_enabled() and button.inner_text() == 'Retry loading older cycles'
                button.click()
                page.wait_for_function("document.querySelectorAll('#panel-feed li[data-outcome]').length === 60")
                assert Handler.chunk_requests == 2
                assert Handler.archive_requests == ['/cycles-archive-index.json', '/cycles-archive-2.json', '/cycles-archive-2.json']
                assert 'search covers loaded cycles only.' in page.locator('[data-feed-coverage]').inner_text()
                assert page.locator('[data-feed-coverage]').inner_text().startswith('1 matching of 60 loaded cycles for integrated')
                assert page.locator('#cycle-cycle-251').is_visible()
                assert 'Task 251' in page.locator('#cycle-cycle-251').inner_text()
                assert page.locator('#cycle-cycle-251').get_attribute('data-day') == '2026-09-28'
                assert page.locator('.feed-card[open]').count() == 0
                search.fill('Task 150')
                assert page.locator('[data-feed-coverage]').inner_text().startswith('0 matching of 60 loaded cycles')
                button = page.locator('.feed-show-all')
                button.focus()
                page.keyboard.press('Enter')
                page.wait_for_function("document.querySelectorAll('#panel-feed li[data-outcome]').length === 160")
                assert Handler.archive_requests[-1] == '/cycles-archive-1.json'
                assert page.locator('.feed-day-header:not([hidden])').count() == 1
                assert Handler.served_chunks == ['/cycles-archive-2.json', '/cycles-archive-1.json']
                assert page.locator('[data-feed-coverage]').inner_text().startswith('1 matching of 160 loaded cycles for integrated')
                assert page.locator('#cycle-cycle-150').is_visible()
                assert page.locator('#cycle-cycle-150').get_attribute('aria-hidden') == 'false'
                assert page.locator('#panel-feed li[data-outcome][aria-hidden="false"]').count() == 1
                assert page.locator('.feed-day-header:not([hidden])').count() == 1
                assert page.url.endswith('?q=Task+150&from=2026-09-01&to=2026-09-28#f-integrated')
                assert page.locator('#panel-feed .feed-list').evaluate('(e)=>e.scrollWidth<=e.clientWidth')
                assert page.locator('#cycle-search').evaluate('(e)=>e.getBoundingClientRect().height>=44')
                page.screenshot(path=str(tmp_path / 'cycles-390.png'), full_page=True)
                page.screenshot(path=str(tmp_path / 'cycles-390-first.png'), full_page=False)
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.screenshot(path=str(tmp_path / 'cycles-1440.png'), full_page=True)
                page.screenshot(path=str(tmp_path / 'cycles-1440-first.png'), full_page=False)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.reload(wait_until='networkidle')
                assert page.locator('.filter-btn[data-filter="integrated"]').get_attribute('aria-pressed') == 'true'
                assert page.get_by_label('Search task or cycle ID').input_value() == 'Task 150'
                assert page.locator('#cycle-from').input_value() == '2026-09-01'
                assert page.locator('#cycle-to').input_value() == '2026-09-28'
                search = page.get_by_label('Search task or cycle ID')
                search.fill('no such cycle')
                assert page.locator('[data-feed-coverage]').inner_text().startswith('0 matching of 50 loaded cycles')
                assert 'No matches in the loaded cycle window.' in page.locator('[data-filter-empty]').inner_text()
                page.locator('#cycle-to').fill('2026-09-01')
                assert page.locator('[data-feed-coverage]').inner_text().startswith('0 matching of 50 loaded cycles')
                assert 'from=2026-09-01' in page.url and 'to=2026-09-01' in page.url
                page.get_by_role('button', name='Clear search').click()
                assert page.locator('[data-feed-coverage]').inner_text().startswith('50 matching of 50 loaded cycles for integrated')
                summary = page.locator('.feed-card-summary').first
                summary.focus()
                page.keyboard.press('Enter')
                assert page.locator('.feed-card[open]').count() == 1
                assert page.locator('.feed-card-evidence').first.is_visible()
                assert 'LLM metrics unavailable' in page.locator('.feed-card-evidence').first.inner_text()
                details_link = page.locator('.feed-card[open] .cycle-detail-link')
                assert details_link.get_attribute('href') == 'cycle.html?id=cycle-251'
                failed_filter = page.locator('.filter-btn[data-filter="failed"]')
                failed_filter.focus()
                page.keyboard.press('Enter')
                assert failed_filter.get_attribute('aria-pressed') == 'true'
                assert page.url.endswith('#f-failed')
                assert 'matching of 50 loaded cycles for failed' in page.locator('[data-feed-coverage]').inner_text()
                assert page.locator('#panel-feed .feed-list').evaluate('(e)=>e.scrollWidth<=e.clientWidth')
                assert page.locator('#cycle-search').evaluate('(e)=>e.getBoundingClientRect().height>=44')
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
