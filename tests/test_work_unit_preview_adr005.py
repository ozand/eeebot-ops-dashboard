"""ADR-005 C07/C08/C10 through the existing offline consumer, no browser run."""
import copy
import json
from pathlib import Path

import pytest

from scripts import work_unit_catalog as wc
from scripts import work_unit_observations as wo
from scripts import work_unit_preview as wp

PILOT = Path(__file__).resolve().parents[1] / 'docs/work-units/pilot.json'
AT = '2026-10-12T18:20:00Z'


@pytest.fixture
def catalog():
    return wc.load_json(PILOT)


def saved(catalog):
    target = next(link['target'] for unit in catalog['units'] for link in unit['links']
                  if link['target']['kind'] == 'issue')
    identity = wc.target_id(target)
    return {'schema': wo.SCHEMA, 'catalog_sha256': wo.public_digest(catalog),
            'records': {identity: {'checked_at': AT, 'probe': 'unavailable',
                'last_success': {'observed_at': '2026-10-12T18:00:00Z',
                                 'facts': {'state': 'open', 'workflow_labels': []}}}}}


def forbidden(*_args, **_kwargs):
    pytest.fail('offline consumer must not use transport')


@pytest.mark.parametrize('case,code', [('catalog-version', 'unsupported_schema'),
    ('snapshot-version', 'unsupported_snapshot'), ('public-edit', 'catalog_mismatch')])
def test_adr005_consumer_rejects_incompatible_inputs_without_output(catalog, tmp_path, monkeypatch, case, code):
    snapshot = saved(catalog)
    if case == 'catalog-version':
        catalog['schema'] = 'work-units/v2'
    elif case == 'snapshot-version':
        snapshot['schema'] = 'work-unit-observations/v2'
    else:
        catalog['units'][0]['goal'] += ' revised'
    monkeypatch.setattr(wo, 'build_opener', forbidden)
    with pytest.raises(wc.CatalogError, match=f'^{code}$'):
        wp.build_index(catalog, snapshot, at=AT)
    with pytest.raises(wc.CatalogError, match=f'^{code}$'):
        wp.render_html(catalog, snapshot, at=AT)
    config, cache, output = tmp_path / 'catalog.json', tmp_path / 'snapshot.json', tmp_path / 'out'
    config.write_text(json.dumps(catalog), encoding='utf-8')
    cache.write_text(json.dumps(snapshot), encoding='utf-8')
    originals = (config.read_bytes(), cache.read_bytes())
    assert wp.main([str(config), '--snapshot', str(cache), '--at', AT, '--out', str(output)]) == 1
    assert not output.exists()
    assert (config.read_bytes(), cache.read_bytes()) == originals


@pytest.mark.parametrize('future_field', ['checked_at', 'observed_at'],
                         ids=['future-check-only', 'future-success-only'])
def test_adr005_consumer_preserves_split_clock_skew(catalog, tmp_path, monkeypatch, future_field):
    snapshot = saved(catalog)
    record = next(iter(snapshot['records'].values()))
    if future_field == 'checked_at':
        record['checked_at'] = '2026-10-12T18:20:01Z'
    else:
        record['last_success']['observed_at'] = '2026-10-12T18:20:01Z'
    original = copy.deepcopy(snapshot)
    path = tmp_path / 'clock.json'
    wo.write_snapshot(path, snapshot)
    reread = wc.load_json(path)
    monkeypatch.setattr(wo, 'build_opener', forbidden)
    index = wp.build_index(catalog, reread, at=AT)
    observed_links = [link for unit in index['units'] for link in unit['links']
                      if link['observation']['last_success'] is not None]
    assert observed_links
    for link in observed_links:
        observation = link['observation']
        assert observation['freshness'] == 'clock_skew'
        assert observation['probe'] == 'unavailable'
        assert observation['checked_at'] == record['checked_at']
        assert observation['last_success'] == record['last_success']
    assert all(unit['acceptance_evidence'] == 'not_connected' and
               unit['deployment_evidence'] == 'not_connected' for unit in index['units'])
    page = wp.render_html(catalog, reread, at=AT)
    assert 'clock_skew' in page and 'unavailable' in page
    assert 'Checked: ' + record['checked_at'] in page
    assert 'Last successful observation: ' + record['last_success']['observed_at'] in page
    assert reread == snapshot == original
