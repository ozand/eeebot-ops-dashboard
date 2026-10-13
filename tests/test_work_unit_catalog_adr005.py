"""ADR-005 C03/C07: evidence for the existing catalog v1, not new limits."""
import json
from pathlib import Path

import pytest

from scripts import work_unit_catalog as wc

PILOT = Path(__file__).resolve().parents[1] / 'docs/work-units/pilot.json'


@pytest.fixture
def catalog():
    return wc.load_json(PILOT)


def area(number):
    return {'id': f'eeebot:area:boundary-{number}', 'kind': 'area',
            'title': 'Boundary fixture', 'goal': 'Check the existing contract',
            'boundaries': ['Synthetic data only'], 'acceptance': ['No inferred verdict'],
            'visibility': 'public', 'links': [], 'related_units': []}


def link(number=1):
    return {'role': 'context', 'label': 'Reference', 'visibility': 'public',
            'target': {'repo': 'ozand/eeebot-ops-dashboard', 'kind': 'issue', 'number': number}}


@pytest.mark.parametrize('version', ['work-units/v0', 'work-units/v2',
    'work-units/v1.0', 'work-unit-observations/v1', None, True],
    ids=['v0', 'v2', 'v1-dot-zero', 'other-format', 'null', 'boolean'])
def test_adr005_unsupported_catalog_schema_rejected(catalog, tmp_path, capsys, version):
    catalog['schema'] = version
    source = tmp_path / 'catalog.json'
    original = json.dumps(catalog).encode()
    source.write_bytes(original)
    loaded = wc.load_json(source)
    with pytest.raises(wc.CatalogError, match='^unsupported_schema$'):
        wc.validate_catalog(loaded)
    assert wc.main([str(source)]) == 1
    assert source.read_bytes() == original
    assert loaded['schema'] == version  # No implicit migration or repair.
    output = capsys.readouterr()
    assert output.out == 'work-unit catalog: invalid or unreadable\n'
    assert output.err == ''


@pytest.mark.parametrize('size', [0, 1, 31, 32, 33])
def test_adr005_catalog_unit_count_boundaries(size):
    assert wc.MAX_UNITS == 32
    value = {'schema': wc.SCHEMA, 'units': [area(i) for i in range(size)]}
    if 1 <= size <= 32:
        assert wc.validate_catalog(value) is value
    else:
        with pytest.raises(wc.CatalogError, match='^unit_limit$'):
            wc.validate_catalog(value)


@pytest.mark.parametrize('size', [0, 63, 64, 65])
def test_adr005_catalog_link_count_boundaries(size):
    assert wc.MAX_LINKS == 64
    unit = area(0)
    unit['links'] = [link(i + 1) for i in range(size)]
    value = {'schema': wc.SCHEMA, 'units': [unit]}
    if size <= 64:
        assert wc.validate_catalog(value) is value
    else:
        with pytest.raises(wc.CatalogError, match='^link_limit$'):
            wc.validate_catalog(value)


@pytest.mark.parametrize('field', ['boundaries', 'acceptance'])
@pytest.mark.parametrize('size', [0, 1, 15, 16, 17])
def test_adr005_catalog_criteria_count_boundaries(field, size):
    unit = area(0)
    unit[field] = [f'Criterion {i}' for i in range(size)]
    value = {'schema': wc.SCHEMA, 'units': [unit]}
    if 1 <= size <= 16:
        assert wc.validate_catalog(value) is value
    else:
        with pytest.raises(wc.CatalogError, match='^invalid_criteria$'):
            wc.validate_catalog(value)


@pytest.mark.parametrize('field,limit', [('title', 512), ('goal', 512),
    ('boundaries', 512), ('acceptance', 512), ('label', 120), ('path', 512)])
@pytest.mark.parametrize('boundary', ['empty', 'below', 'at', 'above'])
def test_adr005_catalog_text_length_boundaries(field, limit, boundary):
    size = {'empty': 0, 'below': limit - 1, 'at': limit, 'above': limit + 1}[boundary]
    unit = area(0)
    unit['links'] = [link()]
    if field in {'boundaries', 'acceptance'}:
        unit[field] = ['x' * size]
    elif field == 'label':
        unit['links'][0]['label'] = 'x' * size
    elif field == 'path':
        unit['links'][0]['target'] = {'repo': 'ozand/eeebot-ops-dashboard',
            'kind': 'file', 'sha': 'a' * 40, 'path': 'x' * size}
    else:
        unit[field] = 'x' * size
    value = {'schema': wc.SCHEMA, 'units': [unit]}
    if 0 < size <= limit:
        assert wc.validate_catalog(value) is value
    else:
        with pytest.raises(wc.CatalogError, match='^invalid_text$'):
            wc.validate_catalog(value)


@pytest.mark.parametrize('size,code', [(0, 'invalid_text'), (179, 'invalid_unit_id'),
    (180, 'invalid_unit_id'), (181, 'invalid_text')])
def test_adr005_unit_id_text_ceiling_does_not_bypass_identity_grammar(size, code):
    unit = area(0)
    unit['id'] = 'x' * size
    if 0 < size <= 180:
        wc.text(unit['id'], 180)  # Text length passes, identity must still fail.
    with pytest.raises(wc.CatalogError, match=f'^{code}$'):
        wc.validate_catalog({'schema': wc.SCHEMA, 'units': [unit]})


@pytest.mark.parametrize('size', [0, 1, 63, 64, 65])
def test_adr005_local_id_slug_boundaries(size):
    unit = area(0)
    unit['id'] = 'eeebot:area:' + 'x' * size
    value = {'schema': wc.SCHEMA, 'units': [unit]}
    if 1 <= size <= 64:
        assert wc.validate_catalog(value) is value
    else:
        with pytest.raises(wc.CatalogError, match='^invalid_unit_id$'):
            wc.validate_catalog(value)


@pytest.mark.parametrize('size,code', [(0, None), (31, None),
    (32, 'dangling_unit_relation'), (33, 'related_limit')])
def test_adr005_relation_ceiling_and_reachable_graph_are_distinct(size, code):
    units = [area(i) for i in range(32)]
    units[0]['related_units'] = [area(i + 1)['id'] for i in range(size)]
    value = {'schema': wc.SCHEMA, 'units': units}
    # With 32 total units and no self-edge, only 31 distinct peers can resolve.
    # 32 passes the list-size guard, NOT the whole graph contract. 33 hits the cap.
    if code is None:
        assert wc.validate_catalog(value) is value
    else:
        with pytest.raises(wc.CatalogError, match=f'^{code}$'):
            wc.validate_catalog(value)


@pytest.mark.parametrize('kind,field', [('issue', 'number'), ('pr', 'number'),
    ('run', 'number'), ('run', 'attempt')])
@pytest.mark.parametrize('number', [0, 1, 10**15 - 1, 10**15, 10**15 + 1])
def test_adr005_target_integer_boundaries(kind, field, number):
    target = {'repo': 'ozand/eeebot-ops-dashboard', 'kind': kind, 'number': 1}
    if kind == 'run':
        target.update(attempt=1, expected_sha='a' * 40)
    target[field] = number
    if 1 <= number <= 10**15:
        wc.validate_target(target)
    else:
        with pytest.raises(wc.CatalogError, match='^invalid_identifier$'):
            wc.validate_target(target)


@pytest.mark.parametrize('kind', ['file', 'commit', 'run'])
@pytest.mark.parametrize('size', [39, 40, 41])
def test_adr005_pinned_sha_length_boundaries(kind, size):
    target = {'repo': 'ozand/eeebot-ops-dashboard', 'kind': kind}
    if kind == 'run':
        target.update(number=1, attempt=1, expected_sha='a' * size)
    else:
        target['sha'] = 'a' * size
        if kind == 'file':
            target['path'] = 'README.md'
    if size == 40:
        wc.validate_target(target)
    else:
        with pytest.raises(wc.CatalogError, match='^(pinned|expected)_sha_required$'):
            wc.validate_target(target)


@pytest.mark.parametrize('delta', [-1, 0, 1], ids=['below', 'at', 'above'])
def test_adr005_catalog_json_byte_boundary(catalog, tmp_path, delta):
    assert wc.MAX_BYTES == 1_048_576
    catalog['units'][0]['title'] = '\u00e9'  # The ceiling counts UTF-8 bytes, not characters.
    payload = json.dumps(catalog, ensure_ascii=False).encode('utf-8')
    size = 1_048_576 + delta
    source = tmp_path / 'catalog.json'
    source.write_bytes(payload + b' ' * (size - len(payload)))
    assert source.stat().st_size == size
    if delta <= 0:
        assert wc.validate_catalog(wc.load_json(source)) == catalog
    else:
        with pytest.raises(wc.CatalogError, match='^input_too_large$'):
            wc.load_json(source)
