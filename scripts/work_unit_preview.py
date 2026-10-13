"""Offline-only work-unit cards. Not connected to production publication."""
from __future__ import annotations

import argparse
import hashlib
from html import escape
import json
from pathlib import Path

try:
    from scripts import work_unit_catalog as wc
    from scripts import work_unit_observations as wo
except ModuleNotFoundError as exc:
    if exc.name not in {'scripts', 'scripts.work_unit_catalog', 'scripts.work_unit_observations'}:
        raise
    import work_unit_catalog as wc
    import work_unit_observations as wo

ROLE_LABELS = {'context': '\u041a\u043e\u043d\u0442\u0435\u043a\u0441\u0442',
               'work': '\u0420\u0430\u0431\u043e\u0442\u0430',
               'result': '\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442'}


def build_index(catalog: dict, snapshot: dict | None = None, *, at: str) -> dict:
    wo.timestamp(at)
    records = wo.validate_snapshot(catalog, snapshot)['records'] if snapshot is not None else {}
    public = wc.public_catalog(catalog)
    for unit in public['units']:
        for link in unit['links']:
            identity = wc.target_id(link['target'])
            record = records.get(identity)
            link['reference_id'] = identity
            link['url'] = wc.target_url(link['target'])
            link['observation'] = wo.evidence_view(record, at=at)
            link['observation']['checked_at'] = record['checked_at'] if record else None
        # Not inferred from successful CI, closed issues, PR text or links.
        unit['acceptance_evidence'] = 'not_connected'
        unit['deployment_evidence'] = 'not_connected'
    return {'schema': 'work-unit-index/v1', 'assessment_at': at, 'units': public['units']}


def semantic_digest(index: dict) -> str:
    """Comparison aid only, NOT a production publish/refresh gate."""
    def strip_times(value):
        if isinstance(value, dict):
            return {k: strip_times(v) for k,v in value.items()
                    if k not in {'assessment_at', 'checked_at', 'observed_at'}}
        if isinstance(value, list):
            return [strip_times(v) for v in value]
        return value
    raw = json.dumps(strip_times(index), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def anchor(identity: str) -> str:
    return 'unit-' + hashlib.sha256(identity.encode()).hexdigest()


def _html(index: dict) -> str:
    """Private helper: accepts only build_index output, not untrusted HTML/JSON."""
    chunks = ['<!doctype html><html lang="ru"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
        '<title>eeebot | Context / Work / Result pilot</title>',
        '<style>body{font:16px/1.55 system-ui,sans-serif;background:#111827;color:#e5e7eb;margin:0}'
        'main{max-width:1200px;margin:auto;padding:24px}h1{font-size:2rem;line-height:1.2}'
        'h2{font-size:1.45rem}h3{font-size:1.2rem}a{color:#93c5fd}a:focus-visible{outline:3px solid #fbbf24}'
        'nav{display:flex;flex-wrap:wrap;gap:16px;margin:24px 0}.unit{border-top:1px solid #475569;padding:24px 0}'
        '.columns{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}'
        '.column,.note{background:#1f2937;padding:16px;border-radius:8px;min-width:0}'
        '.material{border-top:1px solid #475569;padding:12px 0}.meta{font-size:.85rem;color:#cbd5e1}'
        'p,code,a,dd{overflow-wrap:anywhere}dl{margin:8px 0}dt{font-weight:600}dd{margin:0 0 8px}'
        '.criteria{margin:12px 0}.back{display:block;margin-top:12px}@media(max-width:800px){'
        '.columns{grid-template-columns:1fr}main{padding:16px}h1{font-size:1.6rem}}'
        '</style></head><body><main id="top"><header><p>EEEBOT / READ-ONLY PILOT</p>',
        '<h1>' + ' / '.join(ROLE_LABELS.values()) + '</h1>',
        '<p class="note">Local preview only. Not a deployed dashboard. All observations are historical '
        'and relative to the assessment time below; this static page makes no live-status claim. '
        'No runtime source is connected. Links and reviewed descriptions are not independent evidence.</p>',
        f'<p>Assessment: <time>{escape(index["assessment_at"])}</time></p></header>',
        '<nav aria-label="Work units">']
    titles = {u['id']: u['title'] for u in index['units']}
    for unit in index['units']:
        chunks.append(f'<a href="#{anchor(unit["id"])}">{escape(unit["title"])}</a>')
    chunks.append('</nav>')
    if not index['units']:
        chunks.append('<p>No public units in the approved catalog.</p>')
    for unit in index['units']:
        chunks += [f'<article class="unit" id="{anchor(unit["id"])}"><h2>{escape(unit["title"])}</h2>',
            f'<p class="meta">{escape(unit["kind"])} | <code>{escape(unit["id"])}</code></p>',
            f'<p>{escape(unit["goal"])}</p><div class="criteria"><strong>Boundaries</strong><ul>']
        chunks.extend(f'<li>{escape(item)}</li>' for item in unit['boundaries'])
        chunks.append('</ul><strong>Acceptance criteria (not a recorded decision)</strong><ul>')
        chunks.extend(f'<li>{escape(item)}</li>' for item in unit['acceptance'])
        chunks.append('</ul></div><div class="columns">')
        for role in wc.ROLES:
            chunks.append(f'<section class="column" data-role="{role}"><h3>{ROLE_LABELS[role]}</h3>')
            links = [link for link in unit['links'] if link['role'] == role]
            if not links:
                chunks.append('<p>No linked materials; no outcome is inferred.</p>')
            for link in links:
                obs = link['observation']
                chunks += [f'<div class="material"><a href="{escape(link["url"], quote=True)}" rel="noreferrer">'
                           f'{escape(link["label"])}</a>',
                    f'<p class="meta">{escape(link["reference_id"])}</p>',
                    f'<p>Latest probe: <strong>{escape(obs["probe"])}</strong><br>'
                    f'Freshness at assessment: <strong>{escape(obs["freshness"])}</strong></p>']
                if obs['checked_at']:
                    chunks.append(f'<p class="meta">Checked: {escape(obs["checked_at"])}</p>')
                if obs['last_success']:
                    last = obs['last_success']
                    chunks.append(f'<p class="meta">Last successful observation: {escape(last["observed_at"])}</p><dl>')
                    for name, value in last['facts'].items():
                        display = 'unknown / not observed' if value is None else json.dumps(value, ensure_ascii=False)
                        chunks.append(f'<dt>{escape(name)}</dt><dd>{escape(display)}</dd>')
                    chunks.append('</dl>')
                else:
                    chunks.append('<p class="meta">Reference only; source facts not observed.</p>')
                chunks.append('</div>')
            if role == 'result':
                chunks.append('<p class="note">Acceptance evidence: not connected.<br>'
                              'Deployment evidence: not connected.<br>'
                              'This does not mean rejected or not deployed.</p>')
            chunks.append('</section>')
        chunks.append('</div><p>Related units: ')
        chunks.extend(f'<a href="#{anchor(i)}">{escape(titles[i])}</a> ' for i in unit['related_units'])
        chunks.append('</p><a class="back" href="#top">Back to index</a></article>')
    chunks.append('<footer><p>No writes, auto-refresh, external assets or executable scripts. '
                  'Review public labels and source permissions before any future publication.</p></footer></main></body></html>')
    return ''.join(chunks)


def render_html(catalog: dict, snapshot: dict | None = None, *, at: str) -> str:
    return _html(build_index(catalog, snapshot, at=at))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('catalog', type=Path)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--at', help='explicit assessment time for deterministic replay')
    parser.add_argument('--out', type=Path, required=True, help='new directory; existing directories are refused')
    args = parser.parse_args(argv)
    try:
        catalog = wc.validate_catalog(wc.load_json(args.catalog))
        snapshot = wc.load_json(args.snapshot) if args.snapshot else None
        index = build_index(catalog, snapshot, at=args.at or wo.now_utc())
        page = _html(index)
        args.out.mkdir()  # no overwrite, no guessed host path, no recursive parent creation
        (args.out / 'index.html').write_text(page, encoding='utf-8')
        (args.out / 'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except (wc.CatalogError, OSError):
        print('preview refused: invalid input or output directory unavailable; no raw diagnostic emitted')
        return 1
    print('offline preview created; no publication performed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
