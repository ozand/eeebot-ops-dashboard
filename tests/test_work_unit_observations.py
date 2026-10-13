import copy
from http.client import BadStatusLine, IncompleteRead, LineTooLong, RemoteDisconnected
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from scripts import work_unit_catalog as wc
from scripts import work_unit_observations as wo

PILOT = Path(__file__).resolve().parents[1] / 'docs/work-units/pilot.json'
NOW = '2026-10-12T18:00:00Z'


@pytest.fixture
def catalog():
    return wc.load_json(PILOT)


def response(target):
    if target['kind'] == 'issue':
        return {'number': target['number'], 'html_url': wc.target_url(target), 'state': 'open',
                'labels': [{'name': 'status:test'}, {'name': 'PRIVATE-CANARY'}], 'body': 'PRIVATE-CANARY'}
    if target['kind'] == 'pr':
        return {'number': target['number'], 'html_url': wc.target_url(target), 'state': 'open',
                'merged': False, 'head': {'sha': 'a'*40}, 'body': '999 tests passed; PRIVATE-CANARY',
                'merge_commit_sha': 'b'*40}
    return {'id': target['number'], 'run_attempt': target['attempt'], 'head_sha': target['expected_sha'],
            'status': 'completed', 'conclusion': 'success', 'title': 'PRIVATE-CANARY',
            'html_url': f"https://github.com/{target['repo']}/actions/runs/{target['number']}"}


def good(catalog):
    return wo.collect(catalog, get=lambda t, _: response(t), clock=lambda: NOW)


def test_reads_only_six_unique_dynamic_public_references(catalog):
    calls = []
    def get(target, timeout):
        calls.append(wc.target_id(target))
        assert 0 < timeout <= 10
        return response(target)
    snapshot = wo.collect(catalog, get=get, clock=lambda: NOW)
    assert len(calls) == len(set(calls)) == 6
    assert 'PRIVATE-CANARY' not in json.dumps(snapshot)
    assert '999 tests' not in json.dumps(snapshot)
    assert all(r['last_success'] is None for r in snapshot['records'].values() if r['probe'] == 'not_collected')


def test_private_sources_never_requested(catalog):
    catalog['units'][0]['links'][2]['visibility'] = 'private'
    seen = []
    wo.collect(catalog, get=lambda t, _: seen.append(wc.target_id(t)) or response(t), clock=lambda: NOW)
    assert not any(':pr:419' in identity for identity in seen)


@pytest.mark.parametrize('exception,probe', [(OSError('SECRET'), 'unavailable'),
    (HTTPError('SECRET',404,'SECRET',None,None),'not_found_or_inaccessible'),
    (HTTPError('SECRET',403,'SECRET',None,None),'unavailable')])
def test_error_preserves_historical_success_without_fresh_claim(catalog, exception, probe):
    previous = good(catalog)
    def bad(*_):
        raise exception
    later = '2026-10-12T18:20:00Z'
    snapshot = wo.collect(catalog, previous, get=bad, clock=lambda: later)
    record = next(r for r in snapshot['records'].values() if r['last_success'])
    assert record['probe'] == probe
    assert record['last_success']['observed_at'] == NOW
    assert wo.evidence_view(record, at=later)['freshness'] == 'stale'
    assert 'SECRET' not in json.dumps(snapshot)


@pytest.mark.parametrize('age,expected', [(899,'recent_at_assessment'),(900,'stale'),(901,'stale'),(-1,'clock_skew')])
def test_exact_freshness_boundary(catalog, age, expected):
    record = next(r for r in good(catalog)['records'].values() if r['last_success'])
    at = (datetime(2026,10,12,18,tzinfo=timezone.utc)+timedelta(seconds=age)).isoformat()
    assert wo.evidence_view(record, at=at)['freshness'] == expected


@pytest.mark.parametrize('field,value', [('id',1),('run_attempt',2),('head_sha','f'*40)])
def test_mismatched_run_never_becomes_success(catalog, field, value):
    def get(target, _):
        raw = response(target)
        if target['kind'] == 'run':
            raw[field] = value
        return raw
    snapshot = wo.collect(catalog, get=get, clock=lambda: NOW)
    run = next(r for i,r in snapshot['records'].items() if ':run:' in i)
    assert run['probe'] == 'mismatch'
    assert run['last_success'] is None


def test_budget_does_not_fabricate_absence(catalog):
    snapshot = wo.collect(catalog, get=lambda t, _: response(t), clock=lambda: NOW, max_requests=1)
    assert sum(r['probe']=='present' for r in snapshot['records'].values()) == 1
    assert sum(r['probe']=='budget_exhausted' for r in snapshot['records'].values()) == 5


def test_deadline_stops_admitting_network_requests(catalog):
    tick = iter([0,61,62,63,64,65,66])
    def forbidden(*_):
        raise AssertionError('network should not run')
    snapshot = wo.collect(catalog, get=forbidden, clock=lambda: NOW, monotonic=lambda: next(tick))
    assert all(r['probe'] in {'budget_exhausted','not_collected'} for r in snapshot['records'].values())


@pytest.mark.parametrize('case', ['extra','catalog','source','facts','time','success'])
def test_invalid_cache_is_rejected_not_promoted_to_evidence(catalog, case):
    snapshot = good(catalog)
    record = next(r for r in snapshot['records'].values() if r['last_success'])
    if case == 'extra':
        snapshot['raw_body'] = 'SECRET'
    elif case == 'catalog':
        snapshot['catalog_sha256'] = 'f'*64
    elif case == 'source':
        snapshot['records']['private:source'] = copy.deepcopy(record)
    elif case == 'facts':
        record['last_success']['facts']['accepted'] = True
    elif case == 'time':
        record['checked_at'] = '2026-10-12T18:00:00'
    else:
        record['last_success'] = None
    with pytest.raises(wc.CatalogError):
        wo.validate_snapshot(catalog, snapshot)


def test_pr_merge_commit_is_not_a_merge_or_checkout_proof(catalog):
    snapshot = good(catalog)
    pr = next(r for i,r in snapshot['records'].items() if ':pr:' in i)
    assert pr['last_success']['facts']['merged'] is False
    run = next(r for i,r in snapshot['records'].items() if ':run:' in i)
    assert run['last_success']['facts']['checkout_sha'] is None
    assert 'merge_commit_sha' not in json.dumps(snapshot)


def test_raw_wrong_shapes_are_unknown_not_crashes(catalog):
    for raw in [None, [], {'state': 'open'}, {'number': True}]:
        snapshot = wo.collect(catalog, get=lambda *_: raw, clock=lambda: NOW)
        assert not any(r['probe']=='present' for r in snapshot['records'].values())


def test_static_private_changes_do_not_invalidate_public_snapshot(catalog):
    catalog['units'][0]['links'][1]['visibility'] = 'private'
    digest = wo.public_digest(catalog)
    catalog['units'][0]['links'][1]['label'] = 'PRIVATE-CHANGED'
    assert wo.public_digest(catalog) == digest


def test_atomic_snapshot_roundtrip_and_no_network_by_default(catalog, tmp_path):
    snapshot = good(catalog)
    path = tmp_path / 'snapshot.json'
    wo.write_snapshot(path, snapshot)
    assert wo.validate_snapshot(catalog, wc.load_json(path)) == snapshot
    assert list(tmp_path.glob('.work-unit-*')) == []
    with pytest.raises(SystemExit):
        wo.main([str(PILOT), '--output', str(path)])


def test_no_redirects_and_endpoint_is_constructed(catalog):
    assert wo.NoRedirect().redirect_request(None,None,302,'x',{},'https://other.invalid') is None
    for target in wo.targets(catalog).values():
        endpoint = wo.endpoint(target)
        assert endpoint is None or endpoint.startswith('https://api.github.com/repos/ozand/')


@pytest.mark.parametrize('payload', [b'{"state": "open"}', b'x' * (wc.MAX_BYTES + 1), b'{"a":1,"a":2}', b'null'])
def test_transport_get_is_bounded_unauthenticated_and_does_not_echo_payload(catalog, monkeypatch, payload):
    target = next(t for t in wo.targets(catalog).values() if t['kind'] == 'issue')
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, size):
            assert size == wc.MAX_BYTES + 1
            return payload
    class Opener:
        def open(self, request, timeout):
            assert request.get_method() == 'GET'
            assert 'Authorization' not in dict(request.header_items())
            assert request.full_url == wo.endpoint(target)
            assert timeout == 3.0
            return Response()
    monkeypatch.setattr(wo, 'build_opener', lambda *_: Opener())
    if len(payload) > wc.MAX_BYTES or payload.startswith(b'{"a"'):
        with pytest.raises(wc.CatalogError):
            wo.github_get(target, 3.0)
    else:
        assert wo.github_get(target, 3.0) == json.loads(payload)


def test_invalid_previous_file_does_not_replace_good_output(catalog, tmp_path, monkeypatch):
    previous = tmp_path / 'bad.json'
    previous.write_text('{}')
    out = tmp_path / 'saved.json'
    out.write_text('original')
    monkeypatch.setattr(wo, 'github_get', lambda *_: pytest.fail('no request expected'))
    assert wo.main([str(PILOT), '--collect', '--previous', str(previous), '--output', str(out)]) == 1
    assert out.read_text() == 'original'


def test_multiple_workflow_labels_are_preserved_without_resolving_conflict(catalog):
    target = next(t for t in wo.targets(catalog).values() if t['kind'] == 'issue')
    raw = response(target)
    raw['labels'] = [{'name':'status:test'}, {'name':'status:roll-out'}]
    assert wo.normalize(target, raw)['workflow_labels'] == ['status:roll-out','status:test']


@pytest.mark.parametrize('failure', [
    IncompleteRead(b'PRIVATE-PROTOCOL-BODY-CANARY', 128),
    RemoteDisconnected('PRIVATE-PROTOCOL-DIAGNOSTIC-CANARY'),
    BadStatusLine('PRIVATE-PROTOCOL-DIAGNOSTIC-CANARY'),
    LineTooLong('PRIVATE-PROTOCOL-DIAGNOSTIC-CANARY'),
], ids=['incomplete-read', 'remote-disconnected', 'bad-status-line', 'line-too-long'])
def test_protocol_read_failure_preserves_previous_snapshot(catalog, monkeypatch, tmp_path, capsys, failure):
    """A real github_get read failure must not discard history or export partial bytes."""
    previous = good(catalog)
    original = copy.deepcopy(previous)
    path = tmp_path / 'snapshot.json'
    wo.write_snapshot(path, previous)
    dynamic = {wo.endpoint(t): t for t in wo.targets(catalog).values()
               if wo.endpoint(t) is not None}
    failed_url = next(iter(dynamic))
    failed_id = wc.target_id(dynamic[failed_url])
    calls, closed = [], []

    class Response:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.append(self.url)
            return False

        def read(self, size):
            assert size == wc.MAX_BYTES + 1
            if self.url == failed_url:
                raise failure
            return json.dumps(response(dynamic[self.url])).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.get_method() == 'GET'
            assert 0 < timeout <= 10
            assert request.full_url in dynamic
            assert 'Authorization' not in dict(request.header_items())
            calls.append(request.full_url)
            return Response(request.full_url)

    monkeypatch.setattr(wo, 'build_opener', lambda *_: Opener())
    later = '2026-10-12T18:20:00Z'
    saved = wo.collect(catalog, wc.load_json(path), get=wo.github_get,
                       clock=lambda: later, monotonic=lambda: 0.0)
    record = saved['records'][failed_id]
    assert record['probe'] == 'unavailable'
    assert record['checked_at'] == later
    assert record['last_success'] == original['records'][failed_id]['last_success']
    assert record['last_success']['observed_at'] == NOW
    assert wo.evidence_view(record, at=later)['freshness'] == 'stale'
    assert calls == closed == list(dynamic)  # No retry, no abort of later sources.
    for url, target in dynamic.items():
        if url != failed_url:
            other = saved['records'][wc.target_id(target)]
            assert other['probe'] == 'present'
            assert other['last_success']['observed_at'] == later
    assert previous == original
    wo.write_snapshot(path, saved)
    assert wo.validate_snapshot(catalog, wc.load_json(path)) == saved
    output = capsys.readouterr()
    exported = path.read_text() + json.dumps(saved) + output.out + output.err
    assert 'PRIVATE-PROTOCOL-' not in exported
    assert 'PRIVATE-CANARY' not in exported
    assert 'Traceback' not in exported


@pytest.mark.parametrize('failure', [RuntimeError('programming failure'), ValueError('programming failure')])
def test_protocol_handling_does_not_swallow_unrelated_exceptions(catalog, failure):
    def bad(*_):
        raise failure

    with pytest.raises(type(failure), match='programming failure'):
        wo.collect(catalog, get=bad, clock=lambda: NOW, monotonic=lambda: 0.0)
