#!/usr/bin/env python3
"""Civilization-style tech-tree viewer for the eeebot self-evolving loop.

Standalone operator tooling. Fetches four state files (plus a short ledger
tail) from the `eeepc` authority host in a single SSH round-trip, then
renders a self-contained static HTML page styled like a strategy-game
research screen: one wide horizontal canvas, time flowing left to right,
in the spirit of the Civilization 5 tech tree. No external assets, no
CDNs -- the output opens fine as a plain `file://` document.

Usage:
    python scripts/techtree_viewer.py [--host eeepc] [--out techtree.html] [--open]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SSH_USER = 'ozand'
REMOTE_SUDO_USER = 'eeepc-agent'
SSH_TIMEOUT_SECONDS = 45

# Authority-host state root. Shared by both read paths: fetch_remote_state
# (below) uses it only as the default embedded in REMOTE_READER_SCRIPT, and
# read_local_state (issue #27) takes it as a real default argument so the
# viewer can run *on* eeepc without SSHing to itself.
STATE_ROOT = '/var/lib/eeepc-agent/self-evolving-agent/state'

# Ledger-tail filter, mirrored inside REMOTE_READER_SCRIPT's own copy of
# these same three constants (issue #27) -- see the duplication note on
# read_local_state for why that copy is not imported from here instead.
LEDGER_PHASES = {'tech_tree', 'hypothesis', 'evolution_tree'}
LEDGER_TAIL_LIMIT = 40
LEDGER_SCAN_WINDOW = 5000

# Read every source fail-soft, from a single remote python3 process fed over
# stdin. This keeps the whole fetch to exactly one SSH round-trip and avoids
# leaving any temp files behind on the remote host.
REMOTE_READER_SCRIPT = r'''
import json
import os

STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"
LEDGER_PHASES = {"tech_tree", "hypothesis", "evolution_tree"}
LEDGER_TAIL_LIMIT = 40
LEDGER_SCAN_WINDOW = 5000

# mtimes of every source file this process actually managed to read, so the
# caller (running on a different machine/clock, over SSH) can still report
# a "newest source age" freshness marker on the page (issue #27) instead of
# only a generation timestamp that a stale snapshot cannot be told apart by.
_mtimes = []


def read_json(relpath):
    path = os.path.join(STATE_ROOT, relpath)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _mtimes.append(os.path.getmtime(path))
        return data
    except Exception:
        return None


def read_ledger_tail(relpath):
    path = os.path.join(STATE_ROOT, relpath)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        _mtimes.append(os.path.getmtime(path))
    except Exception:
        return []
    matched = []
    for line in reversed(lines[-LEDGER_SCAN_WINDOW:]):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("phase") in LEDGER_PHASES:
            matched.append(obj)
            if len(matched) >= LEDGER_TAIL_LIMIT:
                break
    matched.reverse()
    return matched


result = {
    "portfolio": read_json("tech_tree/portfolio.json"),
    "scorecard": read_json("scorecard/latest.json"),
    "evolution_tree": read_json("evolution/tree.json"),
    "hypotheses": read_json("hypotheses/lifecycle.json"),
    "ledger_tail": read_ledger_tail("ledger/cycles.jsonl"),
    "_source_mtimes": _mtimes,
}
print(json.dumps(result))
'''.lstrip('\n')
# NOTE (issue #27): REMOTE_READER_SCRIPT duplicates STATE_ROOT/LEDGER_* and
# the read_json/read_ledger_tail bodies that also exist as real module code
# above and in read_local_state below. That is deliberate, not an oversight:
# this string is piped as-is into a bare `python3 -` on the remote host and
# cannot `import` anything from this file, so it has to stand entirely on
# its own. Do not "fix" this duplication by making REMOTE_READER_SCRIPT
# import techtree_viewer -- that would break the one thing this string
# exists to do. Keep the two copies in sync by hand if the filter changes.


def fetch_remote_state(host: str) -> dict[str, Any]:
    """Run the embedded reader script on `host` in one SSH round-trip.

    Returns the combined dict on success. On any transport-level failure
    (ssh unreachable, timeout, non-JSON stdout) returns an all-null dict
    with an `_error` note so the page can still render fail-soft panels.
    """
    empty: dict[str, Any] = {
        'portfolio': None,
        'scorecard': None,
        'evolution_tree': None,
        'hypotheses': None,
        'ledger_tail': None,
        '_newest_source_age_seconds': None,
    }
    command = [
        'ssh',
        f'{SSH_USER}@{host}',
        f'sudo -u {REMOTE_SUDO_USER} python3 -',
    ]
    try:
        proc = subprocess.run(
            command,
            input=REMOTE_READER_SCRIPT,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - defensive: any transport failure is fail-soft
        empty['_error'] = f'ssh transport failure: {exc.__class__.__name__}: {exc}'
        return empty

    if proc.returncode != 0:
        stderr = (proc.stderr or '').strip()
        empty['_error'] = f'ssh exited {proc.returncode}: {stderr[:400]}'
        return empty

    try:
        data = json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        empty['_error'] = f'could not parse remote output as JSON: {exc}'
        return empty

    for key in empty:
        data.setdefault(key, None)

    # The remote script reports raw mtimes (its own host clock); age is
    # computed here, against *this* machine's clock, since that is the
    # clock the "generated at" timestamp on the page will also use. Over a
    # healthy LAN/Tailscale link the skew is negligible; this path is
    # workstation tooling, not the autopublisher, so exactness isn't
    # required -- only "not fabricated when we don't actually know" is.
    mtimes = data.pop('_source_mtimes', None)
    if isinstance(mtimes, list) and mtimes:
        data['_newest_source_age_seconds'] = max(0.0, time.time() - max(mtimes))
    return data


def read_local_state(state_root: str) -> dict[str, Any]:
    """Read all five state sources directly from `state_root` -- no SSH (issue
    #27: for running the viewer *on* eeepc itself, e.g. from the
    autopublisher). Returns the exact same dict shape as fetch_remote_state,
    including the `_error` convention on a missing/unreadable state root, so
    render_page needs no change to its input contract regardless of which
    reader produced `data`.

    This intentionally duplicates the read_json / read_ledger_tail bodies
    that also live inside REMOTE_READER_SCRIPT above, in miniature.
    REMOTE_READER_SCRIPT is piped as a bare string to a remote `python3 -`
    and cannot import this function, so the two copies cannot be unified
    without breaking the remote path -- this is the accepted trade, not
    something a later cleanup should "fix".
    """
    empty: dict[str, Any] = {
        'portfolio': None,
        'scorecard': None,
        'evolution_tree': None,
        'hypotheses': None,
        'ledger_tail': None,
        '_newest_source_age_seconds': None,
    }
    root = Path(state_root)
    if not root.is_dir():
        empty['_error'] = f'state root not found or not a directory: {root}'
        return empty
    try:
        next(root.iterdir(), None)
    except OSError as exc:
        empty['_error'] = f'state root unreadable: {exc.__class__.__name__}: {exc}'
        return empty

    mtimes: list[float] = []

    def read_json(relpath: str) -> Any:
        path = root / relpath
        try:
            with path.open('r', encoding='utf-8') as fh:
                data = json.load(fh)
            mtimes.append(path.stat().st_mtime)
            return data
        except Exception:  # noqa: BLE001 - fail-soft per source, matches REMOTE_READER_SCRIPT
            return None

    def read_ledger_tail(relpath: str) -> list[Any]:
        path = root / relpath
        try:
            with path.open('r', encoding='utf-8', errors='replace') as fh:
                lines = fh.readlines()
            mtimes.append(path.stat().st_mtime)
        except Exception:  # noqa: BLE001
            return []
        matched: list[Any] = []
        for line in reversed(lines[-LEDGER_SCAN_WINDOW:]):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if obj.get('phase') in LEDGER_PHASES:
                matched.append(obj)
                if len(matched) >= LEDGER_TAIL_LIMIT:
                    break
        matched.reverse()
        return matched

    data: dict[str, Any] = {
        'portfolio': read_json('tech_tree/portfolio.json'),
        'scorecard': read_json('scorecard/latest.json'),
        'evolution_tree': read_json('evolution/tree.json'),
        'hypotheses': read_json('hypotheses/lifecycle.json'),
        'ledger_tail': read_ledger_tail('ledger/cycles.jsonl'),
        '_newest_source_age_seconds': None,
    }
    if mtimes:
        data['_newest_source_age_seconds'] = max(0.0, time.time() - max(mtimes))
    return data


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def esc(value: Any) -> str:
    """HTML-escape any value; titles/branches/etc. come from an autonomous
    LLM loop and must be treated as untrusted text."""
    if value is None:
        return ''
    return html.escape(str(value), quote=True)


def humanize_number(value: Any) -> str:
    if value is None:
        return 'n/a'
    try:
        num = float(value)
    except (TypeError, ValueError):
        return esc(value)
    sign = '-' if num < 0 else ''
    num = abs(num)
    if num >= 1_000_000_000:
        return f'{sign}{num / 1_000_000_000:.1f}B'
    if num >= 1_000_000:
        return f'{sign}{num / 1_000_000:.1f}M'
    if num >= 1_000:
        return f'{sign}{num / 1_000:.1f}K'
    if num == int(num):
        return f'{sign}{int(num)}'
    return f'{sign}{num:.2f}'


def humanize_ratio(value: Any) -> str:
    if value is None:
        return 'n/a'
    try:
        return f'{float(value) * 100:.1f}%'
    except (TypeError, ValueError):
        return esc(value)


def short_sha(sha: Any, length: int = 7) -> str:
    if not sha:
        return 'n/a'
    text = str(sha)
    return esc(text[:length])


def fmt_ts(ts: Any) -> str:
    if not ts:
        return 'unknown time'
    return esc(str(ts).replace('T', ' ').replace('Z', ' UTC'))


def title_case_name(name: Any) -> str:
    if not name:
        return 'unnamed'
    text = str(name).replace('-', ' ').replace('_', ' ')
    return esc(text.title())


def small_caps_metric(name: Any) -> str:
    if not name:
        return 'n/a'
    return esc(str(name))


def humanize_age(seconds: float) -> str:
    """Coarse, glanceable age string for the footer freshness marker (issue
    #27). Callers must only invoke this when a real source mtime was found
    -- there is no "unknown" case here by design, so a fabricated age can
    never be produced by this function itself."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f'{int(seconds)}s'
    minutes = seconds / 60
    if minutes < 60:
        return f'{int(minutes)}m'
    hours = minutes / 60
    if hours < 24:
        return f'{hours:.1f}h'
    days = hours / 24
    return f'{days:.1f}d'


def unavailable_panel(title: str, reason: str = 'source unavailable') -> str:
    return f'''
    <section class="panel panel-unavailable">
      <h2 class="panel-title">{esc(title)}</h2>
      <p class="unavailable-note">&#8968; {esc(reason)} &#8969;</p>
    </section>
    '''


def build_sparkline(gain_history: list[Any] | None) -> str:
    if not gain_history:
        return '<div class="spark-empty">no observations yet</div>'

    values: list[float] = []
    for v in gain_history:
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            values.append(0.0)

    # Two synchronized flex rows sharing a baseline: the top row holds
    # positive bars (aligned to its bottom edge, growing upward) and the
    # bottom row holds negative bars (aligned to its top edge, growing
    # downward). Each index gets a same-width placeholder in whichever row
    # it doesn't occupy so the columns stay aligned across both rows.
    max_abs = max((abs(v) for v in values), default=0.0) or 1.0
    top_cells = []
    bottom_cells = []
    for v in values:
        height = max(2, round((abs(v) / max_abs) * 14))
        title = esc(round(v, 4))
        if v >= 0:
            top_cells.append(f'<span class="bar bar-pos" style="height:{height}px;" title="{title}"></span>')
            bottom_cells.append('<span class="bar bar-placeholder"></span>')
        else:
            top_cells.append('<span class="bar bar-placeholder"></span>')
            bottom_cells.append(f'<span class="bar bar-neg" style="height:{height}px;" title="{title}"></span>')

    mean_gain = sum(values) / len(values)
    mean_class = 'mean-pos' if mean_gain >= 0 else 'mean-neg'
    return (
        '<div class="spark">'
        f'<div class="spark-top">{"".join(top_cells)}</div>'
        '<div class="spark-baseline"></div>'
        f'<div class="spark-bottom">{"".join(bottom_cells)}</div>'
        '</div>'
        f'<div class="spark-mean {mean_class}">mean gain {mean_gain:+.4f}</div>'
    )


# ---------------------------------------------------------------------------
# Wide horizontal canvas -- Civilization 5 tech-tree style.
#
# Time flows left -> right. Two lanes share one canvas: Lane A ("RESEARCH")
# is the directions chronicle (portfolio.switches), Lane B ("WORLD HISTORY")
# is the git-native evolution DAG. Both lanes place nodes as small HTML
# boxes via SVG <foreignObject> so the existing escaped-HTML card markup
# (sparkline, badges, etc.) can be reused verbatim inside one <svg>, and
# orthogonal "elbow" connectors (native SVG <path>, right-angle bends) join
# box edges Civ-style instead of curvy timeline arrows.
# ---------------------------------------------------------------------------

DIR_BOX_W = 210
DIR_BOX_H = 150
DIR_ROW_GAP = 16
RESERVE_COL_W = 210
COL_PITCH = 250  # era-column pitch, shared by both lanes for the grid lines
LANE_TOP_PAD = 60  # room above the spine row for the mint-origin axis row
MINT_AXIS_Y = 18
CANVAS_MARGIN_X = 40
CANVAS_MARGIN_Y = 20
LANE_GAP = 50
MIN_CANVAS_W = 1200

EVO_BOX_W = 150
EVO_BOX_H = 46
EVO_ROW_H = 62
EVO_MARGIN_X = 60
EVO_MAX_DISPLAY = 30

# Fixed, deterministic glyph table for research directions -- plain numeric
# character references (matches the rest of this file's convention of never
# embedding literal multi-byte glyphs in source) so rendering never depends
# on the terminal/editor encoding. Picked to read as "tech tree" icons
# without relying on emoji font coverage.
_DIRECTION_GLYPH_CODES = (9672, 9958, 9879, 9750, 10070, 11041, 11042, 9959, 9751, 9874, 10057)


def _direction_glyph(name: Any) -> str:
    """Deterministic (stable across runs/processes) unicode glyph per
    direction name -- purely cosmetic, never invents identity."""
    key = str(name or '')
    digest = hashlib.md5(key.encode('utf-8')).hexdigest()
    idx = int(digest[:8], 16) % len(_DIRECTION_GLYPH_CODES)
    return f'&#{_DIRECTION_GLYPH_CODES[idx]};'


def _elbow_path(x1: float, y1: float, x2: float, y2: float, css_class: str, dashed: bool = False) -> str:
    """Orthogonal 2-bend "elbow" connector (horizontal / vertical /
    horizontal) from (x1, y1) to (x2, y2), Civ-tech-tree style."""
    midx = (x1 + x2) / 2
    d = f'M{x1:.0f},{y1:.0f} L{midx:.0f},{y1:.0f} L{midx:.0f},{y2:.0f} L{x2:.0f},{y2:.0f}'
    dash_attr = ' stroke-dasharray="6 5"' if dashed else ''
    return f'<path d="{d}" class="{css_class}"{dash_attr} />'


def _visited_sequence(switches: list[Any], valid_names: set[str]) -> list[str]:
    """The empire's research spine: from0 -> to0 -> to1 -> ... in
    ``switches`` order, deduplicated consecutively, restricted to
    directions that still exist in the portfolio (fail-soft against stale
    switch entries naming a retired/renamed node)."""
    seq: list[str] = []
    for switch in switches:
        if not isinstance(switch, dict):
            continue
        for key in ('from', 'to'):
            name = switch.get(key)
            if name in valid_names and (not seq or seq[-1] != name):
                seq.append(name)
    return seq


def _direction_box_html(name: str, node: dict[str, Any], is_current: bool, x: float, y: float, dim: bool = False) -> str:
    status = node.get('status') or 'active'
    minted_by = node.get('minted_by')

    if is_current:
        badge_class, badge_text = 'badge-researching', 'RESEARCHING'
        box_class = 'dir-box dir-box-current'
    elif status == 'plateaued':
        badge_class, badge_text = 'badge-plateaued', 'PLATEAUED'
        box_class = 'dir-box dir-box-plateaued'
    else:
        badge_class, badge_text = 'badge-available', 'AVAILABLE'
        box_class = 'dir-box'
    if dim and not is_current:
        box_class += ' dir-box-dim'

    ribbon = '<div class="ribbon">MINTED</div>' if minted_by == 'hypothesis' else ''
    cooldown_note = ''
    if status == 'plateaued' and node.get('cooldown_until_ts'):
        cooldown_note = f'<div class="cooldown">cooldown until {fmt_ts(node.get("cooldown_until_ts"))}</div>'

    body = (
        f'<div class="{box_class}">'
        f'{ribbon}'
        '<div class="dir-box-head">'
        f'<span class="dir-glyph">{_direction_glyph(name)}</span>'
        f'<span class="dir-name">{title_case_name(name)}</span>'
        '</div>'
        f'<div class="dir-lever">{small_caps_metric(node.get("lever_metric"))}</div>'
        f'<span class="badge {badge_class}">{badge_text}</span>'
        f'{build_sparkline(node.get("gain_history"))}'
        f'{cooldown_note}'
        '</div>'
    )
    return f'<foreignObject x="{x:.0f}" y="{y:.0f}" width="{DIR_BOX_W}" height="{DIR_BOX_H}">{body}</foreignObject>'


def _lane_a_layout(portfolio: dict[str, Any] | None, ledger_tail: list[Any] | None) -> dict[str, Any]:
    """Lane A geometry: DIRECTIONS. Returns a dict describing either an
    'unavailable' state or the full set of SVG fragments + bounding box."""
    if not isinstance(portfolio, dict):
        return {'available': False, 'reason': 'portfolio.json unavailable'}

    nodes = portfolio.get('nodes')
    if not isinstance(nodes, dict) or not nodes:
        return {'available': False, 'reason': 'no research nodes recorded'}

    valid_nodes = {name: node for name, node in nodes.items() if isinstance(node, dict)}
    if not valid_nodes:
        return {'available': False, 'reason': 'no research nodes recorded'}

    current = portfolio.get('current')
    switches = portfolio.get('switches')
    if not isinstance(switches, list) or not switches:
        derived = []
        for entry in (ledger_tail or []):
            if isinstance(entry, dict) and entry.get('phase') == 'tech_tree' and entry.get('from') and entry.get('to'):
                derived.append(entry)
        switches = derived

    visited = _visited_sequence(switches, set(valid_nodes))

    if visited:
        visited_set = set(visited)
        reserve = sorted(
            (name for name in valid_nodes if name not in visited_set),
            key=lambda n: valid_nodes[n].get('created_ts') or '',
        )
        note = None
    else:
        # No switch chronicle at all -- everything (including any
        # in-progress `current`) sits in the reserve column.
        reserve = sorted(valid_nodes, key=lambda n: valid_nodes[n].get('created_ts') or '')
        note = 'no research journey yet'

    reserve_x = CANVAS_MARGIN_X
    spine_x0 = CANVAS_MARGIN_X + RESERVE_COL_W + 40
    spine_y = LANE_TOP_PAD

    pos: dict[str, tuple[float, float]] = {}
    boxes: list[str] = []
    elbows: list[str] = []
    mint: list[str] = []

    for idx, name in enumerate(visited):
        x = spine_x0 + idx * COL_PITCH
        pos[name] = (x, spine_y)
        is_current = name == current or (current not in valid_nodes and idx == len(visited) - 1)
        boxes.append(_direction_box_html(name, valid_nodes[name], is_current, x, spine_y))

    for i in range(len(visited) - 1):
        x1, y1 = pos[visited[i]]
        x2, y2 = pos[visited[i + 1]]
        elbows.append(_elbow_path(x1 + DIR_BOX_W, y1 + DIR_BOX_H / 2, x2, y2 + DIR_BOX_H / 2, 'dir-elbow'))

    for ridx, name in enumerate(reserve):
        y = spine_y + ridx * (DIR_BOX_H + DIR_ROW_GAP)
        pos[name] = (reserve_x, y)
        is_current = name == current
        boxes.append(_direction_box_html(name, valid_nodes[name], is_current, reserve_x, y, dim=True))

    for name, node in valid_nodes.items():
        if node.get('minted_by') == 'hypothesis' and name in pos:
            bx, by = pos[name]
            gx = bx + DIR_BOX_W / 2
            mint.append(f'<text x="{gx:.0f}" y="{MINT_AXIS_Y}" class="mint-glyph" text-anchor="middle">&#127979;</text>')
            mint.append(_elbow_path(gx, MINT_AXIS_Y + 8, gx, by, 'mint-elbow', dashed=True))

    reserve_height = len(reserve) * DIR_BOX_H + max(len(reserve) - 1, 0) * DIR_ROW_GAP
    lane_content_height = max(DIR_BOX_H, reserve_height)
    height = spine_y + lane_content_height + 24

    width = reserve_x + RESERVE_COL_W + CANVAS_MARGIN_X
    if visited:
        width = max(width, spine_x0 + len(visited) * COL_PITCH)

    return {
        'available': True,
        'boxes': boxes,
        'elbows': elbows,
        'mint': mint,
        'note': note,
        'width': width,
        'height': height,
        'grid_cols': len(visited),
        'grid_x0': spine_x0,
        'grid_pitch': COL_PITCH,
    }


def _evo_box_html(sha: str, node: dict[str, Any], is_current: bool, is_abandoned: bool, switch_marked: bool, x: float, y: float) -> str:
    branch = str(node.get('branch') or '')
    tail = branch.rsplit('/', 1)[-1] if branch else ''
    raw_label = f'{tail}-{sha[:7]}' if tail else sha[:7]
    label = esc(raw_label)
    marker = ' &#8634;' if switch_marked else ''

    box_class = 'evo-box'
    if is_current:
        box_class += ' evo-box-current'
    elif is_abandoned:
        box_class += ' evo-box-abandoned'

    diamond = '<span class="evo-diamond">&#9672;</span>' if is_current else ''
    body = (
        f'<div class="{box_class}">'
        f'{diamond}<span class="evo-box-label">{label}{marker}</span>'
        '</div>'
    )
    return f'<foreignObject x="{x:.0f}" y="{y:.0f}" width="{EVO_BOX_W}" height="{EVO_BOX_H}">{body}</foreignObject>'


def _lane_b_layout(evolution_tree: dict[str, Any] | None) -> dict[str, Any]:
    """Lane B geometry: EVOLUTION (git-native DAG). Mirrors Lane A's
    contract: an 'unavailable' dict, a 'fallback_html' dict (<2 nodes,
    simple list), or the full SVG-fragment layout."""
    if not isinstance(evolution_tree, dict):
        return {'available': False, 'reason': 'evolution tree unavailable'}

    current_sha = evolution_tree.get('current_sha')
    nodes = evolution_tree.get('nodes')
    switches = evolution_tree.get('switches')
    switches_count = len(switches) if isinstance(switches, list) else (switches if isinstance(switches, int) else 0)

    if not isinstance(nodes, dict):
        nodes = {}
    valid_nodes = {sha: node for sha, node in nodes.items() if isinstance(node, dict)}

    summary = {'current_sha': current_sha, 'total_nodes': len(nodes), 'switches_count': switches_count}

    if len(valid_nodes) < 2:
        node_list = sorted(valid_nodes.items(), key=lambda item: item[1].get('ts') or '', reverse=True)
        rows = []
        for sha, node in node_list[:6]:
            rows.append(
                '<li>'
                f'<span class="timeline-sha">{short_sha(sha)}</span>'
                f'<span class="timeline-branch">{esc(node.get("branch") or "n/a")}</span>'
                f'<span class="timeline-ts">{fmt_ts(node.get("ts"))}</span>'
                '</li>'
            )
        list_html = (
            '<ul class="timeline-list">' + ''.join(rows) + '</ul>' if rows
            else '<p class="unavailable-note">no evolution nodes recorded yet</p>'
        )
        return {'available': True, 'fallback_html': list_html, 'summary': summary, 'height': 150}

    items = sorted(valid_nodes.items(), key=lambda kv: kv[1].get('ts') or '', reverse=True)
    total = len(items)
    trunc_note = None
    if total > EVO_MAX_DISPLAY:
        items = items[:EVO_MAX_DISPLAY]
        trunc_note = f'showing last {EVO_MAX_DISPLAY} of {total} nodes'
    kept = dict(items)

    depth: dict[str, int] = {}

    def _depth(sha: str, guard: frozenset[str] = frozenset()) -> int:
        if sha in depth:
            return depth[sha]
        if sha in guard:  # defensive cycle guard -- a real git DAG never cycles
            depth[sha] = 0
            return 0
        node = kept.get(sha) or {}
        parent = node.get('parent_sha')
        if not parent or parent not in kept:
            depth[sha] = 0
        else:
            depth[sha] = _depth(parent, guard | {sha}) + 1
        return depth[sha]

    for sha in kept:
        _depth(sha)

    children_count: dict[str, int] = {}
    for sha, node in kept.items():
        parent = node.get('parent_sha')
        if parent in kept:
            children_count[parent] = children_count.get(parent, 0) + 1

    ordered = sorted(kept.items(), key=lambda kv: (depth[kv[0]], kv[1].get('parent_sha') or '', kv[1].get('ts') or ''))
    slot: dict[int, int] = {}
    pos: dict[str, tuple[float, float]] = {}
    for sha, _node in ordered:
        d = depth[sha]
        s = slot.get(d, 0)
        slot[d] = s + 1
        pos[sha] = (EVO_MARGIN_X + d * COL_PITCH, LANE_TOP_PAD + s * EVO_ROW_H)

    max_depth = max(depth.values(), default=0)
    max_slot = max(slot.values(), default=1)

    switch_shas: set[str] = set()
    for switch in (switches or []):
        if isinstance(switch, dict):
            for key in ('from_sha', 'to_sha'):
                sha_ref = switch.get(key)
                if sha_ref:
                    switch_shas.add(sha_ref)

    elbows = []
    for sha, node in kept.items():
        parent = node.get('parent_sha')
        if parent in pos:
            x1, y1 = pos[parent]
            x2, y2 = pos[sha]
            elbows.append(_elbow_path(x1 + EVO_BOX_W, y1 + EVO_BOX_H / 2, x2, y2 + EVO_BOX_H / 2, 'evo-elbow'))

    boxes = []
    for sha, node in kept.items():
        x, y = pos[sha]
        is_current = sha == current_sha
        is_abandoned = (not is_current) and children_count.get(sha, 0) == 0
        boxes.append(_evo_box_html(sha, node, is_current, is_abandoned, sha in switch_shas, x, y))

    width = EVO_MARGIN_X * 2 + (max_depth + 1) * COL_PITCH
    height = LANE_TOP_PAD + max_slot * EVO_ROW_H + 24

    return {
        'available': True,
        'boxes': boxes,
        'elbows': elbows,
        'note': trunc_note,
        'summary': summary,
        'width': width,
        'height': height,
        'grid_cols': max_depth + 1,
        'grid_x0': EVO_MARGIN_X,
        'grid_pitch': COL_PITCH,
    }


def build_tech_canvas(portfolio: dict[str, Any] | None, ledger_tail: list[Any] | None, evolution_tree: dict[str, Any] | None) -> str:
    """The main area: ONE wide horizontally-scrollable SVG canvas holding
    both lanes on a shared left-to-right time axis. Fail-soft per lane."""
    lane_a = _lane_a_layout(portfolio, ledger_tail)
    lane_b = _lane_b_layout(evolution_tree)

    canvas_width = MIN_CANVAS_W
    if lane_a.get('available') and 'width' in lane_a:
        canvas_width = max(canvas_width, lane_a['width'])
    if lane_b.get('available') and 'width' in lane_b:
        canvas_width = max(canvas_width, lane_b['width'])

    grid_xs: set[float] = set()
    groups: list[str] = []
    y_cursor = CANVAS_MARGIN_Y

    # --- Lane A: RESEARCH -----------------------------------------------
    label_a = '<text x="10" y="14" class="lane-label">RESEARCH</text>'
    if lane_a.get('available'):
        note_html = f'<text x="10" y="32" class="lane-note">{esc(lane_a["note"])}</text>' if lane_a.get('note') else ''
        body = ''.join(lane_a['boxes']) + ''.join(lane_a['elbows']) + ''.join(lane_a['mint'])
        groups.append(f'<g class="lane lane-a" transform="translate(0,{y_cursor})">{label_a}{note_html}{body}</g>')
        for i in range(lane_a.get('grid_cols', 0) + 1):
            grid_xs.add(lane_a['grid_x0'] + i * lane_a['grid_pitch'] - 20)
        lane_a_height = lane_a['height']
    else:
        unavailable_html = f'<text x="10" y="36" class="lane-unavailable">&#8968; {esc(lane_a["reason"])} &#8969;</text>'
        groups.append(f'<g class="lane lane-a" transform="translate(0,{y_cursor})">{label_a}{unavailable_html}</g>')
        lane_a_height = 60
    y_cursor += lane_a_height + LANE_GAP

    # --- Lane B: WORLD HISTORY -------------------------------------------
    label_b = '<text x="10" y="14" class="lane-label">WORLD HISTORY</text>'
    if lane_b.get('available'):
        if 'fallback_html' in lane_b:
            fo_width = max(canvas_width - 40, 400)
            fo_height = lane_b['height']
            fallback = (
                f'<foreignObject x="10" y="22" width="{fo_width}" height="{fo_height}">'
                f'<div class="evo-fallback">{lane_b["fallback_html"]}</div>'
                '</foreignObject>'
            )
            groups.append(f'<g class="lane lane-b" transform="translate(0,{y_cursor})">{label_b}{fallback}</g>')
            lane_b_height = fo_height + 22
        else:
            note_html = f'<text x="10" y="32" class="lane-note">{esc(lane_b["note"])}</text>' if lane_b.get('note') else ''
            body = ''.join(lane_b['boxes']) + ''.join(lane_b['elbows'])
            groups.append(f'<g class="lane lane-b" transform="translate(0,{y_cursor})">{label_b}{note_html}{body}</g>')
            for i in range(lane_b.get('grid_cols', 0) + 1):
                grid_xs.add(lane_b['grid_x0'] + i * lane_b['grid_pitch'] - 20)
            lane_b_height = lane_b['height']
    else:
        unavailable_html = f'<text x="10" y="36" class="lane-unavailable">&#8968; {esc(lane_b["reason"])} &#8969;</text>'
        groups.append(f'<g class="lane lane-b" transform="translate(0,{y_cursor})">{label_b}{unavailable_html}</g>')
        lane_b_height = 60
    y_cursor += lane_b_height + CANVAS_MARGIN_Y

    canvas_height = y_cursor
    canvas_width = max(canvas_width, MIN_CANVAS_W)

    grid_svg = ''.join(
        f'<line x1="{x:.0f}" y1="0" x2="{x:.0f}" y2="{canvas_height}" class="era-grid-line" />'
        for x in sorted(grid_xs) if x > 0
    )

    svg = (
        f'<svg class="tech-canvas" width="{canvas_width}" height="{canvas_height}" '
        f'viewBox="0 0 {canvas_width} {canvas_height}">'
        f'{grid_svg}{"".join(groups)}'
        '</svg>'
    )
    return f'<div class="canvas-outer">{svg}</div>'


# ---------------------------------------------------------------------------
# Left rail -- ERAS (trust-ladder medallions) + GREAT LIBRARY summary.
# ---------------------------------------------------------------------------

def build_eras_rail(scorecard: dict[str, Any] | None) -> str:
    control_plane = scorecard.get('control_plane') if isinstance(scorecard, dict) else None
    ladder_info = control_plane.get('runtime_trust_ladder') if isinstance(control_plane, dict) else None
    if not isinstance(ladder_info, dict):
        return unavailable_panel('Eras', 'trust ladder unavailable')

    level = ladder_info.get('level')
    unlocked = ladder_info.get('unlocked')
    unlocked_set = set(unlocked) if isinstance(unlocked, list) else set()
    ladder = ladder_info.get('ladder')
    if not isinstance(ladder, list) or not ladder:
        return unavailable_panel('Eras', 'trust ladder unavailable')

    medallions = []
    for idx, rung in enumerate(ladder):
        rung_name = str(rung).rsplit('/', 1)[-1]
        if rung_name.endswith('.py'):
            rung_name = rung_name[:-3]
        is_lit = rung in unlocked_set or (isinstance(level, int) and idx < level)
        css_class = 'era-lit' if is_lit else 'era-locked'
        glyph = '&#9733;' if is_lit else '&#128274;'
        medallions.append(f'''
        <div class="era-medallion {css_class}">
          <div class="era-glyph">{glyph}</div>
          <div class="era-name">{title_case_name(rung_name)}</div>
        </div>
        ''')

    level_text = esc(level) if level is not None else 'unknown'
    return f'''
    <section class="panel panel-eras">
      <h2 class="panel-title">Eras</h2>
      <div class="era-rail">{''.join(medallions)}</div>
      <div class="era-caption">Trust Level {level_text}</div>
    </section>
    '''


def build_library_rail(scorecard: dict[str, Any] | None, hypotheses: dict[str, Any] | None) -> str:
    control_plane = scorecard.get('control_plane') if isinstance(scorecard, dict) else None
    counts = control_plane.get('hypothesis_loop') if isinstance(control_plane, dict) else None

    counts_html = '<p class="unavailable-note">hypothesis counts unavailable</p>'
    if isinstance(counts, dict):
        order = ['active', 'answered', 'supported', 'refuted', 'inconclusive']
        chips = []
        for key in order:
            chips.append(
                f'<div class="count-chip count-{esc(key)}">'
                f'<span class="count-value">{esc(counts.get(key, 0))}</span>'
                f'<span class="count-label">{esc(key)}</span></div>'
            )
        counts_html = f'<div class="count-row">{"".join(chips)}</div>'

    entries = hypotheses.get('entries') if isinstance(hypotheses, dict) else None
    verdict_rows = []
    if isinstance(entries, dict):
        verdicted = [
            (key, entry) for key, entry in entries.items()
            if isinstance(entry, dict) and entry.get('verdict')
        ]
        verdicted.sort(key=lambda kv: kv[1].get('verdict_at') or '', reverse=True)
        for key, entry in verdicted[:12]:
            verdict = str(entry.get('verdict') or '').upper()
            badge_class = {
                'SUPPORTED': 'verdict-supported',
                'REFUTED': 'verdict-refuted',
                'INCONCLUSIVE': 'verdict-inconclusive',
            }.get(verdict, 'verdict-inconclusive')
            title = entry.get('title') or key
            verdict_rows.append(f'''
            <li>
              <span class="verdict-title">{esc(title)}</span>
              <span class="badge {badge_class}">{esc(verdict) or 'UNKNOWN'}</span>
            </li>
            ''')

    verdicts_html = (
        '<ul class="verdict-list verdict-list-compact">' + ''.join(verdict_rows) + '</ul>'
        if verdict_rows else '<p class="unavailable-note">no verdicts recorded yet</p>'
    )

    return f'''
    <section class="panel panel-library">
      <h2 class="panel-title">Great Library</h2>
      {counts_html}
      {verdicts_html}
    </section>
    '''


def build_left_rail(scorecard: dict[str, Any] | None, hypotheses: dict[str, Any] | None) -> str:
    return f'<aside class="rail">{build_eras_rail(scorecard)}{build_library_rail(scorecard, hypotheses)}</aside>'


def build_empire_stats_strip(scorecard: dict[str, Any] | None) -> str:
    if not isinstance(scorecard, dict):
        return unavailable_panel('Empire Stats', 'scorecard unavailable')

    loop = scorecard.get('loop') if isinstance(scorecard.get('loop'), dict) else {}
    cost = scorecard.get('cost') if isinstance(scorecard.get('cost'), dict) else {}
    heldout = scorecard.get('heldout') if isinstance(scorecard.get('heldout'), dict) else {}
    computed_at = scorecard.get('computed_at_utc')

    stats = [
        ('integrations', esc(loop.get('integrations', 'n/a'))),
        ('confirmed ratio', humanize_ratio(loop.get('confirmed_integration_ratio'))),
        ('repeat failure rate', humanize_ratio(loop.get('repeat_failure_rate'))),
        ('tokens / integration', humanize_number(cost.get('tokens_per_integration'))),
        ('held-out', f"{esc(heldout.get('passed', 'n/a'))}/{esc(heldout.get('checked', 'n/a'))}"),
    ]
    stat_html = ''.join(
        f'<div class="empire-stat"><span class="stat-label">{esc(label)}</span>'
        f'<span class="stat-value">{value}</span></div>'
        for label, value in stats
    )

    return f'''
    <header class="empire-strip">
      <div class="empire-title">EEEBOT EMPIRE</div>
      <div class="empire-stats">{stat_html}</div>
      <div class="empire-computed">computed {fmt_ts(computed_at)}</div>
    </header>
    '''


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

CSS = '''
    :root {
      color-scheme: dark;
    }
    * { box-sizing: border-box; }
    html, body {
      overflow-x: hidden;
      max-width: 100vw;
    }
    body {
      margin: 0;
      background: radial-gradient(ellipse at top, #101c30 0%, #0b1220 60%, #070c16 100%);
      color: #d8dce6;
      font-family: 'Segoe UI', Helvetica, Arial, sans-serif;
      padding: 0 0 32px 0;
    }
    h1, h2, h3, .panel-title, .empire-title {
      font-family: Georgia, 'Times New Roman', serif;
    }
    a { color: #c9a227; }

    /* --- top strip: slim, one line --- */
    .empire-strip {
      background: linear-gradient(180deg, #14213a 0%, #0d1626 100%);
      border-bottom: 2px solid #c9a227;
      padding: 8px 20px;
      display: flex;
      flex-wrap: nowrap;
      overflow-x: auto;
      align-items: center;
      gap: 18px;
    }
    .empire-title {
      color: #c9a227;
      font-size: 1.05em;
      letter-spacing: 2px;
      text-shadow: 0 0 10px rgba(201, 162, 39, 0.45);
      white-space: nowrap;
    }
    .empire-stats {
      display: flex;
      flex-wrap: nowrap;
      gap: 18px;
    }
    .empire-stat { display: flex; flex-direction: column; align-items: center; min-width: 78px; white-space: nowrap; }
    .empire-computed { color: #7d8aa3; font-size: 0.78em; white-space: nowrap; margin-left: auto; }

    .stat-label {
      text-transform: uppercase;
      font-size: 0.64em;
      letter-spacing: 1px;
      color: #8b96ad;
    }
    .stat-value {
      font-size: 1.02em;
      color: #eae3c8;
      font-weight: 600;
    }

    /* --- overall layout: fixed-width left rail + dominant canvas --- */
    .layout {
      display: flex;
      align-items: flex-start;
      gap: 16px;
      padding: 16px;
      max-width: 100vw;
    }

    .rail {
      flex: 0 0 220px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }

    .canvas-outer {
      flex: 1 1 auto;
      min-width: 0;
      overflow-x: auto;
      overflow-y: hidden;
      border: 1px solid #24314f;
      border-radius: 10px;
      background: linear-gradient(180deg, rgba(20, 33, 58, 0.55) 0%, rgba(11, 18, 32, 0.7) 100%);
      padding-bottom: 4px;
    }
    .tech-canvas { display: block; }
    .era-grid-line { stroke: #1c2740; stroke-width: 1; }
    .lane-label {
      fill: #c9a227;
      font-family: Georgia, 'Times New Roman', serif;
      font-size: 13px;
      letter-spacing: 2px;
    }
    .lane-note { fill: #6a7590; font-style: italic; font-size: 11px; }
    .lane-unavailable { fill: #6a7590; font-style: italic; font-size: 12px; }

    .panel {
      background: linear-gradient(180deg, rgba(20, 33, 58, 0.85) 0%, rgba(11, 18, 32, 0.9) 100%);
      border: 1px solid #24314f;
      border-radius: 10px;
      padding: 14px 14px 16px 14px;
      box-shadow: 0 4px 18px rgba(0, 0, 0, 0.35);
    }
    .panel-title {
      margin: 0 0 10px 0;
      color: #c9a227;
      font-size: 1.05em;
      letter-spacing: 1px;
      border-bottom: 1px solid #2c3a5c;
      padding-bottom: 6px;
    }
    .panel-unavailable {
      text-align: center;
      color: #6a7590;
    }
    .unavailable-note {
      color: #6a7590;
      font-style: italic;
    }

    /* --- eras rail: vertical medallions --- */
    .era-rail { display: flex; flex-direction: column; gap: 8px; }
    .era-medallion {
      padding: 8px 6px;
      border-radius: 10px;
      text-align: center;
      border: 2px solid #2c3a5c;
    }
    .era-lit {
      border-color: #2fd3c4;
      box-shadow: 0 0 12px rgba(47, 211, 196, 0.5);
      background: rgba(47, 211, 196, 0.08);
    }
    .era-locked {
      opacity: 0.55;
      background: rgba(90, 90, 90, 0.08);
    }
    .era-glyph { font-size: 1.2em; }
    .era-name { font-size: 0.7em; margin-top: 4px; color: #c7cfe0; }
    .era-caption {
      text-align: center;
      margin-top: 8px;
      color: #c9a227;
      letter-spacing: 1px;
      font-family: Georgia, serif;
      font-size: 0.85em;
    }

    /* --- direction boxes (Lane A / RESEARCH) --- */
    .dir-box {
      width: 100%;
      height: 100%;
      background: rgba(15, 24, 42, 0.94);
      border: 1px solid #2c3a5c;
      border-radius: 8px;
      padding: 10px;
      position: relative;
      font-family: 'Segoe UI', Helvetica, Arial, sans-serif;
      color: #d8dce6;
    }
    .dir-box-current {
      border-color: #c9a227;
      box-shadow: 0 0 16px rgba(201, 162, 39, 0.65);
    }
    .dir-box-plateaued { opacity: 0.7; border-color: #6d3232; }
    .dir-box-dim { opacity: 0.55; }
    .dir-box-head { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
    .dir-glyph { font-size: 1.1em; color: #c9a227; }
    .dir-name { font-weight: 600; color: #eae3c8; font-size: 0.92em; }
    .dir-lever {
      font-variant: small-caps;
      letter-spacing: 0.5px;
      color: #8b96ad;
      font-size: 0.76em;
      margin-bottom: 6px;
    }
    .ribbon {
      position: absolute;
      top: -8px;
      right: -8px;
      background: #c9a227;
      color: #1a1406;
      font-size: 0.6em;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 4px;
      transform: rotate(4deg);
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.4);
    }
    .cooldown { font-size: 0.68em; color: #8b96ad; margin-top: 4px; }

    .dir-elbow { fill: none; stroke: #2fd3c4; stroke-width: 2.2; opacity: 0.9; }
    .mint-elbow { fill: none; stroke: #c9a227; stroke-width: 2; opacity: 0.85; }
    .mint-glyph { fill: #c9a227; font-size: 15px; }

    /* --- evolution boxes (Lane B / WORLD HISTORY) --- */
    .evo-box {
      width: 100%;
      height: 100%;
      display: flex;
      align-items: center;
      gap: 4px;
      background: rgba(15, 24, 42, 0.9);
      border: 1.5px solid #4a5878;
      border-radius: 7px;
      padding: 0 8px;
      font-family: 'Consolas', 'Courier New', monospace;
      font-size: 11px;
      color: #c7cfe0;
    }
    .evo-box-current {
      border-color: #c9a227;
      box-shadow: 0 0 12px rgba(201, 162, 39, 0.75);
      background: rgba(47, 211, 196, 0.12);
    }
    .evo-box-abandoned { opacity: 0.45; }
    .evo-diamond { color: #c9a227; font-weight: 700; }
    .evo-box-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .evo-elbow { fill: none; stroke: #3a4a6e; stroke-width: 1.6; }
    .evo-fallback { color: #d8dce6; }

    .badge {
      display: inline-block;
      font-size: 0.64em;
      font-weight: 700;
      letter-spacing: 0.5px;
      padding: 2px 7px;
      border-radius: 4px;
      margin-bottom: 6px;
    }
    .badge-researching { background: rgba(201, 162, 39, 0.22); color: #c9a227; border: 1px solid #c9a227; }
    .badge-available { background: rgba(139, 150, 173, 0.18); color: #b7c0d4; border: 1px solid #4a5878; }
    .badge-plateaued { background: rgba(178, 58, 58, 0.15); color: #d97b7b; border: 1px solid #6d3232; }
    .verdict-supported { background: rgba(201, 162, 39, 0.18); color: #c9a227; border: 1px solid #c9a227; }
    .verdict-refuted { background: rgba(178, 58, 58, 0.18); color: #d97b7b; border: 1px solid #6d3232; }
    .verdict-inconclusive { background: rgba(139, 150, 173, 0.18); color: #b7c0d4; border: 1px solid #4a5878; }

    .spark { margin-bottom: 2px; }
    .spark-top, .spark-bottom {
      display: flex;
      align-items: flex-end;
      gap: 2px;
      height: 14px;
    }
    .spark-bottom { align-items: flex-start; }
    .spark-baseline { border-top: 1px dashed #3a4a6e; }
    .bar { width: 5px; border-radius: 1px; display: block; }
    .bar-pos { background: #c9a227; }
    .bar-neg { background: #b23a3a; }
    .bar-placeholder { height: 0; background: transparent; }
    .spark-empty { color: #6a7590; font-style: italic; font-size: 0.74em; margin-bottom: 4px; }
    .spark-mean { font-size: 0.7em; margin-top: 2px; }
    .mean-pos { color: #c9a227; }
    .mean-neg { color: #d97b7b; }

    ul.verdict-list, ul.timeline-list {
      list-style: none;
      margin: 0;
      padding: 0;
      max-height: 220px;
      overflow-y: auto;
    }
    ul.verdict-list li, ul.timeline-list li {
      padding: 5px 0;
      border-bottom: 1px solid #1c2740;
      font-size: 0.78em;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }
    .verdict-list-compact .verdict-title { color: #eae3c8; flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; }
    .timeline-sha { font-family: 'Consolas', 'Courier New', monospace; color: #2fd3c4; }
    .timeline-branch { color: #b7c0d4; font-size: 0.9em; }
    .timeline-ts { color: #6a7590; font-size: 0.85em; }

    .count-row { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
    .count-chip {
      display: flex;
      flex-direction: column;
      align-items: center;
      min-width: 56px;
      padding: 6px 6px;
      border-radius: 6px;
      border: 1px solid #2c3a5c;
      background: rgba(15, 24, 42, 0.7);
    }
    .count-value { font-size: 1.05em; font-weight: 700; color: #eae3c8; }
    .count-label { font-size: 0.6em; text-transform: uppercase; color: #8b96ad; letter-spacing: 0.5px; }
    .count-supported .count-value { color: #c9a227; }
    .count-refuted .count-value { color: #d97b7b; }

    footer.page-footer {
      text-align: center;
      color: #4f5a76;
      font-size: 0.78em;
      margin-top: 20px;
      padding-top: 14px;
    }
'''

PAGE_TEMPLATE = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>eeebot Tech Tree</title>
<style>{css}</style>
</head>
<body>
{empire_strip}
<div class="layout">
{rail}
{canvas}
</div>
<footer class="page-footer">generated {generated_at} UTC &middot; host {host} &middot; newest source {source_age}{error_note}</footer>
</body>
</html>
'''


def render_page(data: dict[str, Any], host: str, generated_at: str | None = None) -> str:
    """Build the full self-contained HTML page from the combined state dict.

    `data` is expected to have the same shape as the JSON produced by
    REMOTE_READER_SCRIPT: keys portfolio / scorecard / evolution_tree /
    hypotheses / ledger_tail, any of which may be None. Never raises on
    missing/malformed data -- every panel fails soft to an "unavailable"
    strip instead.
    """
    if generated_at is None:
        # UTC, explicitly (issue #27): a reader with no idea what timezone
        # "local time" meant had no way to judge staleness at a glance.
        generated_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    portfolio = data.get('portfolio')
    scorecard = data.get('scorecard')
    evolution_tree = data.get('evolution_tree')
    hypotheses = data.get('hypotheses')
    ledger_tail = data.get('ledger_tail')

    error_note = ''
    if data.get('_error'):
        # Fixed, generic string only (issue #27 review, blocker B2): the raw
        # `_error` text can embed a host filesystem path (read_local_state's
        # "state root not found or not a directory: <path>") or remote
        # ssh/API stderr, and this page is published publicly. The real
        # message belongs on stderr at the call site (see main() below and
        # techtree_autopublish.run()), never inside HTML that leaves the host.
        # "see host logs" was misleading (issue #27 review round 3, note
        # N8): `_error` can be an ssh transport failure produced on the
        # OPERATOR'S workstation running this viewer remotely, not on the
        # authority host -- "host logs" reads as an instruction to go look
        # at the wrong machine. Word it so it is honest for both the local
        # (autopublisher, running on the host) and remote (operator's own
        # machine) readers.
        error_note = ' &middot; fetch note: state read failed (details in the publisher\'s logs)'

    # Age of the newest source file this page was built from (issue #27) --
    # the second half of the freshness marker. Never fabricated: if no
    # reader (local or remote) could establish a real mtime, say so plainly
    # instead of printing a 0 or omitting the field silently.
    age_seconds = data.get('_newest_source_age_seconds')
    if isinstance(age_seconds, (int, float)):
        source_age = f'{humanize_age(age_seconds)} old'
    else:
        source_age = 'age unknown'

    empire_strip = build_empire_stats_strip(scorecard)
    rail_html = build_left_rail(scorecard, hypotheses)
    canvas_html = build_tech_canvas(portfolio, ledger_tail, evolution_tree)

    return PAGE_TEMPLATE.format(
        css=CSS,
        empire_strip=empire_strip,
        rail=rail_html,
        canvas=canvas_html,
        generated_at=esc(generated_at),
        host=esc(host),
        source_age=esc(source_age),
        error_note=error_note,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='eeepc', help='SSH host alias for the eeepc authority host (default: eeepc)')
    parser.add_argument('--out', default='techtree.html', help='output HTML file path (default: techtree.html)')
    parser.add_argument('--open', action='store_true', help='open the rendered page in the default browser')
    parser.add_argument(
        '--local', action='store_true',
        help='read state directly from --state-root instead of SSHing to --host '
             '(issue #27: for running the viewer on the authority host itself, '
             'e.g. from the autopublisher)',
    )
    parser.add_argument(
        '--state-root', default=STATE_ROOT,
        help=f'local state root to read when --local is set (default: {STATE_ROOT})',
    )
    parser.add_argument(
        '--publish', action='store_true',
        help='also publish the page to GitHub Pages (gh-pages branch of '
             f'{PUBLISH_REPO} as index.html, via the authenticated `gh` CLI); '
             'the page then lives at ' + PUBLISH_URL,
    )
    return parser.parse_args(argv)


# ─── GitHub Pages publishing (optional, --publish) ───────────────────────────
#
# Simplest possible free hosting: the repo is already PUBLIC on GitHub, so a
# `gh-pages` branch + GitHub Pages serves the snapshot with zero new accounts,
# tokens or services. Publishing goes through the GitHub *contents API* via the
# already-authenticated `gh` CLI — it never touches the local git clone (which
# may be dirty or on a diverged history) and needs no checkout of gh-pages.
# NOTE: the repo (and therefore the published page — loop metrics + hypothesis
# titles) is public by design; do not add secrets to the page.

PUBLISH_REPO = 'ozand/eeebot-ops-dashboard'
PUBLISH_BRANCH = 'gh-pages'
PUBLISH_FILE = 'index.html'
PUBLISH_URL = f'https://ozand.github.io/eeebot-ops-dashboard/'


def _gh(args: list[str], input_text: 'str | None' = None) -> subprocess.CompletedProcess:
    """Run `gh` and return its CompletedProcess. Never raises (issue #27
    review, blocker B3): subprocess.run(timeout=...) can raise
    subprocess.TimeoutExpired (gh hangs), and a missing `gh` binary raises
    FileNotFoundError (an OSError subclass) -- both are caught here and
    turned into a synthetic non-zero CompletedProcess instead of an
    uncaught traceback, so every caller in publish_to_pages can keep
    treating "returncode != 0" as the one and only failure signal."""
    try:
        return subprocess.run(
            ['gh'] + args, capture_output=True, text=True, timeout=60,
            input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        # Touch neither str(exc) nor exc.cmd. TimeoutExpired.__str__ is
        # "Command '%s' timed out after %s seconds" % (self.cmd, self.timeout)
        # -- so it does not include output/stderr, but the FIRST %s is cmd,
        # i.e. str(exc) already interpolates the whole argv list today. This
        # comment said the opposite for one round (issue #27 review: round 3
        # blamed captured output, round 4 corrected the mechanism to exc.cmd
        # but then wrongly absolved str(exc); round 5 caught that). Both leak.
        #
        # What leaks: subprocess.run is called as ['gh'] + args, and for the
        # contents-PUT call args carries '-f content=<base64 page body>' as a
        # single element. So either path would dump the whole page into the
        # journal. Build the message from a known-short slice of args instead.
        #
        # args[0] is always 'api' here (every caller in publish_to_pages
        # invokes _gh(['api', ...])), so reporting args[0] alone can never
        # distinguish which of the several `gh api` calls in one
        # publish_to_pages run timed out (issue #27 review round 4, item
        # G). Report the endpoint instead: args[1], or args[3] when a
        # leading '-X <verb>' flag pair pushes the endpoint back one slot
        # (the POST/PUT calls above).
        rest = args[1:]
        if rest[:1] == ['-X']:
            rest = rest[2:]
        endpoint = rest[0] if rest else (args[0] if args else '')
        return subprocess.CompletedProcess(
            args=['gh'] + args, returncode=1, stdout='',
            stderr=f'{exc.__class__.__name__}: gh api {endpoint} timed out',
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return subprocess.CompletedProcess(
            args=['gh'] + args, returncode=1, stdout='',
            stderr=f'{exc.__class__.__name__}: {exc}',
        )


def publish_to_pages(html_out: str) -> int:
    """Upsert the rendered page as ``index.html`` on the ``gh-pages`` branch
    and make sure GitHub Pages is enabled for that branch. Returns 0 on
    success, 1 on any failure (with a message on stderr). Uses only the
    ``gh`` CLI (already authenticated on the operator machine)."""
    import base64

    content_b64 = base64.b64encode(html_out.encode('utf-8')).decode('ascii')

    # Branch may not exist yet: the contents API creates it only if the repo
    # has it; bootstrap it from the default branch's HEAD when absent.
    branch_probe = _gh(['api', f'repos/{PUBLISH_REPO}/branches/{PUBLISH_BRANCH}'])
    if branch_probe.returncode != 0:
        head = _gh(['api', f'repos/{PUBLISH_REPO}/git/ref/heads/master',
                    '--jq', '.object.sha'])
        if head.returncode != 0:
            print(f'publish: cannot resolve master HEAD: {head.stderr.strip()[:200]}',
                  file=sys.stderr)
            return 1
        made = _gh(['api', '-X', 'POST', f'repos/{PUBLISH_REPO}/git/refs',
                    '-f', f'ref=refs/heads/{PUBLISH_BRANCH}',
                    '-f', f'sha={head.stdout.strip()}'])
        if made.returncode != 0:
            print(f'publish: cannot create {PUBLISH_BRANCH}: {made.stderr.strip()[:200]}',
                  file=sys.stderr)
            return 1

    # Upsert index.html (need the existing blob sha for an update).
    sha_probe = _gh(['api',
                     f'repos/{PUBLISH_REPO}/contents/{PUBLISH_FILE}?ref={PUBLISH_BRANCH}',
                     '--jq', '.sha'])
    put_args = ['api', '-X', 'PUT', f'repos/{PUBLISH_REPO}/contents/{PUBLISH_FILE}',
                '-f', 'message=techtree snapshot (techtree_viewer --publish)',
                '-f', f'branch={PUBLISH_BRANCH}',
                '-f', f'content={content_b64}']
    if sha_probe.returncode == 0 and sha_probe.stdout.strip():
        put_args += ['-f', f'sha={sha_probe.stdout.strip()}']
    put = _gh(put_args)
    if put.returncode != 0:
        print(f'publish: contents PUT failed: {put.stderr.strip()[:300]}', file=sys.stderr)
        return 1

    # Enable Pages on gh-pages if not already (idempotent; 409 = already on).
    pages = _gh(['api', f'repos/{PUBLISH_REPO}/pages'])
    if pages.returncode != 0:
        enable = _gh(['api', '-X', 'POST', f'repos/{PUBLISH_REPO}/pages',
                      '--input', '-'],
                     input_text='{"source":{"branch":"gh-pages","path":"/"}}')
        if enable.returncode != 0 and '409' not in (enable.stderr or ''):
            print(f'publish: Pages enable failed (page pushed anyway): '
                  f'{enable.stderr.strip()[:200]}', file=sys.stderr)

    print(f'published: {PUBLISH_URL} (Pages может обновляться ~минуту)')
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.local:
        data = read_local_state(args.state_root)
    else:
        data = fetch_remote_state(args.host)
    html_out = render_page(data, args.host)

    out_path = Path(args.out)
    out_path.write_text(html_out, encoding='utf-8')
    print(f'wrote {out_path.resolve()}')

    if data.get('_error'):
        print(f'note: {data["_error"]}', file=sys.stderr)

    if args.open:
        webbrowser.open(out_path.resolve().as_uri())

    if args.publish:
        return publish_to_pages(html_out)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
