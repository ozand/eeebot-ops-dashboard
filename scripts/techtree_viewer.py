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
INSTANCE_REPO = '/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving'

# Ledger-tail filter, mirrored inside REMOTE_READER_SCRIPT's own copy of
# these constants.
LEDGER_PHASES = {
    'started', 'outcome', 'gate', 'proposer_reject', 'dedup', 'idle',
    'evolution_tree', 'tech_tree', 'hypothesis',
}
LEDGER_TAIL_LIMIT = 200
LEDGER_SCAN_WINDOW = 5000

# Read every source fail-soft, from a single remote python3 process fed over
# stdin. This keeps the whole fetch to exactly one SSH round-trip and avoids
# leaving any temp files behind on the remote host.
REMOTE_READER_SCRIPT = r'''
import json
import os
import subprocess

STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"
INSTANCE_REPO = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"
LEDGER_PHASES = {
    "started", "outcome", "gate", "proposer_reject", "dedup", "idle",
    "evolution_tree", "tech_tree", "hypothesis",
}
LEDGER_TAIL_LIMIT = 200
LEDGER_SCAN_WINDOW = 5000

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


def read_file_text(relpath):
    path = os.path.join(INSTANCE_REPO, relpath)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        _mtimes.append(os.path.getmtime(path))
        return content
    except Exception:
        return None


def extract_git_titles():
    titles = {}
    cycle_files = {}
    error = None
    if not os.path.isdir(INSTANCE_REPO):
        return titles, cycle_files, error
    try:
        cmd = ["git", "-C", INSTANCE_REPO, "-c", f"safe.directory={INSTANCE_REPO}", "log", "--first-parent", "-n", "60", "--format=%H %s"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            err_msg = (res.stderr or res.stdout or "").strip()
            short_reason = f"git log failed (exit {res.returncode}): {err_msg}"
            if len(short_reason) > 200:
                short_reason = short_reason[:197] + "..."
            return titles, cycle_files, short_reason
        for line in res.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            commit_sha, subject = parts
            if "merge: integrate selfevo/cycle-" in subject:
                cycle_part = subject.split("merge: integrate selfevo/", 1)[-1].strip()
                if cycle_part.startswith("cycle-cycle-"):
                    norm_cycle_id = "cycle-" + cycle_part[len("cycle-cycle-"):]
                else:
                    norm_cycle_id = cycle_part
                
                try:
                    cmd_title = ["git", "-C", INSTANCE_REPO, "-c", f"safe.directory={INSTANCE_REPO}", "log", f"{commit_sha}^2", "-n", "5", "--format=%s"]
                    res_title = subprocess.run(cmd_title, capture_output=True, text=True, timeout=5)
                    if res_title.returncode == 0:
                        for t_line in res_title.stdout.strip().splitlines():
                            t_line = t_line.strip()
                            if not t_line:
                                continue
                            if t_line.startswith("chore:") or t_line.startswith("merge:"):
                                continue
                            titles[cycle_part] = t_line
                            if norm_cycle_id != cycle_part:
                                titles[norm_cycle_id] = t_line
                            break
                except Exception:
                    pass

                try:
                    cmd_diff = ["git", "-C", INSTANCE_REPO, "-c", f"safe.directory={INSTANCE_REPO}", "diff", "--name-only", f"{commit_sha}^1", commit_sha]
                    res_diff = subprocess.run(cmd_diff, capture_output=True, text=True, timeout=5)
                    if res_diff.returncode == 0:
                        diff_files = [f.strip() for f in res_diff.stdout.strip().splitlines() if f.strip()][:10]
                        cycle_files[cycle_part] = diff_files
                        if norm_cycle_id != cycle_part:
                            cycle_files[norm_cycle_id] = diff_files
                except Exception:
                    pass
    except Exception as exc:
        short_reason = f"git log exception: {exc.__class__.__name__}: {exc}"
        if len(short_reason) > 200:
            short_reason = short_reason[:197] + "..."
        return titles, cycle_files, short_reason
    return titles, cycle_files, None


_cycle_titles, _cycle_files, _cycle_titles_error = extract_git_titles()

result = {
    "portfolio": read_json("tech_tree/portfolio.json"),
    "scorecard": read_json("scorecard/latest.json"),
    "evolution_tree": read_json("evolution/tree.json"),
    "hypotheses": read_json("hypotheses/lifecycle.json"),
    "ledger_tail": read_ledger_tail("ledger/cycles.jsonl"),
    "demand_rotation": read_json("demand/rotation.json"),
    "demand_completed": read_json("demand/completed.json"),
    "skill_reads": read_json("skill_fitness/reads.json"),
    "goal_text": read_json("goals/goal_text.json"),
    "agents_md": read_file_text("AGENTS.md"),
    "cycle_titles": _cycle_titles,
    "cycle_files": _cycle_files,
    "cycle_titles_error": _cycle_titles_error,
    "_source_mtimes": _mtimes,
}
print(json.dumps(result))
'''.lstrip('\n')


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
        'demand_rotation': None,
        'demand_completed': None,
        'skill_reads': None,
        'goal_text': None,
        'agents_md': None,
        'cycle_titles': None,
        'cycle_files': {},
        'cycle_titles_error': None,
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

    mtimes = data.pop('_source_mtimes', None)
    if isinstance(mtimes, list) and mtimes:
        data['_newest_source_age_seconds'] = max(0.0, time.time() - max(mtimes))
    return data


def extract_git_titles_local(repo_root: Path) -> tuple[dict[str, str], dict[str, list[str]], str | None]:
    titles: dict[str, str] = {}
    cycle_files: dict[str, list[str]] = {}
    if not repo_root.is_dir():
        return titles, cycle_files, None
    try:
        repo_str = str(repo_root)
        cmd = ['git', '-C', repo_str, '-c', f'safe.directory={repo_str}', 'log', '--first-parent', '-n', '60', '--format=%H %s']
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            err_msg = (res.stderr or res.stdout or '').strip()
            short_reason = f'git log failed (exit {res.returncode}): {err_msg}'
            if len(short_reason) > 200:
                short_reason = short_reason[:197] + '...'
            return titles, cycle_files, short_reason
        for line in res.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(' ', 1)
            if len(parts) != 2:
                continue
            commit_sha, subject = parts
            if 'merge: integrate selfevo/cycle-' in subject:
                cycle_part = subject.split('merge: integrate selfevo/', 1)[-1].strip()
                if cycle_part.startswith('cycle-cycle-'):
                    norm_cycle_id = 'cycle-' + cycle_part[len('cycle-cycle-'):]
                else:
                    norm_cycle_id = cycle_part

                try:
                    cmd_title = ['git', '-C', repo_str, '-c', f'safe.directory={repo_str}', 'log', f'{commit_sha}^2', '-n', '5', '--format=%s']
                    res_title = subprocess.run(cmd_title, capture_output=True, text=True, timeout=5)
                    if res_title.returncode == 0:
                        for t_line in res_title.stdout.strip().splitlines():
                            t_line = t_line.strip()
                            if not t_line:
                                continue
                            if t_line.startswith('chore:') or t_line.startswith('merge:'):
                                continue
                            titles[cycle_part] = t_line
                            if norm_cycle_id != cycle_part:
                                titles[norm_cycle_id] = t_line
                            break
                except Exception:
                    pass

                try:
                    cmd_diff = ['git', '-C', repo_str, '-c', f'safe.directory={repo_str}', 'diff', '--name-only', f'{commit_sha}^1', commit_sha]
                    res_diff = subprocess.run(cmd_diff, capture_output=True, text=True, timeout=5)
                    if res_diff.returncode == 0:
                        diff_files = [f.strip() for f in res_diff.stdout.strip().splitlines() if f.strip()][:10]
                        cycle_files[cycle_part] = diff_files
                        if norm_cycle_id != cycle_part:
                            cycle_files[norm_cycle_id] = diff_files
                except Exception:
                    pass
    except Exception as exc:
        short_reason = f'git log exception: {exc.__class__.__name__}: {exc}'
        if len(short_reason) > 200:
            short_reason = short_reason[:197] + '...'
        return titles, cycle_files, short_reason
    return titles, cycle_files, None


def read_local_state(state_root: str, instance_repo: str | None = None) -> dict[str, Any]:
    """Read all state sources directly from `state_root` -- no SSH."""
    empty: dict[str, Any] = {
        'portfolio': None,
        'scorecard': None,
        'evolution_tree': None,
        'hypotheses': None,
        'ledger_tail': None,
        'demand_rotation': None,
        'demand_completed': None,
        'skill_reads': None,
        'goal_text': None,
        'agents_md': None,
        'cycle_titles': None,
        'cycle_files': {},
        'cycle_titles_error': None,
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
        except Exception:  # noqa: BLE001 - fail-soft per source
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

    repo_path = Path(instance_repo) if instance_repo else root.parent / 'eeebot-self-evolving'
    agents_text = None
    if repo_path.is_dir():
        agents_file = repo_path / 'AGENTS.md'
        try:
            with agents_file.open('r', encoding='utf-8', errors='replace') as fh:
                agents_text = fh.read()
            mtimes.append(agents_file.stat().st_mtime)
        except Exception:
            pass

    titles, cycle_files, titles_error = extract_git_titles_local(repo_path)

    data: dict[str, Any] = {
        'portfolio': read_json('tech_tree/portfolio.json'),
        'scorecard': read_json('scorecard/latest.json'),
        'evolution_tree': read_json('evolution/tree.json'),
        'hypotheses': read_json('hypotheses/lifecycle.json'),
        'ledger_tail': read_ledger_tail('ledger/cycles.jsonl'),
        'demand_rotation': read_json('demand/rotation.json'),
        'demand_completed': read_json('demand/completed.json'),
        'skill_reads': read_json('skill_fitness/reads.json'),
        'goal_text': read_json('goals/goal_text.json'),
        'agents_md': agents_text,
        'cycle_titles': titles,
        'cycle_files': cycle_files,
        'cycle_titles_error': titles_error,
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

EVO_BOX_W = 180
EVO_BOX_H = 64
EVO_ROW_H = 80
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


def _evo_box_html(
    sha: str,
    node: dict[str, Any],
    is_current: bool,
    is_abandoned: bool,
    switch_marked: bool,
    x: float,
    y: float,
    task_titles: dict[str, str] | None = None,
    portfolio: dict[str, Any] | None = None,
) -> str:
    branch = str(node.get('branch') or '')
    tail = branch.rsplit('/', 1)[-1] if branch else ''
    cycle_id = str(node.get('cycle_id') or '')
    if not cycle_id and 'cycle-' in tail:
        cycle_id = tail

    # Resolve task title
    title = ''
    if task_titles:
        if cycle_id and cycle_id in task_titles:
            title = task_titles[cycle_id]
        elif sha in task_titles:
            title = task_titles[sha]
        elif short_sha(sha) in task_titles:
            title = task_titles[short_sha(sha)]

    display_title = title if title else (cycle_id if cycle_id else (tail if tail else short_sha(sha)))
    label = esc(display_title)
    marker = ' &#8634;' if switch_marked else ''

    fitness = node.get('fitness')
    fitness_line = ''
    if isinstance(fitness, dict):
        parts = []
        if 'reward' in fitness:
            r = fitness.get('reward')
            if isinstance(r, (int, float)):
                parts.append(f'r:{r:.2f}')
            else:
                parts.append('r:—')
        if 'integrations' in fitness:
            ints = fitness.get('integrations')
            if ints is not None:
                parts.append(f'integrations:{ints}')
        if parts:
            fitness_line = f'<div class="evo-fitness">{" ".join(parts)}</div>'

    dir_badge = ''
    dir_name = node.get('direction') or node.get('research_direction')
    if not dir_name and isinstance(portfolio, dict):
        current_dir = portfolio.get('current')
        if current_dir:
            dir_name = current_dir
    if dir_name:
        dir_badge = f'<span class="evo-dir-badge">{esc(dir_name)}</span>'

    box_class = 'evo-box'
    if is_current:
        box_class += ' evo-box-current'
    elif is_abandoned:
        box_class += ' evo-box-abandoned'

    diamond = '<span class="evo-diamond">&#9672;</span>' if is_current else ''
    tooltip = esc(f'{short_sha(sha)} | {title or display_title} | {branch}')

    body = (
        f'<div class="{box_class}" title="{tooltip}" id="node-{esc(short_sha(sha))}">'
        f'<div class="evo-header">{diamond}<span class="evo-box-label">{label}{marker}</span></div>'
        f'<div class="evo-meta">{dir_badge}<span class="evo-sha">{esc(short_sha(sha))}</span></div>'
        f'{fitness_line}'
        '</div>'
    )
    return f'<foreignObject x="{x:.0f}" y="{y:.0f}" width="{EVO_BOX_W}" height="{EVO_BOX_H}">{body}</foreignObject>'


def _lane_b_layout(
    evolution_tree: dict[str, Any] | None,
    task_titles: dict[str, str] | None = None,
    portfolio: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        boxes.append(_evo_box_html(sha, node, is_current, is_abandoned, sha in switch_shas, x, y, task_titles, portfolio))

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


def build_tech_canvas(
    portfolio: dict[str, Any] | None,
    ledger_tail: list[Any] | None,
    evolution_tree: dict[str, Any] | None,
    task_titles: dict[str, str] | None = None,
) -> str:
    """The main area: ONE wide horizontally-scrollable SVG canvas holding
    both lanes on a shared left-to-right time axis. Fail-soft per lane."""
    lane_a = _lane_a_layout(portfolio, ledger_tail)
    lane_b = _lane_b_layout(evolution_tree, task_titles=task_titles, portfolio=portfolio)

    canvas_width = MIN_CANVAS_W
    if lane_a.get('available') and 'width' in lane_a:
        canvas_width = max(canvas_width, lane_a['width'])
    if lane_b.get('available') and 'width' in lane_b:
        canvas_width = max(canvas_width, lane_b['width'])

    grid_xs: set[float] = set()
    groups: list[str] = []
    y_cursor = CANVAS_MARGIN_Y

    # --- Lane A: RESEARCH -----------------------------------------------
    label_a = '<text x="10" y="14" class="lane-label">RESEARCH DIRECTIONS</text>'
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

    # --- Lane B: EVOLUTION LINEAGE ---------------------------------------
    label_b = '<text x="10" y="14" class="lane-label">EVOLUTION LINEAGE (DGM)</text>'
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

def build_now_panel(
    portfolio: dict[str, Any] | None,
    evolution_tree: dict[str, Any] | None,
    demand_rotation: dict[str, Any] | None,
    demand_completed: dict[str, Any] | None,
    task_titles: dict[str, str] | None = None,
    ledger_tail: list[dict[str, Any]] | None = None,
) -> str:
    # 1. Active research direction
    dir_html = '<p class="unavailable-note">research direction unavailable</p>'
    if isinstance(portfolio, dict):
        current_dir = portfolio.get('current') or 'none'
        nodes = portfolio.get('nodes') if isinstance(portfolio.get('nodes'), dict) else {}
        node = nodes.get(current_dir) if isinstance(nodes, dict) else {}
        if isinstance(node, dict):
            status = node.get('status') or 'unknown'
            lever_metric = node.get('lever_metric') or 'n/a'
            direction_type = node.get('direction') or 'n/a'
            last_val = node.get('last_lever_value')
            val_str = f' (last: {esc(last_val)})' if last_val is not None else ''
            dir_html = (
                f'<div class="now-item"><span class="now-label">Direction:</span> '
                f'<span class="badge badge-researching">{esc(current_dir)}</span> '
                f'<span class="badge badge-available">{esc(status)}</span> '
                f'<span class="now-detail">lever: <strong>{esc(lever_metric)}</strong> &middot; '
                f'aim: <em>{esc(direction_type)}</em>{val_str}</span></div>'
            )
        else:
            dir_html = (
                f'<div class="now-item"><span class="now-label">Direction:</span> '
                f'<span class="badge badge-researching">{esc(current_dir)}</span></div>'
            )

    # 2. Current / Last Cycle
    cycle_html = '<p class="unavailable-note">cycle info unavailable</p>'
    current_sha = evolution_tree.get('current_sha') if isinstance(evolution_tree, dict) else None
    nodes_tree = evolution_tree.get('nodes') if isinstance(evolution_tree, dict) else {}
    last_node = nodes_tree.get(current_sha) if isinstance(nodes_tree, dict) and current_sha else None
    
    # Try finding latest cycle from tree or ledger
    latest_cycle_id = None
    if isinstance(last_node, dict) and last_node.get('cycle_id'):
        latest_cycle_id = str(last_node.get('cycle_id'))
    elif isinstance(ledger_tail, list) and ledger_tail:
        for entry in reversed(ledger_tail):
            if isinstance(entry, dict) and entry.get('cycle_id'):
                latest_cycle_id = str(entry.get('cycle_id'))
                break

    if latest_cycle_id:
        titles_map = task_titles if isinstance(task_titles, dict) else {}
        title = titles_map.get(latest_cycle_id) or titles_map.get(latest_cycle_id.replace('cycle-', '')) or latest_cycle_id
        sha_str = f' [{esc(current_sha[:8])}]' if current_sha else ''
        cycle_html = (
            f'<div class="now-item"><span class="now-label">Latest Cycle:</span> '
            f'<strong>{esc(title)}</strong> '
            f'<span class="now-sub">({esc(latest_cycle_id)}{sha_str})</span></div>'
        )

    # 3. Demand snapshot
    demand_html = '<p class="unavailable-note">demand snapshot unavailable</p>'
    served_items = []
    if isinstance(demand_rotation, dict):
        served = demand_rotation.get('served')
        if isinstance(served, dict):
            for gid, ts in list(served.items())[-5:]:
                served_items.append(f'<span class="demand-chip served" title="served: {esc(ts)}">{esc(gid)}</span>')

    completed_items = []
    if isinstance(demand_completed, dict):
        entries = demand_completed.get('entries')
        if isinstance(entries, dict):
            for gid, cinfo in list(entries.items())[-5:]:
                cid = cinfo.get('cycle_id', '') if isinstance(cinfo, dict) else ''
                completed_items.append(f'<span class="demand-chip completed" title="cycle: {esc(cid)}">{esc(gid)}</span>')

    if served_items or completed_items:
        s_part = f'<div class="demand-subgroup"><span class="demand-sublabel">Served:</span> {" ".join(served_items) if served_items else "<em>none</em>"}</div>'
        c_part = f'<div class="demand-subgroup"><span class="demand-sublabel">Completed:</span> {" ".join(completed_items) if completed_items else "<em>none</em>"}</div>'
        demand_html = f'<div class="now-demand-grid">{s_part}{c_part}</div>'

    return f'''
    <section class="panel panel-now">
      <h2 class="panel-title">Now / Active Focus</h2>
      <div class="now-content">
        {dir_html}
        {cycle_html}
        <div class="now-item">
          <span class="now-label">Demand Queue:</span>
          {demand_html}
        </div>
      </div>
    </section>
    '''


def build_cycle_feed(
    ledger_tail: list[dict[str, Any]] | None,
    demand_completed: dict[str, Any] | None = None,
    task_titles: dict[str, str] | None = None,
    evolution_tree: dict[str, Any] | None = None,
    cycle_files: dict[str, list[str]] | None = None,
) -> str:
    if not isinstance(ledger_tail, list):
        return unavailable_panel('Cycle Feed', 'ledger unavailable')

    # Group ledger phases by cycle_id
    cycles_dict: dict[str, list[dict[str, Any]]] = {}
    for entry in ledger_tail:
        if not isinstance(entry, dict):
            continue
        cid = entry.get('cycle_id')
        if not cid:
            continue
        cid = str(cid)
        if cid not in cycles_dict:
            cycles_dict[cid] = []
        cycles_dict[cid].append(entry)

    if not cycles_dict:
        return f'''
        <section class="panel panel-feed">
          <h2 class="panel-title">Cycle Feed</h2>
          <p class="unavailable-note">no cycle entries in ledger</p>
        </section>
        '''

    # Map completed demand by cycle_id
    demand_by_cycle: dict[str, dict[str, Any]] = {}
    if isinstance(demand_completed, dict):
        entries = demand_completed.get('entries')
        if isinstance(entries, dict):
            for gid, cinfo in entries.items():
                if isinstance(cinfo, dict) and cinfo.get('cycle_id'):
                    demand_by_cycle[str(cinfo.get('cycle_id'))] = cinfo

    # Map evolution tree nodes by cycle_id
    tree_by_cycle: dict[str, dict[str, Any]] = {}
    if isinstance(evolution_tree, dict):
        nodes = evolution_tree.get('nodes')
        if isinstance(nodes, dict):
            for sha, node in nodes.items():
                if isinstance(node, dict) and node.get('cycle_id'):
                    tree_by_cycle[str(node.get('cycle_id'))] = node

    titles_map = task_titles if isinstance(task_titles, dict) else {}

    # Build rows (up to 50 latest cycles)
    rows = []
    cycle_items = list(cycles_dict.items())
    # Take latest 50
    for cid, phases in reversed(cycle_items[-50:]):
        # Determine task title
        title = titles_map.get(cid) or titles_map.get(cid.replace('cycle-', '')) or cid
        
        # Outcome derivation from phases
        outcome_kind = 'in_progress'
        outcome_label = 'running'
        badge_class = 'badge-available'
        reason = ''
        files_changed = []
        metric_delta = ''
        ts_val = ''

        # Check demand and cycle_files for files_changed
        all_files: list[str] = []
        if cid in demand_by_cycle:
            fc = demand_by_cycle[cid].get('files_changed')
            if isinstance(fc, list):
                for f in fc:
                    f_str = str(f)
                    if f_str not in all_files:
                        all_files.append(f_str)

        if isinstance(cycle_files, dict):
            cf = cycle_files.get(cid) or cycle_files.get(cid.replace('cycle-', ''))
            if isinstance(cf, list):
                for f in cf:
                    f_str = str(f)
                    if f_str not in all_files:
                        all_files.append(f_str)

        files_changed = all_files

        # Scan phases
        for p in phases:
            if not ts_val and p.get('ts'):
                ts_val = str(p.get('ts'))
            phase_name = p.get('phase')
            if phase_name == 'started':
                pass
            elif phase_name == 'gate':
                gate_passed = p.get('passed') or p.get('status') == 'passed' or p.get('smoke_passed')
                if not gate_passed and p.get('reason'):
                    outcome_kind = 'gate_blocked'
                    reason = str(p.get('reason'))
            elif phase_name == 'proposer_reject':
                outcome_kind = 'rejected'
                reason = str(p.get('reason') or 'proposer reject')
            elif phase_name == 'dedup':
                if p.get('duplicate') or p.get('status') == 'duplicate':
                    outcome_kind = 'rejected'
                    reason = str(p.get('reason') or 'duplicate')
            elif phase_name == 'idle':
                outcome_kind = 'idle'
                reason = str(p.get('reason') or 'no demand')
            elif phase_name == 'outcome':
                status = p.get('status') or (p.get('outcome') if isinstance(p.get('outcome'), str) else None)
                if status in ('success', 'integrated'):
                    outcome_kind = 'integrated'
                elif status in ('fail', 'failed'):
                    outcome_kind = 'failed'
                    if p.get('reason'):
                        reason = str(p.get('reason'))
                elif status == 'partial':
                    outcome_kind = 'partial'
                if p.get('delta') is not None:
                    metric_delta = str(p.get('delta'))
                elif p.get('metric_delta') is not None:
                    metric_delta = str(p.get('metric_delta'))

        # Check if integrated in evolution tree
        if cid in tree_by_cycle:
            outcome_kind = 'integrated'
            tree_node = tree_by_cycle[cid]
            fitness = tree_node.get('fitness') if isinstance(tree_node.get('fitness'), dict) else {}
            if fitness.get('reward') is not None:
                metric_delta = f"reward: {fitness.get('reward')}"

        if outcome_kind == 'integrated':
            badge_class = 'badge-integrated'
            outcome_label = 'INTEGRATED'
        elif outcome_kind == 'failed':
            badge_class = 'badge-failed'
            outcome_label = f'FAILED{(": " + reason) if reason else ""}'
        elif outcome_kind == 'gate_blocked':
            badge_class = 'badge-blocked'
            outcome_label = f'GATE BLOCKED{(": " + reason) if reason else ""}'
        elif outcome_kind == 'rejected':
            badge_class = 'badge-rejected'
            outcome_label = f'REJECTED{(": " + reason) if reason else ""}'
        elif outcome_kind == 'idle':
            badge_class = 'badge-available'
            outcome_label = f'IDLE{(": " + reason) if reason else ""}'
        elif outcome_kind == 'partial':
            badge_class = 'badge-researching'
            outcome_label = 'PARTIAL'

        files_html = ''
        if files_changed:
            files_str = ', '.join(files_changed[:3]) + (f' +{len(files_changed)-3} more' if len(files_changed) > 3 else '')
            files_html = f'<div class="feed-files" title="{esc(", ".join(files_changed))}">📁 {esc(files_str)}</div>'

        delta_html = f'<span class="feed-delta">{esc(metric_delta)}</span>' if metric_delta else ''
        ts_html = f'<span class="feed-ts">{fmt_ts(ts_val)}</span>' if ts_val else ''

        rows.append(f'''
        <li class="feed-row feed-outcome-{outcome_kind}" id="cycle-{esc(cid)}">
          <div class="feed-header">
            <span class="badge {badge_class}">{esc(outcome_label)}</span>
            <strong class="feed-title">{esc(title)}</strong>
            <span class="feed-cid">({esc(cid)})</span>
            {delta_html}
            {ts_html}
          </div>
          {files_html}
        </li>
        ''')

    return f'''
    <section class="panel panel-feed">
      <h2 class="panel-title">Cycle Feed (Recent {len(rows)})</h2>
      <ul class="feed-list">
        {''.join(rows)}
      </ul>
    </section>
    '''


def build_hypotheses_panel(hypotheses_lifecycle: dict[str, Any] | None, hypotheses: dict[str, Any] | None = None) -> str:
    # Accept either hypotheses_lifecycle or legacy hypotheses dict
    entries_dict = {}
    if isinstance(hypotheses_lifecycle, dict) and isinstance(hypotheses_lifecycle.get('entries'), dict):
        entries_dict = hypotheses_lifecycle.get('entries')
    elif isinstance(hypotheses, dict) and isinstance(hypotheses.get('entries'), dict):
        entries_dict = hypotheses.get('entries')
    elif hypotheses_lifecycle is None and hypotheses is None:
        return unavailable_panel('Hypotheses Lifecycle', 'hypotheses unavailable')

    if not entries_dict:
        return f'''
        <section class="panel panel-hypotheses">
          <h2 class="panel-title">Hypotheses Lifecycle</h2>
          <p class="unavailable-note">no hypotheses recorded</p>
        </section>
        '''

    active_items = []
    answered_items = []

    for hid, info in entries_dict.items():
        if not isinstance(info, dict):
            continue
        status = str(info.get('status') or 'open').lower()
        title = info.get('title') or hid
        first_seen = info.get('first_seen') or ''
        last_touched = info.get('last_touched') or ''
        answered_ev = info.get('answered_evidence') or ''
        answered_at = info.get('answered_at') or ''
        verdict = info.get('verdict')

        ev_html = ''
        if answered_ev:
            ev_html = f'<span class="hypo-ev">evidence: <a href="#cycle-{esc(answered_ev)}">{esc(answered_ev)}</a></span>'

        if 'answered' in status or status in ('supported', 'refuted', 'inconclusive') or verdict:
            v_label = str(verdict or status).upper()
            badge_class = {
                'SUPPORTED': 'verdict-supported',
                'REFUTED': 'verdict-refuted',
                'INCONCLUSIVE': 'verdict-inconclusive',
                'ANSWERED': 'badge-integrated',
            }.get(v_label, 'badge-integrated')
            answered_items.append(f'''
            <li class="hypo-row answered">
              <span class="badge {badge_class}">{esc(v_label)}</span>
              <strong class="hypo-title">{esc(title)}</strong>
              <div class="hypo-meta">
                {ev_html}
                {f'<span class="hypo-ts">answered {fmt_ts(answered_at)}</span>' if answered_at else ""}
              </div>
            </li>
            ''')
        else:
            active_items.append(f'''
            <li class="hypo-row active">
              <span class="badge badge-researching">{esc(status.upper())}</span>
              <strong class="hypo-title">{esc(title)}</strong>
              <div class="hypo-meta">
                {f'<span class="hypo-ts">seen {fmt_ts(first_seen)}</span>' if first_seen else ""}
                {f'<span class="hypo-ts">touched {fmt_ts(last_touched)}</span>' if last_touched else ""}
              </div>
            </li>
            ''')

    # NOTE: no backslashes inside f-string expressions -- host runs Python 3.11 (pre-PEP 701)
    active_body = "".join(active_items) if active_items else '<li class="unavailable-note">none active</li>'
    answered_body = "".join(answered_items) if answered_items else '<li class="unavailable-note">none answered</li>'
    active_html = f'<div class="hypo-group"><h3>Active ({len(active_items)})</h3><ul class="hypo-list">{active_body}</ul></div>'
    answered_html = f'<div class="hypo-group"><h3>Answered ({len(answered_items)})</h3><ul class="hypo-list">{answered_body}</ul></div>'

    return f'''
    <section class="panel panel-hypotheses">
      <h2 class="panel-title">Hypotheses Lifecycle</h2>
      <div class="hypo-split">
        {active_html}
        {answered_html}
      </div>
    </section>
    '''


def build_agent_panel(
    agents_md: str | None,
    goal_text: dict[str, Any] | None,
    skill_reads: dict[str, Any] | None,
    portfolio: dict[str, Any] | None = None,
    ledger_tail: list[dict[str, Any]] | None = None,
) -> str:
    # 1. AGENTS.md
    if agents_md is not None:
        md_text = agents_md.strip()
        agents_html = f'<div class="agents-md-box"><pre><code>{esc(md_text[:2000])}{"..." if len(md_text) > 2000 else ""}</code></pre></div>'
    else:
        agents_html = '<p class="unavailable-note">AGENTS.md unavailable</p>'

    # 2. Goals charter
    goals_html = '<p class="unavailable-note">goals charter unavailable</p>'
    if isinstance(goal_text, dict):
        g_text = goal_text.get('charter') or goal_text.get('goal_text') or goal_text.get('text') or str(goal_text)
        goals_html = f'<div class="goal-text-box"><pre><code>{esc(str(g_text)[:1500])}</code></pre></div>'

    # 3. Skills fitness table
    skills_html = '<p class="unavailable-note">skill reads unavailable</p>'
    if isinstance(skill_reads, dict):
        reads_list = skill_reads.get('reads')
        if isinstance(reads_list, list):
            # Aggregate per-skill reads count
            read_counts: dict[str, int] = {}
            for r in reads_list:
                if isinstance(r, dict) and r.get('skill'):
                    sname = str(r.get('skill'))
                    read_counts[sname] = read_counts.get(sname, 0) + 1

            if read_counts:
                # Sort by read count descending
                sorted_skills = sorted(read_counts.items(), key=lambda kv: kv[1], reverse=True)
                rows = []
                for sname, count in sorted_skills:
                    # Note on confirmed usage: honestly note omission / not tracked
                    usage_note = '<span class="skill-untracked">not tracked</span>'
                    ratio_flag = 'skill-high-ratio' if count >= 5 else ''
                    rows.append(f'''
                    <tr class="{ratio_flag}">
                      <td class="skill-name">{esc(sname)}</td>
                      <td class="skill-reads">{count}</td>
                      <td class="skill-usage">{usage_note}</td>
                    </tr>
                    ''')

                skills_html = f'''
                <table class="skills-table">
                  <thead>
                    <tr><th>Skill</th><th>Reads</th><th>Confirmed Usage</th></tr>
                  </thead>
                  <tbody>
                    {''.join(rows)}
                  </tbody>
                </table>
                '''
            else:
                skills_html = '<p class="unavailable-note">no skill reads recorded</p>'

    return f'''
    <section class="panel panel-agent">
      <h2 class="panel-title">Agent Configuration & Fitness</h2>
      <div class="agent-grid">
        <div class="agent-subcol">
          <h3>AGENTS.md Charter</h3>
          {agents_html}
        </div>
        <div class="agent-subcol">
          <h3>Goals Charter</h3>
          {goals_html}
        </div>
        <div class="agent-subcol">
          <h3>Skill Fitness (Reads & Usage)</h3>
          {skills_html}
        </div>
      </div>
    </section>
    '''


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

    /* --- overall layout: column flex with nice gap --- */
    .dashboard-main {
      display: flex;
      flex-direction: column;
      gap: 18px;
      padding: 18px;
      max-width: 1600px;
      margin: 0 auto;
    }

    .canvas-outer {
      width: 100%;
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
      padding: 14px 18px;
      box-shadow: 0 4px 18px rgba(0, 0, 0, 0.35);
    }
    .panel-title {
      margin: 0 0 12px 0;
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

    /* --- Now panel --- */
    .now-content {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .now-item {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      font-size: 0.9em;
    }
    .now-label {
      font-weight: 700;
      color: #8b96ad;
      min-width: 140px;
      text-transform: uppercase;
      font-size: 0.78em;
      letter-spacing: 0.5px;
    }
    .now-detail {
      color: #a0aec0;
      font-size: 0.85em;
      margin-left: 6px;
    }
    .now-sub {
      color: #718096;
      font-size: 0.85em;
    }
    .now-demand-grid {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .demand-subgroup {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
    }
    .demand-sublabel {
      color: #718096;
      font-size: 0.8em;
      min-width: 75px;
    }
    .demand-chip {
      font-size: 0.75em;
      padding: 2px 8px;
      border-radius: 4px;
      font-family: 'Consolas', monospace;
      border: 1px solid #2c3a5c;
      background: rgba(15, 24, 42, 0.8);
    }
    .demand-chip.served {
      color: #2fd3c4;
      border-color: rgba(47, 211, 196, 0.4);
    }
    .demand-chip.completed {
      color: #c9a227;
      border-color: rgba(201, 162, 39, 0.4);
    }

    /* --- Cycle Feed --- */
    .feed-list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 420px;
      overflow-y: auto;
    }
    .feed-row {
      background: rgba(15, 24, 42, 0.7);
      border: 1px solid #24314f;
      border-radius: 6px;
      padding: 8px 12px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .feed-row:hover {
      border-color: #3a4a6e;
      background: rgba(20, 33, 58, 0.8);
    }
    .feed-outcome-integrated { border-left: 4px solid #2fd3c4; }
    .feed-outcome-failed { border-left: 4px solid #b23a3a; }
    .feed-outcome-gate_blocked { border-left: 4px solid #e06c75; }
    .feed-outcome-rejected { border-left: 4px solid #d19a66; }
    .feed-outcome-idle { border-left: 4px solid #5c6370; }
    .feed-outcome-partial { border-left: 4px solid #c9a227; }
    .feed-outcome-in_progress { border-left: 4px solid #61afef; }

    .feed-header {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      font-size: 0.88em;
    }
    .feed-title {
      color: #eae3c8;
      font-size: 0.95em;
    }
    .feed-cid {
      color: #718096;
      font-family: 'Consolas', monospace;
      font-size: 0.82em;
    }
    .feed-delta {
      color: #2fd3c4;
      font-family: 'Consolas', monospace;
      font-size: 0.8em;
      margin-left: auto;
    }
    .feed-ts {
      color: #718096;
      font-size: 0.78em;
      white-space: nowrap;
    }
    .feed-files {
      font-size: 0.78em;
      color: #8b96ad;
      font-family: 'Consolas', monospace;
      padding-left: 4px;
    }

    /* --- Hypotheses Panel --- */
    .hypo-split {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }
    @media (max-width: 800px) {
      .hypo-split { grid-template-columns: 1fr; }
    }
    .hypo-group h3 {
      font-size: 0.9em;
      color: #8b96ad;
      text-transform: uppercase;
      letter-spacing: 1px;
      margin: 0 0 8px 0;
      border-bottom: 1px solid #1c2740;
      padding-bottom: 4px;
    }
    .hypo-list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 320px;
      overflow-y: auto;
    }
    .hypo-row {
      background: rgba(15, 24, 42, 0.7);
      border: 1px solid #24314f;
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 0.85em;
    }
    .hypo-title {
      color: #eae3c8;
      margin-left: 6px;
    }
    .hypo-meta {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 0.8em;
      color: #718096;
      margin-top: 4px;
    }
    .hypo-ev a {
      color: #2fd3c4;
      text-decoration: underline;
      font-family: 'Consolas', monospace;
    }

    /* --- Agent Panel --- */
    .agent-grid {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 16px;
    }
    @media (max-width: 1000px) {
      .agent-grid { grid-template-columns: 1fr; }
    }
    .agent-subcol h3 {
      font-size: 0.9em;
      color: #8b96ad;
      text-transform: uppercase;
      letter-spacing: 1px;
      margin: 0 0 8px 0;
      border-bottom: 1px solid #1c2740;
      padding-bottom: 4px;
    }
    .agents-md-box pre, .goal-text-box pre {
      margin: 0;
      background: rgba(11, 18, 32, 0.9);
      border: 1px solid #24314f;
      border-radius: 6px;
      padding: 10px;
      font-family: 'Consolas', monospace;
      font-size: 0.78em;
      color: #c7cfe0;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 250px;
      overflow-y: auto;
    }
    .skills-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.8em;
      background: rgba(11, 18, 32, 0.6);
      border-radius: 6px;
      overflow: hidden;
    }
    .skills-table th {
      text-align: left;
      padding: 6px 8px;
      background: #14213a;
      color: #8b96ad;
      font-size: 0.75em;
      text-transform: uppercase;
      border-bottom: 1px solid #24314f;
    }
    .skills-table td {
      padding: 6px 8px;
      border-bottom: 1px solid #1c2740;
    }
    .skill-high-ratio {
      background: rgba(201, 162, 39, 0.08);
    }
    .skill-reads {
      font-weight: 700;
      color: #c9a227;
      font-family: 'Consolas', monospace;
      text-align: center;
    }
    .skill-untracked {
      color: #5c6370;
      font-style: italic;
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

    /* --- evolution boxes (Lane B / DGM LINEAGE) --- */
    .evo-box {
      width: 100%;
      height: 100%;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 2px;
      background: rgba(15, 24, 42, 0.9);
      border: 1.5px solid #4a5878;
      border-radius: 7px;
      padding: 4px 8px;
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
    .evo-header {
      display: flex;
      align-items: center;
      gap: 4px;
      overflow: hidden;
    }
    .evo-diamond { color: #c9a227; font-weight: 700; }
    .evo-box-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; font-weight: 600; color: #eae3c8; }
    .evo-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 9px;
    }
    .evo-dir-badge {
      font-size: 8px;
      text-transform: uppercase;
      background: rgba(47, 211, 196, 0.15);
      color: #2fd3c4;
      padding: 1px 4px;
      border-radius: 3px;
      border: 1px solid rgba(47, 211, 196, 0.3);
    }
    .evo-sha { color: #718096; }
    .evo-fitness { font-size: 9px; color: #a0aec0; }
    .evo-elbow { fill: none; stroke: #3a4a6e; stroke-width: 1.6; }
    .evo-fallback { color: #d8dce6; }

    .badge {
      display: inline-block;
      font-size: 0.64em;
      font-weight: 700;
      letter-spacing: 0.5px;
      padding: 2px 7px;
      border-radius: 4px;
    }
    .badge-researching { background: rgba(201, 162, 39, 0.22); color: #c9a227; border: 1px solid #c9a227; }
    .badge-available { background: rgba(139, 150, 173, 0.18); color: #b7c0d4; border: 1px solid #4a5878; }
    .badge-plateaued { background: rgba(178, 58, 58, 0.15); color: #d97b7b; border: 1px solid #6d3232; }
    .badge-integrated { background: rgba(47, 211, 196, 0.2); color: #2fd3c4; border: 1px solid #2fd3c4; }
    .badge-failed { background: rgba(178, 58, 58, 0.2); color: #e06c75; border: 1px solid #b23a3a; }
    .badge-blocked { background: rgba(224, 108, 117, 0.2); color: #e06c75; border: 1px solid #e06c75; }
    .badge-rejected { background: rgba(209, 154, 102, 0.2); color: #d19a66; border: 1px solid #d19a66; }
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
<main class="dashboard-main">
{now_panel}
{canvas}
{cycle_feed}
{hypotheses_panel}
{agent_panel}
</main>
<footer class="page-footer">generated {generated_at} UTC &middot; host {host} &middot; newest source {source_age}{error_note}{titles_note}</footer>
</body>
</html>
'''


def render_page(data: dict[str, Any], host: str, generated_at: str | None = None) -> str:
    """Build the full self-contained HTML page from the combined state dict.

    `data` is expected to have the same shape as the JSON produced by
    REMOTE_READER_SCRIPT: keys portfolio / scorecard / evolution_tree /
    hypotheses / ledger_tail / demand_rotation / demand_completed / skill_reads /
    goal_text / agents_md / cycle_titles, any of which may be None. Never raises
    on missing/malformed data -- every panel fails soft to an "unavailable"
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
    demand_rotation = data.get('demand_rotation')
    demand_completed = data.get('demand_completed')
    skill_reads = data.get('skill_reads')
    goal_text = data.get('goal_text')
    agents_md = data.get('agents_md')
    cycle_titles = data.get('cycle_titles')

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

    titles_note = ''
    if data.get('cycle_titles_error'):
        titles_note = f' &middot; &#9888; task titles unavailable ({esc(str(data.get("cycle_titles_error")))})'

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
    now_panel = build_now_panel(
        portfolio=portfolio,
        evolution_tree=evolution_tree,
        demand_rotation=demand_rotation,
        demand_completed=demand_completed,
        task_titles=cycle_titles,
        ledger_tail=ledger_tail,
    )
    canvas_html = build_tech_canvas(
        portfolio=portfolio,
        ledger_tail=ledger_tail,
        evolution_tree=evolution_tree,
        task_titles=cycle_titles,
    )
    cycle_feed = build_cycle_feed(
        ledger_tail=ledger_tail,
        demand_completed=demand_completed,
        task_titles=cycle_titles,
        evolution_tree=evolution_tree,
        cycle_files=data.get('cycle_files'),
    )
    hypotheses_panel = build_hypotheses_panel(hypotheses)
    agent_panel = build_agent_panel(
        agents_md=agents_md,
        goal_text=goal_text,
        skill_reads=skill_reads,
        portfolio=portfolio,
        ledger_tail=ledger_tail,
    )

    return PAGE_TEMPLATE.format(
        css=CSS,
        empire_strip=empire_strip,
        now_panel=now_panel,
        canvas=canvas_html,
        cycle_feed=cycle_feed,
        hypotheses_panel=hypotheses_panel,
        agent_panel=agent_panel,
        generated_at=esc(generated_at),
        host=esc(host),
        source_age=esc(source_age),
        error_note=error_note,
        titles_note=titles_note,
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
