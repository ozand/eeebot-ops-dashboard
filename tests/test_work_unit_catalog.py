"""The catalog is descriptive, bounded and separate from live task state."""
import copy
import json
from pathlib import Path

import pytest

from scripts import work_unit_catalog as wc

PILOT = Path(__file__).resolve().parents[1] / 'docs/work-units/pilot.json'


@pytest.fixture
def catalog():
    return wc.load_json(PILOT)


def test_pilot_has_three_units_and_reuses_canonical_references(catalog):
    assert wc.validate_catalog(catalog) is catalog
    assert len(catalog['units']) == 3
    a = catalog['units'][0]['links'][0]['target']
    b = catalog['units'][2]['links'][2]['target']
    assert wc.target_id(a) == wc.target_id(b)
    assert catalog['units'][0]['links'][0]['role'] != catalog['units'][2]['links'][2]['role']


@pytest.mark.parametrize('field', ['status', 'test_count', 'deployed', 'accepted', 'progress'])
def test_catalog_cannot_become_a_second_status_store(catalog, field):
    catalog['units'][0][field] = True
    with pytest.raises(wc.CatalogError, match='invalid_fields'):
        wc.validate_catalog(catalog)


@pytest.mark.parametrize('path', ['../x', '/x', 'a//b', 'a/./b', 'a/../b', 'a\\b', '%2e%2e/x', 'a?token=x', 'a#x', 'C:/x'])
def test_repository_paths_never_become_local_or_arbitrary_urls(catalog, path):
    catalog['units'][0]['links'][1]['target']['path'] = path
    with pytest.raises(wc.CatalogError):
        wc.validate_catalog(catalog)


@pytest.mark.parametrize('sha', ['master', 'main', 'a'*7, 'A'*40, None, [], True])
def test_file_versions_are_pinned(catalog, sha):
    catalog['units'][0]['links'][1]['target']['sha'] = sha
    with pytest.raises(wc.CatalogError):
        wc.validate_catalog(catalog)


@pytest.mark.parametrize('number', [True, False, 0, -1, 1.0, '418', 10**16])
def test_identifiers_are_strict_integers(catalog, number):
    catalog['units'][0]['links'][0]['target']['number'] = number
    with pytest.raises(wc.CatalogError):
        wc.validate_catalog(catalog)


def test_unknown_repositories_and_missing_visibility_fail_closed(catalog):
    bad = copy.deepcopy(catalog)
    bad['units'][0]['links'][0]['target']['repo'] = 'other/private'
    with pytest.raises(wc.CatalogError, match='repository_not_approved'):
        wc.validate_catalog(bad)
    del catalog['units'][0]['visibility']
    with pytest.raises(wc.CatalogError):
        wc.validate_catalog(catalog)


@pytest.mark.parametrize('case', ['duplicate_unit', 'duplicate_link', 'dangling', 'self', 'kind', 'role'])
def test_identity_and_relation_invariants(catalog, case):
    unit = catalog['units'][0]
    if case == 'duplicate_unit':
        catalog['units'].append(copy.deepcopy(unit))
    elif case == 'duplicate_link':
        unit['links'].append(copy.deepcopy(unit['links'][0]))
    elif case == 'dangling':
        unit['related_units'].append('eeebot:area:missing')
    elif case == 'self':
        unit['related_units'].append(unit['id'])
    elif case == 'kind':
        catalog['units'][2]['kind'] = 'project'
    else:
        unit['links'][0]['role'] = 'done'
    with pytest.raises(wc.CatalogError):
        wc.validate_catalog(catalog)


def test_private_units_links_and_relationships_do_not_escape(catalog):
    original = copy.deepcopy(catalog)
    catalog['units'][1]['visibility'] = 'private'
    catalog['units'][1]['title'] = 'SECRET-UNIT-CANARY'
    catalog['units'][0]['links'][1]['visibility'] = 'private'
    catalog['units'][0]['links'][1]['label'] = 'SECRET-LINK-CANARY'
    projected = wc.public_catalog(catalog)
    encoded = json.dumps(projected)
    assert 'SECRET-' not in encoded
    assert catalog['units'][1]['id'] not in {u['id'] for u in projected['units']}
    assert catalog['units'][1]['id'] not in projected['units'][-1]['related_units']
    assert original['units'][0]['title'] == projected['units'][0]['title']
    projected['units'][0]['title'] = 'changed'
    assert catalog['units'][0]['title'] != 'changed'


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff', b'{'*2000])
def test_json_parser_rejects_ambiguous_or_invalid_input(tmp_path, raw):
    path = tmp_path / 'bad.json'
    path.write_bytes(raw)
    with pytest.raises(wc.CatalogError):
        wc.load_json(path)


def test_bounds_and_safe_error_messages(catalog, tmp_path):
    path = tmp_path / 'large.json'
    path.write_bytes(b' ' * (wc.MAX_BYTES + 1))
    with pytest.raises(wc.CatalogError, match='input_too_large'):
        wc.load_json(path)
    catalog['units'][0]['title'] = 'SECRET\nINJECTION'
    with pytest.raises(wc.CatalogError) as exc:
        wc.validate_catalog(catalog)
    assert 'SECRET' not in str(exc.value)


def test_run_reference_is_one_expected_revision_and_attempt(catalog):
    target = catalog['units'][0]['links'][-1]['target']
    assert '/attempts/1' in wc.target_url(target)
    assert target['expected_sha'] in wc.target_id(target)
    del target['attempt']
    with pytest.raises(wc.CatalogError):
        wc.validate_catalog(catalog)


def test_digest_does_not_depend_on_dict_key_order(catalog):
    other = dict(reversed(list(catalog.items())))
    assert wc.catalog_digest(catalog) == wc.catalog_digest(other)


def test_cli_returns_error_for_bad_catalog(tmp_path):
    path = tmp_path / 'empty.json'
    path.write_text('{}')
    assert wc.main([str(path)]) == 1
    assert wc.main([str(PILOT)]) == 0
