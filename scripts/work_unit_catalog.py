"""Work-unit pilot v1: descriptors and references, never a second backlog.

Standalone and stdlib-only. This module is not on the publisher sync manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

SCHEMA = 'work-units/v1'
REPOSITORIES = frozenset({'ozand/eeebot-ops-dashboard', 'ozand/eeebot'})
ROLES = ('context', 'work', 'result')
MAX_BYTES = 1_048_576
MAX_UNITS = 32
MAX_LINKS = 64
SHA = re.compile(r'[0-9a-f]{40}')
LOCAL_ID = re.compile(r'eeebot:(?:area|project|resource):[a-z0-9][a-z0-9-]{0,63}')


class CatalogError(ValueError):
    """Messages contain codes/field names only, never source values."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise CatalogError(code)


def fields(value: object, required: set[str], optional: set[str] | None = None) -> None:
    require(type(value) is dict, 'object_required')
    require(required <= value.keys() <= required | (optional or set()), 'invalid_fields')


def text(value: object, limit: int = 512) -> None:
    require(type(value) is str and 0 < len(value) <= limit, 'invalid_text')
    require(all(ord(c) >= 32 and ord(c) != 127 and not 0xD800 <= ord(c) <= 0xDFFF
                for c in value), 'invalid_text_characters')


def integer(value: object) -> None:
    require(type(value) is int and 1 <= value <= 10**15, 'invalid_identifier')


def validate_target(target: dict) -> None:
    require(type(target) is dict, 'target_required')
    kind = target.get('kind')
    require(type(kind) is str and kind in {'issue', 'pr', 'run', 'file', 'commit'}, 'invalid_kind')
    required = {'repo', 'kind'}
    required |= {'sha', 'path'} if kind == 'file' else {'sha'} if kind == 'commit' else {'number'}
    if kind == 'run':
        required |= {'attempt', 'expected_sha'}
    fields(target, required)
    require(type(target['repo']) is str and target['repo'] in REPOSITORIES, 'repository_not_approved')
    if kind in {'file', 'commit'}:
        require(type(target['sha']) is str and SHA.fullmatch(target['sha']) is not None, 'pinned_sha_required')
    else:
        integer(target['number'])
    if kind == 'run':
        integer(target['attempt'])
        require(type(target['expected_sha']) is str and SHA.fullmatch(target['expected_sha']) is not None,
                'expected_sha_required')
    if kind == 'file':
        path = target['path']
        text(path)
        require(not any(c in path for c in ('\\', '%', '?', '#', ':')), 'unsafe_repository_path')
        require(all(p not in {'', '.', '..'} for p in path.split('/')), 'unsafe_repository_path')


def target_id(target: dict) -> str:
    validate_target(target)
    kind = target['kind']
    suffix = (f"{target['sha']}:{target['path']}" if kind == 'file' else
              target['sha'] if kind == 'commit' else
              f"{target['number']}:{target['attempt']}:{target['expected_sha']}" if kind == 'run' else
              str(target['number']))
    return f"github:{target['repo']}:{kind}:{suffix}"


def target_url(target: dict) -> str:
    validate_target(target)
    base = 'https://github.com/' + target['repo']
    kind = target['kind']
    if kind == 'file':
        return f"{base}/blob/{target['sha']}/{quote(target['path'], safe='/')}"
    if kind == 'commit':
        return f"{base}/commit/{target['sha']}"
    if kind == 'run':
        return f"{base}/actions/runs/{target['number']}/attempts/{target['attempt']}"
    return f"{base}/{'issues' if kind == 'issue' else 'pull'}/{target['number']}"


def validate_catalog(catalog: dict) -> dict:
    fields(catalog, {'schema', 'units'})
    require(catalog['schema'] == SCHEMA, 'unsupported_schema')
    units = catalog['units']
    require(type(units) is list and 0 < len(units) <= MAX_UNITS, 'unit_limit')
    seen = set()
    for unit in units:
        fields(unit, {'id', 'kind', 'title', 'goal', 'boundaries', 'acceptance', 'visibility', 'links', 'related_units'})
        text(unit['id'], 180)
        require(unit['id'] not in seen, 'duplicate_unit')
        seen.add(unit['id'])
        require(type(unit['kind']) is str and unit['kind'] in {'area', 'project', 'resource'}, 'invalid_unit_kind')
        require(type(unit['visibility']) is str and unit['visibility'] in {'public', 'private'}, 'visibility_required')
        for key in ('title', 'goal'):
            text(unit[key])
        for key in ('boundaries', 'acceptance'):
            require(type(unit[key]) is list and 0 < len(unit[key]) <= 16, 'invalid_criteria')
            for value in unit[key]:
                text(value)
        require(type(unit['links']) is list and len(unit['links']) <= MAX_LINKS, 'link_limit')
        links = set()
        issue_ids = set()
        for link in unit['links']:
            fields(link, {'role', 'label', 'visibility', 'target'})
            require(type(link['role']) is str and link['role'] in ROLES, 'invalid_role')
            require(type(link['visibility']) is str and link['visibility'] in {'public', 'private'}, 'visibility_required')
            text(link['label'], 120)
            identity = target_id(link['target'])
            require((link['role'], identity) not in links, 'duplicate_link')
            links.add((link['role'], identity))
            if link['target']['kind'] == 'issue':
                issue_ids.add(identity)
        require(unit['id'] in issue_ids or LOCAL_ID.fullmatch(unit['id']) is not None, 'invalid_unit_id')
        if unit['id'].startswith('eeebot:'):
            require(unit['id'].split(':')[1] == unit['kind'], 'unit_kind_mismatch')
        related = unit['related_units']
        require(type(related) is list and len(related) <= MAX_UNITS, 'related_limit')
        for other in related:
            text(other, 180)
        require(len(set(related)) == len(related) and unit['id'] not in related, 'invalid_unit_relation')
    for unit in units:
        require(set(unit['related_units']) <= seen, 'dangling_unit_relation')
    return catalog


def _unique_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def load_json(path: Path) -> dict:
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, 'input_too_large')
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: require(False, 'nonfinite_json'))
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise CatalogError('invalid_json') from exc


def public_catalog(catalog: dict) -> dict:
    """Only reviewed, public descriptors; no private IDs, labels or counts."""
    validate_catalog(catalog)
    result = json.loads(json.dumps(catalog))
    result['units'] = [u for u in result['units'] if u['visibility'] == 'public']
    visible = {u['id'] for u in result['units']}
    for unit in result['units']:
        unit['links'] = [link for link in unit['links'] if link['visibility'] == 'public']
        unit['related_units'] = [i for i in unit['related_units'] if i in visible]
    return result


def catalog_digest(catalog: dict) -> str:
    validate_catalog(catalog)
    raw = json.dumps(catalog, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('catalog', type=Path)
    args = parser.parse_args(argv)
    try:
        catalog = validate_catalog(load_json(args.catalog))
    except (CatalogError, OSError):
        print('work-unit catalog: invalid or unreadable')
        return 1
    print(f"work-unit catalog: {len(catalog['units'])} units valid; sha256={catalog_digest(catalog)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
