"""Bounded, opt-in, unauthenticated GitHub GET observations for the pilot.

No host reads, GitHub writes, raw payload persistence or publisher integration.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from scripts import work_unit_catalog as wc
except ModuleNotFoundError as exc:
    if exc.name not in {'scripts', 'scripts.work_unit_catalog'}:
        raise
    import work_unit_catalog as wc

SCHEMA = 'work-unit-observations/v1'
PROBES = {'present', 'unavailable', 'not_found_or_inaccessible', 'invalid', 'mismatch', 'budget_exhausted', 'not_collected'}
STATUSES = {'queued', 'in_progress', 'completed', 'waiting', 'pending', 'requested'}
CONCLUSIONS = {'success', 'failure', 'cancelled', 'neutral', 'skipped', 'timed_out', 'action_required', 'stale', 'startup_failure'}
LABELS = {'status:discovery', 'status:in-progress', 'status:test', 'status:roll-out'}


def timestamp(value: str) -> datetime:
    wc.text(value, 40)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise wc.CatalogError('invalid_timestamp') from exc
    wc.require(parsed.tzinfo is not None, 'timezone_required')
    return parsed.astimezone(timezone.utc)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def targets(catalog: dict) -> dict:
    public = wc.public_catalog(catalog)
    return {wc.target_id(link['target']): link['target']
            for unit in public['units'] for link in unit['links']}


def public_digest(catalog: dict) -> str:
    raw = json.dumps(wc.public_catalog(catalog), sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def endpoint(target: dict) -> str | None:
    wc.validate_target(target)
    prefix = f"https://api.github.com/repos/{target['repo']}"
    if target['kind'] == 'issue':
        return f"{prefix}/issues/{target['number']}"
    if target['kind'] == 'pr':
        return f"{prefix}/pulls/{target['number']}"
    if target['kind'] == 'run':
        return f"{prefix}/actions/runs/{target['number']}/attempts/{target['attempt']}"
    return None


def _sha(value: object) -> None:
    wc.require(type(value) is str and wc.SHA.fullmatch(value) is not None, 'invalid_sha')


def validate_facts(target: dict, facts: dict) -> dict:
    kind = target['kind']
    if kind == 'issue':
        wc.fields(facts, {'state', 'workflow_labels'})
        wc.require(facts['state'] in ('open', 'closed'), 'invalid_state')
        labels = facts['workflow_labels']
        wc.require(type(labels) is list and len(labels) <= len(LABELS), 'invalid_labels')
        wc.require(all(type(x) is str and x in LABELS for x in labels), 'invalid_labels')
        wc.require(labels == sorted(set(labels)), 'invalid_labels')
    elif kind == 'pr':
        wc.fields(facts, {'state', 'merged', 'head_sha'})
        wc.require(facts['state'] in ('open', 'closed') and type(facts['merged']) is bool, 'invalid_state')
        wc.require(not facts['merged'] or facts['state'] == 'closed', 'conflicting_state')
        _sha(facts['head_sha'])
    elif kind == 'run':
        wc.fields(facts, {'run_id', 'attempt', 'head_sha', 'status', 'conclusion', 'checkout_sha'})
        wc.integer(facts['run_id'])
        wc.integer(facts['attempt'])
        _sha(facts['head_sha'])
        wc.require((facts['run_id'], facts['attempt'], facts['head_sha']) ==
                   (target['number'], target['attempt'], target['expected_sha']), 'identity_mismatch')
        wc.require(type(facts['status']) is str and facts['status'] in STATUSES, 'invalid_status')
        c = facts['conclusion']
        wc.require(c is None or type(c) is str and c in CONCLUSIONS, 'invalid_conclusion')
        wc.require((facts['status'] == 'completed') == (c is not None), 'conflicting_state')
        wc.require(facts['checkout_sha'] is None, 'checkout_not_observed')
    else:
        raise wc.CatalogError('source_not_collected')
    return facts


def normalize(target: dict, raw: dict) -> dict:
    """Drop arbitrary titles, bodies, URLs, labels and diagnostics before storage."""
    wc.validate_target(target)
    wc.require(type(raw) is dict, 'invalid_response')
    kind, repo = target['kind'], target['repo']
    prefix = 'https://github.com/' + repo
    if kind in {'issue', 'pr'}:
        wc.integer(raw.get('number'))
        wc.require(raw['number'] == target['number'], 'identity_mismatch')
        wc.require(raw.get('html_url') == wc.target_url(target), 'identity_mismatch')
    if kind == 'issue':
        wc.require('pull_request' not in raw, 'identity_mismatch')
        labels = raw.get('labels')
        wc.require(type(labels) is list and len(labels) <= 100, 'invalid_labels')
        wc.require(all(type(x) is dict and type(x.get('name')) is str for x in labels), 'invalid_labels')
        facts = {'state': raw.get('state'),
                 'workflow_labels': sorted({x['name'] for x in labels if x['name'] in LABELS})}
    elif kind == 'pr':
        head = raw.get('head')
        wc.require(type(head) is dict, 'invalid_head')
        facts = {'state': raw.get('state'), 'merged': raw.get('merged'), 'head_sha': head.get('sha')}
    elif kind == 'run':
        wc.require(raw.get('html_url') == f"{prefix}/actions/runs/{target['number']}", 'identity_mismatch')
        facts = {'run_id': raw.get('id'), 'attempt': raw.get('run_attempt'), 'head_sha': raw.get('head_sha'),
                 'status': raw.get('status'), 'conclusion': raw.get('conclusion'), 'checkout_sha': None}
    else:
        raise wc.CatalogError('source_not_collected')
    return validate_facts(target, facts)


def validate_snapshot(catalog: dict, snapshot: dict) -> dict:
    wc.fields(snapshot, {'schema', 'catalog_sha256', 'records'})
    wc.require(snapshot['schema'] == SCHEMA, 'unsupported_snapshot')
    wc.require(snapshot['catalog_sha256'] == public_digest(catalog), 'catalog_mismatch')
    approved = targets(catalog)
    records = snapshot['records']
    wc.require(type(records) is dict and records.keys() <= approved.keys(), 'unexpected_source')
    for identity, record in records.items():
        wc.fields(record, {'checked_at', 'probe', 'last_success'})
        timestamp(record['checked_at'])
        wc.require(type(record['probe']) is str and record['probe'] in PROBES, 'invalid_probe')
        last = record['last_success']
        if last is not None:
            wc.fields(last, {'observed_at', 'facts'})
            timestamp(last['observed_at'])
            validate_facts(approved[identity], last['facts'])
        if record['probe'] == 'present':
            wc.require(last is not None and last['observed_at'] == record['checked_at'], 'invalid_success')
    return snapshot


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_get(target: dict, timeout: float) -> dict:
    """No credentials and no redirects. The caller supplies validated targets."""
    url = endpoint(target)
    wc.require(url is not None, 'source_not_collected')
    request = Request(url, headers={'Accept': 'application/vnd.github+json',
                                   'User-Agent': 'eeebot-work-unit-pilot'}, method='GET')
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(wc.MAX_BYTES + 1)
    wc.require(len(raw) <= wc.MAX_BYTES, 'response_too_large')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=wc._unique_pairs,
                          parse_constant=lambda _: wc.require(False, 'nonfinite_json'))
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise wc.CatalogError('invalid_response') from exc


def collect(catalog: dict, previous: dict | None = None, *, get=github_get,
            clock=now_utc, monotonic=time.monotonic, max_requests: int = 32,
            budget_seconds: float = 60) -> dict:
    wc.require(type(max_requests) is int and 1 <= max_requests <= 64, 'invalid_budget')
    wc.require(type(budget_seconds) in (int, float) and 0 < budget_seconds <= 120, 'invalid_budget')
    old = validate_snapshot(catalog, previous)['records'] if previous is not None else {}
    records = {}
    deadline = monotonic() + budget_seconds
    calls = 0
    for identity, target in targets(catalog).items():
        last = old.get(identity, {}).get('last_success')
        probe = 'not_collected'
        if endpoint(target) is not None:
            remaining = deadline - monotonic()
            if calls >= max_requests or remaining <= 0:
                probe = 'budget_exhausted'
            else:
                calls += 1
                try:
                    facts = normalize(target, get(target, min(10.0, remaining)))
                    at = clock()
                    last = {'observed_at': at, 'facts': facts}
                    probe = 'present'
                except HTTPError as exc:
                    probe = 'not_found_or_inaccessible' if exc.code == 404 else 'unavailable'
                except (OSError, URLError, TimeoutError, HTTPException):
                    # Protocol failures may carry a partial body; never persist it.
                    probe = 'unavailable'
                except wc.CatalogError as exc:
                    probe = 'mismatch' if str(exc) == 'identity_mismatch' else 'invalid'
        checked = last['observed_at'] if probe == 'present' else clock()
        records[identity] = {'checked_at': checked, 'probe': probe, 'last_success': last}
    snapshot = {'schema': SCHEMA, 'catalog_sha256': public_digest(catalog), 'records': records}
    return validate_snapshot(catalog, snapshot)


def evidence_view(record: dict | None, *, at: str, max_age_seconds: int = 900) -> dict:
    """Freshness is relative to an explicit assessment time, not 'live now'."""
    wc.require(type(max_age_seconds) is int and max_age_seconds > 0, 'invalid_max_age')
    instant = timestamp(at)
    if record is None:
        return {'probe': 'not_collected', 'freshness': 'unknown', 'last_success': None}
    last = record['last_success']
    age = None if last is None else (instant - timestamp(last['observed_at'])).total_seconds()
    checked_age = (instant - timestamp(record['checked_at'])).total_seconds()
    freshness = ('clock_skew' if checked_age < 0 or age is not None and age < 0 else
                 'unknown' if age is None else 'stale' if age >= max_age_seconds else 'recent_at_assessment')
    return {'probe': record['probe'], 'freshness': freshness, 'last_success': last}


def write_snapshot(path: Path, snapshot: dict) -> None:
    raw = (json.dumps(snapshot, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    wc.require(len(raw) <= wc.MAX_BYTES, 'snapshot_too_large')
    # Explicit caller-owned output only; never a host state directory default.
    fd, temporary = tempfile.mkstemp(prefix='.work-unit-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('catalog', type=Path)
    parser.add_argument('--collect', action='store_true', help='explicitly enable public GitHub GET requests')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--previous', type=Path)
    args = parser.parse_args(argv)
    if not args.collect:
        parser.error('--collect is required; no network access by default')
    try:
        catalog = wc.validate_catalog(wc.load_json(args.catalog))
        previous = wc.load_json(args.previous) if args.previous else None
        snapshot = collect(catalog, previous)
        write_snapshot(args.output, snapshot)
    except (wc.CatalogError, OSError):
        print('work-unit observation input/output invalid or unreadable; no raw diagnostic emitted')
        return 1
    failures = any(r['probe'] not in {'present', 'not_collected'} for r in snapshot['records'].values())
    print('work-unit snapshot saved; source checks incomplete' if failures else 'work-unit snapshot saved')
    return 2 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
