#!/usr/bin/env python3
"""Civilization-style tech-tree viewer for the eeebot self-evolving loop.

Standalone operator tooling. Fetches four state files (plus a short ledger
tail) from the `eeepc` authority host in a single SSH round-trip, then
renders a self-contained static HTML page styled like a strategy-game
research screen. No external assets, no CDNs -- the output opens fine as a
plain `file://` document.

Usage:
    python scripts/techtree_viewer.py [--host eeepc] [--out techtree.html] [--open]
"""
from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

SSH_USER = 'ozand'
REMOTE_SUDO_USER = 'eeepc-agent'
SSH_TIMEOUT_SECONDS = 45

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


def read_json(relpath):
    path = os.path.join(STATE_ROOT, relpath)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def read_ledger_tail(relpath):
    path = os.path.join(STATE_ROOT, relpath)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
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


def unavailable_panel(title: str, reason: str = 'source unavailable') -> str:
    return f'''
    <section class="panel panel-unavailable">
      <h2 class="panel-title">{esc(title)}</h2>
      <p class="unavailable-note">&#8968; {esc(reason)} &#8969;</p>
    </section>
    '''


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------

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


def build_research_panel(portfolio: dict[str, Any] | None, ledger_tail: list[Any] | None) -> str:
    if not isinstance(portfolio, dict):
        return unavailable_panel('Research', 'portfolio.json unavailable')

    current = portfolio.get('current')
    nodes = portfolio.get('nodes')
    if not isinstance(nodes, dict) or not nodes:
        return unavailable_panel('Research', 'no research nodes recorded')

    cards = []
    for name, node in nodes.items():
        if not isinstance(node, dict):
            continue
        status = node.get('status') or 'active'
        is_current = name == current
        minted_by = node.get('minted_by')
        ribbon = ''
        if minted_by == 'hypothesis':
            ribbon = '<div class="ribbon">MINTED BY HYPOTHESIS</div>'

        if is_current:
            badge_class, badge_text = 'badge-researching', 'RESEARCHING'
        elif status == 'plateaued':
            badge_class, badge_text = 'badge-plateaued', 'PLATEAUED'
        else:
            badge_class, badge_text = 'badge-available', 'AVAILABLE'

        cooldown_note = ''
        if status == 'plateaued' and node.get('cooldown_until_ts'):
            cooldown_note = f'<div class="cooldown">cooldown until {fmt_ts(node.get("cooldown_until_ts"))}</div>'

        card_class = 'tech-card'
        if is_current:
            card_class += ' tech-card-current'
        elif status == 'plateaued':
            card_class += ' tech-card-plateaued'

        cards.append(f'''
        <div class="{card_class}">
          {ribbon}
          <div class="tech-card-head">
            <span class="tech-icon">&#9881;</span>
            <span class="tech-name">{title_case_name(name)}</span>
          </div>
          <div class="tech-lever">{small_caps_metric(node.get('lever_metric'))}</div>
          <span class="badge {badge_class}">{badge_text}</span>
          {build_sparkline(node.get('gain_history'))}
          {cooldown_note}
        </div>
        ''')

    switches = portfolio.get('switches')
    if not isinstance(switches, list) or not switches:
        # Fail-soft fallback: derive the chronicle from the ledger tail's
        # tech_tree phase entries when portfolio.json has none recorded.
        derived = []
        for entry in (ledger_tail or []):
            if isinstance(entry, dict) and entry.get('phase') == 'tech_tree' and entry.get('from') and entry.get('to'):
                derived.append(entry)
        switches = derived

    chronicle_rows = []
    for switch in reversed(switches[-15:]):
        if not isinstance(switch, dict):
            continue
        chronicle_rows.append(
            f'<li><span class="chronicle-ts">{fmt_ts(switch.get("ts"))}</span>'
            f' &mdash; <span class="chronicle-from">{title_case_name(switch.get("from"))}</span>'
            f' &rarr; <span class="chronicle-to">{title_case_name(switch.get("to"))}</span>'
            f' <span class="chronicle-reason">({esc(switch.get("reason") or "switch")})</span></li>'
        )
    chronicle_html = (
        '<ul class="chronicle-list">' + ''.join(chronicle_rows) + '</ul>'
        if chronicle_rows else '<p class="unavailable-note">no research switches recorded yet</p>'
    )

    return f'''
    <section class="panel panel-research">
      <h2 class="panel-title">Research</h2>
      <div class="tech-row">{''.join(cards)}</div>
      <details class="chronicle" open>
        <summary>Research chronicle</summary>
        {chronicle_html}
      </details>
    </section>
    '''


def build_eras_band(scorecard: dict[str, Any] | None) -> str:
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
      <div class="era-row">{''.join(medallions)}</div>
      <div class="era-caption">Trust Level {level_text}</div>
    </section>
    '''


def build_library_panel(scorecard: dict[str, Any] | None, hypotheses: dict[str, Any] | None) -> str:
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
        for key, entry in verdicted[:20]:
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
              <span class="verdict-at">{fmt_ts(entry.get('verdict_at'))}</span>
            </li>
            ''')

    verdicts_html = (
        '<ul class="verdict-list">' + ''.join(verdict_rows) + '</ul>'
        if verdict_rows else '<p class="unavailable-note">no verdicts recorded yet</p>'
    )

    return f'''
    <section class="panel panel-library">
      <h2 class="panel-title">Great Library</h2>
      {counts_html}
      {verdicts_html}
    </section>
    '''


def build_world_history_panel(evolution_tree: dict[str, Any] | None) -> str:
    if not isinstance(evolution_tree, dict):
        return unavailable_panel('World History', 'evolution tree unavailable')

    current_sha = evolution_tree.get('current_sha')
    nodes = evolution_tree.get('nodes')
    switches = evolution_tree.get('switches')
    switches_count = len(switches) if isinstance(switches, list) else (switches if isinstance(switches, int) else 0)

    if not isinstance(nodes, dict):
        nodes = {}

    node_list = []
    for sha, node in nodes.items():
        if isinstance(node, dict):
            node_list.append((sha, node))
    node_list.sort(key=lambda item: item[1].get('ts') or '', reverse=True)

    timeline_rows = []
    for sha, node in node_list[:6]:
        timeline_rows.append(f'''
        <li>
          <span class="timeline-sha">{short_sha(sha)}</span>
          <span class="timeline-branch">{esc(node.get('branch') or 'n/a')}</span>
          <span class="timeline-ts">{fmt_ts(node.get('ts'))}</span>
        </li>
        ''')

    timeline_html = (
        '<ul class="timeline-list">' + ''.join(timeline_rows) + '</ul>'
        if timeline_rows else '<p class="unavailable-note">no evolution nodes recorded yet</p>'
    )

    return f'''
    <section class="panel panel-history">
      <h2 class="panel-title">World History</h2>
      <div class="history-summary">
        <div><span class="stat-label">current sha</span><span class="stat-value">{short_sha(current_sha, 12)}</span></div>
        <div><span class="stat-label">nodes</span><span class="stat-value">{esc(len(nodes))}</span></div>
        <div><span class="stat-label">switches</span><span class="stat-value">{esc(switches_count)}</span></div>
      </div>
      {timeline_html}
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
    body {
      margin: 0;
      background: radial-gradient(ellipse at top, #101c30 0%, #0b1220 60%, #070c16 100%);
      color: #d8dce6;
      font-family: 'Segoe UI', Helvetica, Arial, sans-serif;
      padding: 0 0 48px 0;
    }
    h1, h2, h3, .panel-title, .empire-title {
      font-family: Georgia, 'Times New Roman', serif;
    }
    a { color: #c9a227; }

    .empire-strip {
      background: linear-gradient(180deg, #14213a 0%, #0d1626 100%);
      border-bottom: 2px solid #c9a227;
      padding: 18px 28px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 18px;
      justify-content: space-between;
    }
    .empire-title {
      color: #c9a227;
      font-size: 1.6em;
      letter-spacing: 2px;
      text-shadow: 0 0 12px rgba(201, 162, 39, 0.45);
    }
    .empire-stats {
      display: flex;
      flex-wrap: wrap;
      gap: 22px;
    }
    .empire-stat { display: flex; flex-direction: column; align-items: center; min-width: 90px; }
    .empire-computed { color: #7d8aa3; font-size: 0.85em; }

    .stat-label {
      text-transform: uppercase;
      font-size: 0.68em;
      letter-spacing: 1px;
      color: #8b96ad;
    }
    .stat-value {
      font-size: 1.15em;
      color: #eae3c8;
      font-weight: 600;
    }

    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px 20px;
      display: grid;
      grid-template-columns: 1fr;
      gap: 22px;
    }

    .panel {
      background: linear-gradient(180deg, rgba(20, 33, 58, 0.85) 0%, rgba(11, 18, 32, 0.9) 100%);
      border: 1px solid #24314f;
      border-radius: 10px;
      padding: 18px 20px 22px 20px;
      box-shadow: 0 4px 18px rgba(0, 0, 0, 0.35);
    }
    .panel-title {
      margin: 0 0 14px 0;
      color: #c9a227;
      font-size: 1.3em;
      letter-spacing: 1px;
      border-bottom: 1px solid #2c3a5c;
      padding-bottom: 8px;
    }
    .panel-unavailable {
      text-align: center;
      color: #6a7590;
    }
    .unavailable-note {
      color: #6a7590;
      font-style: italic;
    }

    .era-row { display: flex; flex-wrap: wrap; gap: 16px; justify-content: center; }
    .era-medallion {
      width: 130px;
      padding: 14px 8px;
      border-radius: 50% / 22%;
      text-align: center;
      border: 2px solid #2c3a5c;
    }
    .era-lit {
      border-color: #2fd3c4;
      box-shadow: 0 0 16px rgba(47, 211, 196, 0.5);
      background: rgba(47, 211, 196, 0.08);
    }
    .era-locked {
      opacity: 0.55;
      background: rgba(90, 90, 90, 0.08);
    }
    .era-glyph { font-size: 1.6em; }
    .era-name { font-size: 0.78em; margin-top: 6px; color: #c7cfe0; }
    .era-caption {
      text-align: center;
      margin-top: 12px;
      color: #c9a227;
      letter-spacing: 1px;
      font-family: Georgia, serif;
    }

    .tech-row {
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
    }
    .tech-card {
      position: relative;
      flex: 1 1 200px;
      max-width: 240px;
      background: rgba(15, 24, 42, 0.9);
      border: 1px solid #2c3a5c;
      border-radius: 8px;
      padding: 14px;
    }
    .tech-card-current {
      border-color: #2fd3c4;
      box-shadow: 0 0 18px rgba(47, 211, 196, 0.55);
    }
    .tech-card-plateaued { opacity: 0.65; }
    .tech-card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
    .tech-icon { font-size: 1.2em; color: #c9a227; }
    .tech-name { font-weight: 600; color: #eae3c8; }
    .tech-lever {
      font-variant: small-caps;
      letter-spacing: 0.5px;
      color: #8b96ad;
      font-size: 0.82em;
      margin-bottom: 8px;
    }
    .ribbon {
      position: absolute;
      top: -8px;
      right: -8px;
      background: #c9a227;
      color: #1a1406;
      font-size: 0.62em;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 4px;
      transform: rotate(4deg);
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.4);
    }

    .badge {
      display: inline-block;
      font-size: 0.68em;
      font-weight: 700;
      letter-spacing: 0.5px;
      padding: 3px 8px;
      border-radius: 4px;
      margin-bottom: 10px;
    }
    .badge-researching { background: rgba(47, 211, 196, 0.18); color: #2fd3c4; border: 1px solid #2fd3c4; }
    .badge-available { background: rgba(139, 150, 173, 0.18); color: #b7c0d4; border: 1px solid #4a5878; }
    .badge-plateaued { background: rgba(178, 58, 58, 0.15); color: #d97b7b; border: 1px solid #6d3232; }
    .badge-verdict, .verdict-supported, .verdict-refuted, .verdict-inconclusive {
      display: inline-block;
    }
    .verdict-supported { background: rgba(201, 162, 39, 0.18); color: #c9a227; border: 1px solid #c9a227; }
    .verdict-refuted { background: rgba(178, 58, 58, 0.18); color: #d97b7b; border: 1px solid #6d3232; }
    .verdict-inconclusive { background: rgba(139, 150, 173, 0.18); color: #b7c0d4; border: 1px solid #4a5878; }

    .spark { margin-bottom: 4px; }
    .spark-top, .spark-bottom {
      display: flex;
      align-items: flex-end;
      gap: 2px;
      height: 16px;
    }
    .spark-bottom { align-items: flex-start; }
    .spark-baseline { border-top: 1px dashed #3a4a6e; }
    .bar { width: 6px; border-radius: 1px; display: block; }
    .bar-pos { background: #c9a227; }
    .bar-neg { background: #b23a3a; }
    .bar-placeholder { height: 0; background: transparent; }
    .spark-empty { color: #6a7590; font-style: italic; font-size: 0.82em; margin-bottom: 8px; }
    .spark-mean { font-size: 0.76em; margin-top: 2px; }
    .mean-pos { color: #c9a227; }
    .mean-neg { color: #d97b7b; }
    .cooldown { font-size: 0.72em; color: #8b96ad; margin-top: 6px; }

    details.chronicle { margin-top: 16px; }
    details.chronicle summary {
      cursor: pointer;
      color: #c9a227;
      font-family: Georgia, serif;
      font-size: 1.02em;
      margin-bottom: 8px;
    }
    ul.chronicle-list, ul.verdict-list, ul.timeline-list {
      list-style: none;
      margin: 0;
      padding: 0;
      max-height: 260px;
      overflow-y: auto;
    }
    ul.chronicle-list li, ul.verdict-list li, ul.timeline-list li {
      padding: 6px 0;
      border-bottom: 1px solid #1c2740;
      font-size: 0.86em;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .chronicle-ts, .verdict-at, .timeline-ts { color: #6a7590; font-size: 0.9em; }
    .chronicle-from { color: #b7c0d4; }
    .chronicle-to { color: #eae3c8; font-weight: 600; }
    .chronicle-reason { color: #6a7590; font-size: 0.85em; }
    .verdict-title { color: #eae3c8; flex: 1 1 auto; }
    .timeline-sha { font-family: 'Consolas', 'Courier New', monospace; color: #2fd3c4; }
    .timeline-branch { color: #b7c0d4; font-size: 0.85em; }

    .count-row { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 16px; }
    .count-chip {
      display: flex;
      flex-direction: column;
      align-items: center;
      min-width: 78px;
      padding: 8px 10px;
      border-radius: 6px;
      border: 1px solid #2c3a5c;
      background: rgba(15, 24, 42, 0.7);
    }
    .count-value { font-size: 1.3em; font-weight: 700; color: #eae3c8; }
    .count-label { font-size: 0.68em; text-transform: uppercase; color: #8b96ad; letter-spacing: 0.5px; }
    .count-supported .count-value { color: #c9a227; }
    .count-refuted .count-value { color: #d97b7b; }

    .history-summary { display: flex; gap: 26px; margin-bottom: 14px; flex-wrap: wrap; }
    .history-summary > div { display: flex; flex-direction: column; }

    footer.page-footer {
      text-align: center;
      color: #4f5a76;
      font-size: 0.78em;
      margin-top: 28px;
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
<main>
{research_panel}
{eras_panel}
{library_panel}
{history_panel}
</main>
<footer class="page-footer">generated {generated_at} local time &middot; host {host}{error_note}</footer>
</body>
</html>
'''


def render_page(data: dict[str, Any], host: str, generated_at: str | None = None) -> str:
    """Build the full self-contained HTML page from the combined state dict.

    `data` is expected to have the same shape as the JSON produced by
    REMOTE_READER_SCRIPT: keys portfolio / scorecard / evolution_tree /
    hypotheses / ledger_tail, any of which may be None. Never raises on
    missing/malformed data -- every panel fails soft to an "unavailable"
    card instead.
    """
    if generated_at is None:
        generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    portfolio = data.get('portfolio')
    scorecard = data.get('scorecard')
    evolution_tree = data.get('evolution_tree')
    hypotheses = data.get('hypotheses')
    ledger_tail = data.get('ledger_tail')

    error_note = ''
    if data.get('_error'):
        error_note = f' &middot; fetch note: {esc(data["_error"])}'

    empire_strip = build_empire_stats_strip(scorecard)
    research_panel = build_research_panel(portfolio, ledger_tail)
    eras_panel = build_eras_band(scorecard)
    library_panel = build_library_panel(scorecard, hypotheses)
    history_panel = build_world_history_panel(evolution_tree)

    return PAGE_TEMPLATE.format(
        css=CSS,
        empire_strip=empire_strip,
        research_panel=research_panel,
        eras_panel=eras_panel,
        library_panel=library_panel,
        history_panel=history_panel,
        generated_at=esc(generated_at),
        host=esc(host),
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data = fetch_remote_state(args.host)
    html_out = render_page(data, args.host)

    out_path = Path(args.out)
    out_path.write_text(html_out, encoding='utf-8')
    print(f'wrote {out_path.resolve()}')

    if data.get('_error'):
        print(f'note: {data["_error"]}', file=sys.stderr)

    if args.open:
        webbrowser.open(out_path.resolve().as_uri())

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
