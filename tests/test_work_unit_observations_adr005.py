"""ADR-005 C03/C07/C08/C10: existing observation contract evidence only."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from scripts import work_unit_catalog as wc
from scripts import work_unit_observations as wo

PILOT = Path(__file__).resolve().parents[1] / 'docs/work-units/pilot.json'
NOW = '2026-10-12T18:00:00Z'
AT = '2026-10-12T18:20:00Z'


@pytest.fixture
def catalog():
    return wc.load_json(PILOT)


def response(target, _timeout):
    if target['kind'] == 'issue':
        return {'number': target['number'], 'html_url': wc.target_url(target),
                'state': 'open', 'labels': []}
    if target['kind'] == 'pr':
        return {'number': target['number'], 'html_url': wc.target_url(target),
                'state': 'open', 'merged': False, 'head': {'sha': 'a' * 40}}
    return {'id': target['number'], 'run_attempt': target['attempt'],
            'head_sha': target['expected_sha'], 'status': 'completed', 'conclusion': 'success',
            'html_url': f"https://github.com/{target['repo']}/actions/runs/{target['number']}"}


def good(catalog):
    return wo.collect(catalog, get=response, clock=lambda: NOW, monotonic=lambda: 0.0)


def forbidden(*_args, **_kwargs):
    pytest.fail('invalid replay must be rejected before a network request')


@pytest.mark.parametrize('version', ['work-unit-observations/v0', 'work-unit-observations/v2',
    'work-unit-observations/v1.0', 'work-units/v1', None, True],
    ids=['v0', 'v2', 'v1-dot-zero', 'other-format', 'null', 'boolean'])
def test_adr005_unsupported_observation_schema_rejected(catalog, tmp_path, monkeypatch, capsys, version):
    saved = good(catalog)
    saved['schema'] = version
    previous = tmp_path / 'previous.json'
    previous.write_text(json.dumps(saved), encoding='utf-8')
    original = previous.read_bytes()
    loaded = wc.load_json(previous)
    with pytest.raises(wc.CatalogError, match='^unsupported_snapshot$'):
        wo.validate_snapshot(catalog, loaded)
    with pytest.raises(wc.CatalogError, match='^unsupported_snapshot$'):
        wo.collect(catalog, loaded, get=forbidden, clock=lambda: AT)
    monkeypatch.setattr(wo, 'build_opener', forbidden)
    assert wo.main([str(PILOT), '--collect', '--previous', str(previous),
                    '--output', str(previous)]) == 1
    assert previous.read_bytes() == original
    assert loaded['schema'] == version
    output = capsys.readouterr()
    assert output.out == 'work-unit observation input/output invalid or unreadable; no raw diagnostic emitted\n'
    assert output.err == ''


@pytest.mark.parametrize('delta', [-1, 0, 1], ids=['below', 'at', 'above'])
def test_adr005_snapshot_input_byte_boundary(catalog, tmp_path, delta):
    saved = good(catalog)
    raw = json.dumps(saved).encode()
    size = 1_048_576 + delta
    path = tmp_path / 'snapshot.json'
    path.write_bytes(raw + b' ' * (size - len(raw)))
    assert path.stat().st_size == size
    if delta <= 0:
        assert wo.validate_snapshot(catalog, wc.load_json(path)) == saved
    else:
        with pytest.raises(wc.CatalogError, match='^input_too_large$'):
            wc.load_json(path)


@pytest.mark.parametrize('delta', [-1, 0, 1], ids=['below', 'at', 'above'])
def test_adr005_response_byte_boundary(catalog, monkeypatch, delta):
    target = next(t for t in wo.targets(catalog).values() if t['kind'] == 'issue')
    raw = response(target, 1.0)
    payload = json.dumps(raw).encode()
    size = 1_048_576 + delta
    payload += b' ' * (size - len(payload))
    reads = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, maximum):
            reads.append(maximum)
            return payload

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == wo.endpoint(target)
            assert request.get_method() == 'GET' and timeout == 1.0
            return Response()

    monkeypatch.setattr(wo, 'build_opener', lambda *_: Opener())
    if delta <= 0:
        assert wo.github_get(target, 1.0) == raw
    else:
        with pytest.raises(wc.CatalogError, match='^response_too_large$'):
            wo.github_get(target, 1.0)
    assert reads == [1_048_577]


@pytest.mark.parametrize('delta', [-1, 0, 1], ids=['below', 'at', 'above'])
def test_adr005_snapshot_output_byte_boundary(catalog, tmp_path, monkeypatch, delta):
    saved = good(catalog)
    # Inject legal JSON whitespace at the serialization boundary. This keeps a
    # schema-valid snapshot while exercising the writer's exact encoded-byte cap.
    encoded = json.dumps(saved, sort_keys=True, indent=2, allow_nan=False)
    size = 1_048_576 + delta
    padded = encoded + ' ' * (size - 1 - len(encoded.encode()))  # writer adds LF
    path = tmp_path / 'saved.json'
    path.write_bytes(b'previous-output')
    with monkeypatch.context() as patch:
        patch.setattr(wo.json, 'dumps', lambda *_args, **_kwargs: padded)
        if delta <= 0:
            wo.write_snapshot(path, saved)
        else:
            with pytest.raises(wc.CatalogError, match='^snapshot_too_large$'):
                wo.write_snapshot(path, saved)
    if delta <= 0:
        assert path.stat().st_size == size
        assert wo.validate_snapshot(catalog, wc.load_json(path)) == saved
    else:
        assert path.read_bytes() == b'previous-output'
    assert list(tmp_path.glob('.work-unit-*')) == []


@pytest.mark.parametrize('size', [39, 40, 41])
def test_adr005_timestamp_text_boundary(size):
    value = '2026-10-12T18:00:00.' + '0' * (size - 26) + '+00:00'
    assert len(value) == size
    if size <= 40:
        assert wo.timestamp(value) == datetime(2026, 10, 12, 18, tzinfo=timezone.utc)
    else:
        with pytest.raises(wc.CatalogError, match='^invalid_text$'):
            wo.timestamp(value)


@pytest.mark.parametrize('requests', [0, 1, 31, 32, 63, 64, 65])
def test_adr005_request_budget_boundaries(catalog, requests):
    unit = catalog['units'][2]
    catalog['units'] = [unit]
    unit['related_units'] = []
    unit['links'] = [{'role': 'context', 'label': 'Synthetic issue', 'visibility': 'public',
        'target': {'repo': 'ozand/eeebot-ops-dashboard', 'kind': 'issue', 'number': i + 1}}
        for i in range(64)]
    calls = []

    def get(target, timeout):
        calls.append(wc.target_id(target))
        assert timeout == 10.0
        return response(target, timeout)

    if 1 <= requests <= 64:
        saved = wo.collect(catalog, get=get, clock=lambda: NOW,
                           monotonic=lambda: 0.0, max_requests=requests)
        assert len(calls) == len(set(calls)) == requests
        assert sum(r['probe'] == 'present' for r in saved['records'].values()) == requests
        assert sum(r['probe'] == 'budget_exhausted' for r in saved['records'].values()) == 64 - requests
    else:
        with pytest.raises(wc.CatalogError, match='^invalid_budget$'):
            wo.collect(catalog, get=get, max_requests=requests)
        assert calls == []


@pytest.mark.parametrize('seconds', [0, 1, 119, 120, 121])
def test_adr005_admission_budget_boundaries(catalog, seconds):
    calls = []

    def get(target, timeout):
        calls.append(timeout)
        return response(target, timeout)

    if 0 < seconds <= 120:
        wo.collect(catalog, get=get, clock=lambda: NOW,
                   monotonic=lambda: 0.0, budget_seconds=seconds)
        assert calls and all(t == min(10.0, seconds) for t in calls)
    else:
        with pytest.raises(wc.CatalogError, match='^invalid_budget$'):
            wo.collect(catalog, get=get, budget_seconds=seconds)
        assert calls == []


@pytest.mark.parametrize('change', ['title', 'goal', 'label', 'file-sha', 'file-path',
                                    'run-attempt', 'run-sha'])
def test_adr005_public_edit_rejects_persisted_snapshot_roundtrip(catalog, tmp_path, monkeypatch, change):
    old = good(catalog)
    path = tmp_path / 'snapshot.json'
    wo.write_snapshot(path, old)
    old_bytes = path.read_bytes()
    loaded = wc.load_json(path)
    assert wo.validate_snapshot(catalog, loaded) == old
    changed = copy.deepcopy(catalog)
    if change in {'title', 'goal'}:
        changed['units'][0][change] += ' revised'
    elif change == 'label':
        changed['units'][0]['links'][0]['label'] += ' revised'
    else:
        kind, field = change.split('-')
        target = next(link['target'] for unit in changed['units'] for link in unit['links']
                      if link['target']['kind'] == kind)
        key = 'expected_sha' if kind == 'run' and field == 'sha' else field
        target[key] = target[key] + 1 if field == 'attempt' else 'README.md' if field == 'path' else 'f' * 40
    wc.validate_catalog(changed)
    assert wo.public_digest(changed) != old['catalog_sha256']
    with pytest.raises(wc.CatalogError, match='^catalog_mismatch$'):
        wo.validate_snapshot(changed, loaded)
    with pytest.raises(wc.CatalogError, match='^catalog_mismatch$'):
        wo.collect(changed, loaded, get=forbidden, clock=lambda: AT)
    config = tmp_path / 'changed.json'
    config.write_text(json.dumps(changed), encoding='utf-8')
    monkeypatch.setattr(wo, 'build_opener', forbidden)
    assert wo.main([str(config), '--collect', '--previous', str(path), '--output', str(path)]) == 1
    assert path.read_bytes() == old_bytes
    # A separately requested fresh capture works; the rejected cache is never migrated.
    fresh = good(changed)
    fresh_path = tmp_path / 'fresh.json'
    wo.write_snapshot(fresh_path, fresh)
    assert wo.validate_snapshot(changed, wc.load_json(fresh_path)) == fresh
    assert wo.validate_snapshot(catalog, wc.load_json(path)) == old


@pytest.mark.parametrize('checked,observed,expected', [
    ('2026-10-12T18:20:01Z', NOW, 'clock_skew'),
    (AT, '2026-10-12T18:20:01Z', 'clock_skew'),
    (AT, NOW, 'stale'), (AT, AT, 'recent_at_assessment')],
    ids=['future-check-only', 'future-success-only', 'normal-check-old-success', 'equal-times'])
def test_adr005_split_clock_skew_snapshot_roundtrip(catalog, tmp_path, checked, observed, expected):
    saved = good(catalog)
    identity = next(i for i, r in saved['records'].items() if r['last_success'])
    record = saved['records'][identity]
    record['probe'] = 'unavailable'
    record['checked_at'] = checked
    record['last_success']['observed_at'] = observed
    before = copy.deepcopy(record)
    path = tmp_path / 'clock.json'
    wo.write_snapshot(path, saved)
    reread = wo.validate_snapshot(catalog, wc.load_json(path))['records'][identity]
    view = wo.evidence_view(reread, at=AT)
    assert view['freshness'] == expected
    assert view['probe'] == 'unavailable'
    assert view['last_success'] == before['last_success']
    assert reread == record == before  # No timestamp clamping or fabricated refresh.
