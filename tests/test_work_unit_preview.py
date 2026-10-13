import copy
from html.parser import HTMLParser
import json
import os
from pathlib import Path

import pytest

from scripts import work_unit_catalog as wc
from scripts import work_unit_observations as wo
from scripts import work_unit_preview as wp

PILOT = Path(__file__).resolve().parents[1] / 'docs/work-units/pilot.json'
NOW = '2026-10-12T18:00:00Z'


@pytest.fixture
def catalog():
    return wc.load_json(PILOT)


def snapshot(catalog):
    target = catalog['units'][0]['links'][0]['target']
    return {'schema':wo.SCHEMA, 'catalog_sha256':wo.public_digest(catalog),
            'records':{wc.target_id(target):{'checked_at':NOW, 'probe':'present',
                'last_success':{'observed_at':NOW, 'facts':{'state':'open','workflow_labels':[]}}}}}


def test_roles_and_uncertainty_appear_without_network(catalog, monkeypatch):
    monkeypatch.setattr(wo, 'github_get', lambda *_: pytest.fail('render must not fetch'))
    page = wp.render_html(catalog, at=NOW)
    for role in wc.ROLES:
        assert page.count(f'data-role="{role}"') == 3
    assert 'not_collected' in page
    assert 'Acceptance evidence: not connected' in page
    assert 'Deployment evidence: not connected' in page
    assert '<script' not in page
    assert 'No runtime source is connected' in page


def test_private_labels_ids_relations_and_content_absent_in_both_outputs(catalog):
    private = copy.deepcopy(catalog['units'][2])
    private['id'] = 'eeebot:area:private-canary'
    private['title'] = 'SECRET-PRIVATE-CANARY'
    private['visibility'] = 'private'
    private['related_units'] = []
    catalog['units'].append(private)
    catalog['units'][0]['related_units'].append(private['id'])
    catalog['units'][0]['links'][1]['visibility'] = 'private'
    catalog['units'][0]['links'][1]['label'] = 'SECRET-LINK-CANARY'
    index = wp.build_index(catalog, at=NOW)
    encoded = json.dumps(index) + wp.render_html(catalog, at=NOW)
    assert 'SECRET-' not in encoded
    assert 'private-canary' not in encoded


def test_markup_is_escaped_and_anchor_targets_exist(catalog):
    catalog['units'][0]['title'] = '<script>alert(1)</script>'
    page = wp.render_html(catalog, at=NOW)
    assert '<script>' not in page
    assert '&lt;script&gt;' in page
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids, self.links = set(), []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if 'id' in attrs:
                self.ids.add(attrs['id'])
            if tag == 'a' and attrs.get('href','').startswith('#'):
                self.links.append(attrs['href'][1:])
    parser = Parser()
    parser.feed(page)
    assert set(parser.links) <= parser.ids


def test_failed_latest_probe_does_not_hide_old_observation(catalog):
    saved = snapshot(catalog)
    record = next(iter(saved['records'].values()))
    record['probe'] = 'unavailable'
    record['checked_at'] = '2026-10-12T18:20:00Z'
    page = wp.render_html(catalog, saved, at='2026-10-12T18:20:00Z')
    assert 'unavailable' in page and 'stale' in page
    assert 'Last successful observation: '+NOW in page
    assert 'Checked: 2026-10-12T18:20:00Z' in page


def test_semantic_change_not_clock_only_change_invalidates_comparison_digest(catalog):
    saved = snapshot(catalog)
    first = wp.build_index(catalog, saved, at=NOW)
    second = wp.build_index(catalog, saved, at='2026-10-12T18:01:00Z')
    assert wp.semantic_digest(first) == wp.semantic_digest(second)
    changed = copy.deepcopy(saved)
    next(iter(changed['records'].values()))['last_success']['facts']['state'] = 'closed'
    assert wp.semantic_digest(wp.build_index(catalog, changed, at=NOW)) != wp.semantic_digest(first)
    stale = wp.build_index(catalog, saved, at='2026-10-12T18:15:00Z')
    assert wp.semantic_digest(first) != wp.semantic_digest(stale)


def test_cli_does_not_overwrite_and_invalid_snapshot_fails_closed(catalog, tmp_path):
    out = tmp_path / 'preview'
    assert wp.main([str(PILOT), '--at', NOW, '--out', str(out)]) == 0
    assert (out/'index.html').exists() and (out/'index.json').exists()
    assert wp.main([str(PILOT), '--out', str(out)]) == 1
    invalid = tmp_path / 'bad.json'
    invalid.write_text('{}')
    assert wp.main([str(PILOT), '--snapshot', str(invalid), '--out', str(tmp_path/'other')]) == 1
    assert not (tmp_path/'other').exists()


@pytest.mark.parametrize('width', [320,1280])
def test_browser_layout_roles_and_keyboard_index(catalog, width):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if os.environ.get('CI'):
            pytest.fail('browser coverage requires Playwright in CI')
        pytest.skip('Playwright not installed locally')
    with sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            if os.environ.get('CI'):
                pytest.fail('browser coverage requires Chromium in CI')
            pytest.skip('Chromium binary not installed locally')
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={'width':width,'height':900})
            page.set_content(wp.render_html(catalog, at=NOW))
            assert page.locator('article.unit').count() == 3
            assert page.locator('[data-role]').count() == 9
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.locator('nav a').first.focus()
            page.keyboard.press('Enter')
            assert page.evaluate('location.hash').startswith('#unit-')
            assert page.locator('article.unit').first.locator('h2').is_visible()
        finally:
            browser.close()
