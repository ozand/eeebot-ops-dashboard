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
import re
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


def read_llm_stats():
    """Issue #60: aggregate per-cycle LLM call stats from llm_calls/<date>.jsonl.
    Fail-soft per file and per line; recent files only (page shows recent cycles)."""
    ldir = os.path.join(STATE_ROOT, "llm_calls")
    stats = {}
    try:
        names = sorted(f for f in os.listdir(ldir) if f.endswith(".jsonl"))
    except Exception:
        return stats
    for name in names[-7:]:
        path = os.path.join(ldir, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            _mtimes.append(os.path.getmtime(path))
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            cid = row.get("cycle_id")
            if not cid:
                continue
            cid = str(cid)
            st = stats.setdefault(cid, {
                "calls": 0, "total_tokens": 0, "duration_ms": 0.0,
                "last_finish_reason": None, "any_length": False, "last_ts": "",
            })
            st["calls"] += 1
            tok = row.get("total_tokens")
            if isinstance(tok, (int, float)) and not isinstance(tok, bool):
                st["total_tokens"] += tok
            dur = row.get("duration_ms")
            if isinstance(dur, (int, float)) and not isinstance(dur, bool):
                st["duration_ms"] += dur
            fr = row.get("finish_reason")
            ts = str(row.get("ts") or "")
            if ts >= st["last_ts"]:
                st["last_ts"] = ts
                st["last_finish_reason"] = fr
            if fr == "length":
                st["any_length"] = True
    return stats


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
    "llm_stats": read_llm_stats(),
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
        'llm_stats': {},
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
        'llm_stats': {},
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

    def read_llm_stats_local() -> dict[str, Any]:
        """Issue #60: per-cycle LLM cost aggregation from llm_calls/*.jsonl.
        Local mirror of the REMOTE_READER_SCRIPT read_llm_stats() -- keep in
        sync. Fail-soft per file/line (issue #29 pattern)."""
        stats: dict[str, Any] = {}
        llm_dir = root / 'llm_calls'
        try:
            names = sorted(p.name for p in llm_dir.iterdir() if p.name.endswith('.jsonl'))
        except OSError:
            return stats
        for name in names[-7:]:
            path = llm_dir / name
            try:
                with path.open('r', encoding='utf-8', errors='replace') as fh:
                    lines = fh.readlines()
                mtimes.append(path.stat().st_mtime)
            except Exception:  # noqa: BLE001
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(row, dict):
                    continue
                cid = str(row.get('cycle_id') or '')
                if not cid:
                    continue
                st = stats.setdefault(cid, {
                    'calls': 0, 'total_tokens': 0, 'duration_ms': 0,
                    'last_finish_reason': None, 'any_length': False, 'last_ts': '',
                })
                st['calls'] += 1
                tok = row.get('total_tokens')
                if isinstance(tok, (int, float)) and not isinstance(tok, bool):
                    st['total_tokens'] += tok
                dur = row.get('duration_ms')
                if isinstance(dur, (int, float)) and not isinstance(dur, bool):
                    st['duration_ms'] += dur
                ts = str(row.get('ts') or '')
                if ts >= str(st.get('last_ts') or ''):
                    st['last_ts'] = ts
                    fr = row.get('finish_reason')
                    if fr:
                        st['last_finish_reason'] = str(fr)
                if row.get('finish_reason') == 'length':
                    st['any_length'] = True
        return stats

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
        'llm_stats': read_llm_stats_local(),
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


def fmt_compact(value: Any, signed: bool = False) -> str:
    """Format large/small numbers with compact K/M suffixes or passthrough strings."""
    if value is None or value == '':
        return 'n/a'
    if isinstance(value, str):
        val_clean = value.strip()
        if not val_clean:
            return 'n/a'
        try:
            num = float(val_clean)
        except ValueError:
            return esc(val_clean)
    elif isinstance(value, (int, float)):
        num = float(value)
    else:
        return esc(str(value))

    sign_str = '+' if (signed and num > 0) else ('-' if num < 0 else '')
    abs_num = abs(num)

    if abs_num >= 1_000_000:
        return f'{sign_str}{abs_num / 1_000_000:.2f}M'
    elif abs_num >= 100_000:
        return f'{sign_str}{abs_num / 1_000:.1f}K'
    elif abs_num >= 1_000:
        return f'{sign_str}{abs_num / 1_000:.2f}K'
    elif abs_num == 0:
        return '0'
    elif isinstance(value, int) or abs_num.is_integer():
        return f'{sign_str}{int(abs_num)}'
    else:
        formatted = f'{abs_num:.2f}'.rstrip('0').rstrip('.')
        return f'{sign_str}{formatted}'


def _fmt_duration_ms(ms: Any) -> str:
    """Issue #60: humanize a millisecond duration ('35m10s'). Empty string
    for missing/non-numeric/non-positive values."""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return ''
    total_s = int(ms // 1000)
    if total_s < 90:
        return f'{total_s}s'
    m, s = divmod(total_s, 60)
    if m < 90:
        if s:
            return f'{m}m{s}s'
        return f'{m}m'
    h, m = divmod(m, 60)
    if s:
        return f'{h}h{m}m{s}s'
    if m:
        return f'{h}h{m}m'
    return f'{h}h'


def _parse_iso_ts(ts_str: Any) -> datetime | None:
    """Parse ISO timestamp string to UTC datetime or None."""
    if not ts_str:
        return None
    s = str(ts_str).strip()
    if not s:
        return None
    try:
        clean_s = s.replace('Z', '+00:00')
        dt = datetime.fromisoformat(clean_s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fmt_ts_short(ts_str: Any, now: datetime | None = None) -> str:
    """Format ISO timestamp into short glanceable string (HH:MM UTC if today UTC, else Mon DD or Mon DD YYYY)."""
    if not ts_str:
        return 'n/a'
    s = str(ts_str).strip()
    if not s:
        return 'n/a'
    dt = _parse_iso_ts(s)
    if dt is None:
        return esc(s)

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    if dt.date() == now.date():
        return f'{dt.strftime("%H:%M")} UTC'
    elif dt.year == now.year:
        return dt.strftime('%b %d').replace(' 0', ' ')
    else:
        return dt.strftime('%b %d %Y').replace(' 0', ' ')


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


def build_sparkline(gain_history: list[Any] | None, aim: str | None = None) -> str:
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
    # Issue #61: an increase on an aim-lower metric is a REGRESSION, not a
    # gain -- render the delta aim-aware instead of bare "mean gain +N".
    if aim == 'lower':
        good = mean_gain < 0
        mean_class = 'mean-pos' if good else 'mean-neg'
        mean_label = f'mean Δ {fmt_compact(mean_gain, signed=True)} (aim: lower)'
    else:
        mean_class = 'mean-pos' if mean_gain >= 0 else 'mean-neg'
        mean_label = f'mean gain {fmt_compact(mean_gain, signed=True)}'
    return (
        '<div class="spark">'
        f'<div class="spark-top">{"".join(top_cells)}</div>'
        '<div class="spark-baseline"></div>'
        f'<div class="spark-bottom">{"".join(bottom_cells)}</div>'
        '</div>'
        f'<div class="spark-mean {mean_class}">{mean_label}</div>'
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
        f'{build_sparkline(node.get("gain_history"), node.get("direction"))}'
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

    # Visual separator separating the reserve / mint column on the left from the visited spine
    mint.append(f'<rect x="{reserve_x + RESERVE_COL_W + 10:.0f}" y="0" width="1" height="100%" class="mint-col-border" fill="#182a20" />')

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


def _score_color(norm: float, alpha: float | None = None) -> str:
    """Issue #53: green->yellow score scale (viridis tail) for node tinting."""
    norm = max(0.0, min(1.0, norm))
    hue = 140 - 80 * norm
    if alpha is None:
        return f'hsl({hue:.0f},70%,{35 + 25 * norm:.0f}%)'
    return f'hsla({hue:.0f},70%,{35 + 25 * norm:.0f}%,{alpha:.2f})'


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
    reward_min: float | None = None,
    reward_max: float | None = None,
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
        r = fitness.get('reward')
        if isinstance(r, (int, float)) and not isinstance(r, bool):
            fitness_line = f'<div class="evo-fitness">r:{fmt_compact(r)}</div>'

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

    # Issue #53: tint scored nodes along the green->yellow scale, normalized
    # to the min/max rewards actually present in the tree. Unscored nodes
    # stay neutral -- never fabricate a score.
    score_style = ''
    if isinstance(fitness, dict):
        r = fitness.get('reward')
        if (
            isinstance(r, (int, float)) and not isinstance(r, bool)
            and reward_min is not None and reward_max is not None
            and reward_max > reward_min
        ):
            norm = (float(r) - reward_min) / (reward_max - reward_min)
            score_style = (
                f' style="border-color:{_score_color(norm)};'
                f'background:{_score_color(norm, 0.12)}"'
            )

    diamond = f'<span class="evo-diamond">{"&#9733;" if is_current else "&#9672;"}</span>'
    tooltip = esc(f'{short_sha(sha)} | {title or display_title} | {branch}')

    # Node anchor and href
    node_id = f'node-{short_sha(sha)}'
    target_cid = cycle_id if cycle_id else (tail if tail.startswith('cycle-') else '')
    cycle_href = f'#cycle-{esc(target_cid)}' if target_cid else ''

    if cycle_href:
        header_content = f'<a href="{cycle_href}" class="evo-node-link">{diamond}<span class="evo-box-label">{label}{marker}</span></a>'
    else:
        header_content = f'{diamond}<span class="evo-box-label">{label}{marker}</span>'

    body = (
        f'<div class="{box_class}"{score_style} title="{tooltip}" id="{esc(node_id)}">'
        f'<div class="evo-header">{header_content}</div>'
        f'<div class="evo-meta">{dir_badge}<span class="evo-sha copyable" translate="no">{esc(short_sha(sha))}</span></div>'
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

    # Effective lineage parent (issue #53 follow-up): prefer the recorded
    # parent_sha when it maps to a kept node. On live data it usually does
    # NOT -- cycles branch from lesson commits recorded between merges, so
    # parent_sha is a bare git commit, never a node key. Fall back to
    # chronological chaining ("built on the previous integrated cycle"),
    # which reconstructs the DGM-style lineage for the sequential cycle
    # history this system actually runs.
    ts_order = sorted(kept.items(), key=lambda kv: str(kv[1].get('ts') or ''))
    chrono_prev: dict[str, str] = {}
    _last: str | None = None
    for _sha, _n in ts_order:
        if _last is not None:
            chrono_prev[_sha] = _last
        _last = _sha

    def _eff_parent(sha: str) -> str | None:
        node = kept.get(sha) or {}
        parent = node.get('parent_sha')
        if parent and parent in kept:
            return str(parent)
        return chrono_prev.get(sha)

    depth: dict[str, int] = {}

    def _depth(sha: str, guard: frozenset[str] = frozenset()) -> int:
        if sha in depth:
            return depth[sha]
        if sha in guard:  # defensive cycle guard -- a real git DAG never cycles
            depth[sha] = 0
            return 0
        parent = _eff_parent(sha)
        if not parent:
            depth[sha] = 0
        else:
            depth[sha] = _depth(parent, guard | {sha}) + 1
        return depth[sha]

    for sha in kept:
        _depth(sha)

    children_count: dict[str, int] = {}
    for sha in kept:
        parent = _eff_parent(sha)
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

    # Issue #53: ancestry chain of the current node = the "best path" that
    # DGM archive trees highlight with a thick edge.
    best_path: set[str] = set()
    if current_sha in kept:
        cur: str | None = current_sha
        guard: set[str] = set()
        while cur and cur in kept and cur not in guard:
            guard.add(cur)
            best_path.add(cur)
            cur = _eff_parent(cur)

    # Real per-node rewards (numeric only) for score tinting + legend.
    rewards: dict[str, float] = {}
    for sha, node in kept.items():
        fit = node.get('fitness')
        r = fit.get('reward') if isinstance(fit, dict) else None
        if isinstance(r, (int, float)) and not isinstance(r, bool):
            rewards[sha] = float(r)
    reward_min = min(rewards.values()) if rewards else None
    reward_max = max(rewards.values()) if rewards else None

    elbows = []
    for sha in kept:
        parent = _eff_parent(sha)
        if parent in pos:
            x1, y1 = pos[parent]
            x2, y2 = pos[sha]
            cls = 'evo-elbow'
            if sha in best_path and parent in best_path:
                cls = 'evo-elbow evo-elbow-best'
            elbows.append(_elbow_path(x1 + EVO_BOX_W, y1 + EVO_BOX_H / 2, x2, y2 + EVO_BOX_H / 2, cls))

    boxes = []
    for sha, node in kept.items():
        x, y = pos[sha]
        is_current = sha == current_sha
        is_abandoned = (not is_current) and children_count.get(sha, 0) == 0
        boxes.append(_evo_box_html(sha, node, is_current, is_abandoned, sha in switch_shas, x, y, task_titles, portfolio, reward_min, reward_max))

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
        'best_path': best_path,
        'reward_min': reward_min,
        'reward_max': reward_max,
    }


def _ts_range_label(evolution_tree: dict[str, Any] | None) -> str:
    """One-glance date-range label for the lineage lane (issue #43): the
    min/max node timestamps formatted short. Not a tick system -- a single
    orientation hint. Empty string when fewer than two parseable ts."""
    if not isinstance(evolution_tree, dict):
        return ''
    nodes = evolution_tree.get('nodes')
    if not isinstance(nodes, dict):
        return ''
    dts: list[datetime] = []
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        ts = node.get('ts')
        if ts is None:
            continue
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            try:
                dts.append(datetime.fromtimestamp(float(ts), tz=timezone.utc))
            except (OverflowError, OSError, ValueError):
                continue
        else:
            dt = _parse_iso_ts(ts)
            if dt is not None:
                dts.append(dt)
    if len(dts) < 2:
        return ''

    def _short(dt: datetime) -> str:
        return dt.strftime('%b %d').replace(' 0', ' ')

    lo, hi = min(dts), max(dts)
    if lo.year == hi.year:
        label = f'{_short(lo)} - {_short(hi)}'
    else:
        label = f'{_short(lo)} {lo.year} - {_short(hi)} {hi.year}'
    return f'<text x="10" y="46" class="lane-note">time range: {esc(label)}</text>'


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

    groups: list[str] = []
    y_cursor = CANVAS_MARGIN_Y

    # --- Lane A: RESEARCH -----------------------------------------------
    label_a = '<text x="10" y="14" class="lane-label">RESEARCH DIRECTIONS</text>'
    if lane_a.get('available'):
        note_html = f'<text x="10" y="32" class="lane-note">{esc(lane_a["note"])}</text>' if lane_a.get('note') else ''
        body = ''.join(lane_a['boxes']) + ''.join(lane_a['elbows']) + ''.join(lane_a['mint'])
        groups.append(f'<g class="lane lane-a" transform="translate(0,{y_cursor})">{label_a}{note_html}{body}</g>')
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
            date_label = _ts_range_label(evolution_tree)
            # Issue #53: score legend (gradient + min/max) when real rewards
            # exist; an honest muted note otherwise.
            legend_html = ''
            if lane_b.get('reward_min') is not None and lane_b.get('reward_max') is not None:
                lx = max(canvas_width - 180, 200)
                legend_html = (
                    '<g class="evo-legend" transform="translate(' + str(lx) + ',8)">'
                    '<defs><linearGradient id="scoregrad" x1="0" y1="0" x2="1" y2="0">'
                    f'<stop offset="0" stop-color="{_score_color(0)}"/>'
                    f'<stop offset="1" stop-color="{_score_color(1)}"/>'
                    '</linearGradient></defs>'
                    '<rect width="120" height="10" rx="3" fill="url(#scoregrad)"/>'
                    f'<text x="0" y="22" class="evo-legend-label">score {fmt_compact(lane_b["reward_min"])}</text>'
                    f'<text x="120" y="22" class="evo-legend-label" text-anchor="end">score {fmt_compact(lane_b["reward_max"])}</text>'
                    '</g>'
                )
            else:
                legend_html = (
                    f'<text x="{max(canvas_width - 200, 200)}" y="14" class="lane-note" text-anchor="end">'
                    'no node scores recorded yet</text>'
                )
            groups.append(f'<g class="lane lane-b" transform="translate(0,{y_cursor})">{label_b}{note_html}{date_label}{legend_html}{body}</g>')
            lane_b_height = lane_b['height']
    else:
        unavailable_html = f'<text x="10" y="36" class="lane-unavailable">&#8968; {esc(lane_b["reason"])} &#8969;</text>'
        groups.append(f'<g class="lane lane-b" transform="translate(0,{y_cursor})">{label_b}{unavailable_html}</g>')
        lane_b_height = 60
    y_cursor += lane_b_height + CANVAS_MARGIN_Y

    canvas_height = y_cursor
    canvas_width = max(canvas_width, MIN_CANVAS_W)

    # Issue #46: the big SVG is opaque to screen readers without role/label.
    node_count = 0
    if lane_b.get('available') and isinstance(lane_b.get('summary'), dict):
        try:
            node_count = int(lane_b['summary'].get('total_nodes') or 0)
        except (TypeError, ValueError):
            node_count = 0
    svg = (
        f'<svg class="tech-canvas" role="img" '
        f'aria-label="Cycle lineage graph: research directions and evolution lineage, '
        f'{node_count} nodes" '
        f'width="{canvas_width}" height="{canvas_height}" '
        f'viewBox="0 0 {canvas_width} {canvas_height}">'
        f'{"".join(groups)}'
        '</svg>'
    )
    jump_html = ''
    if isinstance(evolution_tree, dict):
        current = evolution_tree.get('current_sha')
        if current:
            jump_html = (
                f'<a class="canvas-jump" href="#node-{esc(short_sha(current))}">'
                '&gt;&gt; jump to current cycle</a>'
            )
    return f'<div class="canvas-outer" id="panel-lineage">{jump_html}{svg}</div>'


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
            # Issue #58: annotate the lever value's window/source so it is
            # not confused with the header scorecard KPI.
            val_str = f' (last: {fmt_compact(last_val)}, last cycle)' if last_val is not None else ''
            dir_html = (
                f'<div class="now-item"><span class="now-label">Direction:</span> '
                f'<span class="badge badge-researching">{esc(current_dir)}</span> '
                f'<span class="badge badge-available">{esc(status)}</span> '
                f'<span class="now-detail" title="source: tech-tree portfolio, last cycle measurement">'
                f'lever: <strong>{esc(lever_metric)}</strong> &middot; '
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

    # 3. Demand snapshot -- chips grouped by id prefix (goal-gap / defect /
    # priority / other) with counts; individual ids kept in tooltips. No
    # human-readable labels exist in collected demand data, so grouping is
    # the readable fallback (issue #41).
    def _demand_group(gid: str) -> str:
        for prefix in ('goal-gap', 'defect', 'priority'):
            if gid.startswith(prefix):
                return prefix
        return 'other'

    def _render_demand_groups(groups: dict[str, list[tuple[str, str]]], chip_cls: str, verb: str) -> str:
        order = [p for p in ('goal-gap', 'defect', 'priority', 'other') if groups.get(p)]
        parts = []
        for prefix in order:
            items = groups[prefix]
            count = len(items)
            shown = items[:8]
            tip = '; '.join(f'{gid} ({verb} {detail})' for gid, detail in shown)
            if count > len(shown):
                tip += f'; +{count - len(shown)} more'
            label = prefix if prefix != 'other' else 'other'
            parts.append(
                f'<span class="demand-chip {chip_cls} demand-group" translate="no" title="{esc(tip)}">'
                f'{esc(label)} &times;{count}</span>'
            )
        return ' '.join(parts) if parts else '<em>none</em>'

    demand_html = '<p class="unavailable-note">demand snapshot unavailable</p>'
    served_groups: dict[str, list[tuple[str, str]]] = {}
    if isinstance(demand_rotation, dict):
        served = demand_rotation.get('served')
        if isinstance(served, dict):
            for gid, ts in served.items():
                served_groups.setdefault(_demand_group(str(gid)), []).append((str(gid), str(ts)))

    completed_groups: dict[str, list[tuple[str, str]]] = {}
    if isinstance(demand_completed, dict):
        entries = demand_completed.get('entries')
        if isinstance(entries, dict):
            for gid, cinfo in entries.items():
                cid = cinfo.get('cycle_id', '') if isinstance(cinfo, dict) else ''
                completed_groups.setdefault(_demand_group(str(gid)), []).append((str(gid), str(cid)))

    if served_groups or completed_groups:
        s_part = f'<div class="demand-subgroup"><span class="demand-sublabel">Served:</span> {_render_demand_groups(served_groups, "served", "served")}</div>'
        c_part = f'<div class="demand-subgroup"><span class="demand-sublabel">Completed:</span> {_render_demand_groups(completed_groups, "completed", "cycle")}</div>'
        demand_html = f'<div class="now-demand-grid">{s_part}{c_part}</div>'

    return f'''
    <section class="panel panel-now" id="panel-now">
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
    llm_stats: dict[str, Any] | None = None,
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
    <section class="panel panel-feed" id="panel-feed">
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

    # Map evolution tree nodes by cycle_id and sha
    tree_by_cycle: dict[str, dict[str, Any]] = {}
    tree_by_sha: dict[str, dict[str, Any]] = {}
    if isinstance(evolution_tree, dict):
        nodes = evolution_tree.get('nodes')
        if isinstance(nodes, dict):
            for sha, node in nodes.items():
                if isinstance(node, dict):
                    if node.get('cycle_id'):
                        tree_by_cycle[str(node.get('cycle_id'))] = node
                    tree_by_sha[str(sha)] = node
                    tree_by_sha[short_sha(str(sha))] = node

    titles_map = task_titles if isinstance(task_titles, dict) else {}

    # Build rows (up to 50 latest cycles)
    rows = []
    cycle_items = list(cycles_dict.items())
    # Take latest 50
    for cid, phases in reversed(cycle_items[-50:]):
        # Determine task title
        title = titles_map.get(cid) or titles_map.get(cid.replace('cycle-', ''))
        
        # Outcome derivation from phases
        outcome_kind = 'in_progress'
        outcome_label = 'running'
        badge_class = 'badge-available'
        reason = ''
        files_changed = []
        metric_delta = ''
        ts_val = ''
        derived_title = ''

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

        # Scan phases for most decisive outcome and reason
        # Precedence: outcome > gate fail > proposer_reject > dedup > idle > started
        gate_fail_reason = ''
        reject_reason = ''
        dedup_reason = ''
        idle_reason = ''
        started_seen = False
        outcome_status = None
        outcome_reason = ''

        for p in phases:
            if not ts_val and p.get('ts'):
                ts_val = str(p.get('ts'))
            phase_name = p.get('phase')
            if phase_name == 'started':
                started_seen = True
            elif phase_name == 'gate':
                gate_passed = p.get('passed') or p.get('status') == 'passed' or p.get('smoke_passed')
                if not gate_passed:
                    outcome_kind = 'gate_blocked'
                    gate_fail_reason = str(p.get('reason') or 'blocked')
                    reason = gate_fail_reason
            elif phase_name == 'proposer_reject':
                outcome_kind = 'rejected'
                reject_reason = str(p.get('reason') or 'proposer reject')
                reason = reject_reason
            elif phase_name == 'dedup':
                # Issue #59: a dedup skip is a TERMINAL outcome, not
                # in-progress. Detect it from any of the field shapes the
                # ledger uses (duplicate flag, status, decision string).
                decision = str(p.get('decision') or '')
                if (
                    p.get('duplicate')
                    or p.get('status') == 'duplicate'
                    or 'skip' in decision
                    or 'duplicate' in decision
                ):
                    outcome_kind = 'skipped'
                    dedup_reason = str(p.get('reason') or decision or 'duplicate')
                    reason = dedup_reason
            elif phase_name == 'idle':
                outcome_kind = 'idle'
                idle_reason = str(p.get('reason') or 'no demand')
                reason = idle_reason
            elif phase_name == 'outcome':
                st = p.get('status') or (p.get('outcome') if isinstance(p.get('outcome'), str) else None)
                outcome_status = st
                if st in ('success', 'integrated'):
                    outcome_kind = 'integrated'
                elif st in ('fail', 'failed'):
                    outcome_kind = 'failed'
                    if p.get('reason'):
                        outcome_reason = str(p.get('reason'))
                        reason = outcome_reason
                elif st == 'partial':
                    outcome_kind = 'partial'
                    if p.get('reason'):
                        outcome_reason = str(p.get('reason'))
                        reason = outcome_reason
                elif st == 'skipped':
                    outcome_kind = 'skipped'
                    if p.get('reason'):
                        outcome_reason = str(p.get('reason'))
                        reason = outcome_reason
                if p.get('delta') is not None:
                    metric_delta = str(p.get('delta'))
                elif p.get('metric_delta') is not None:
                    metric_delta = str(p.get('metric_delta'))

        # In Lane B, nodes can be referenced by cycle_id or sha
        tree_node_match = tree_by_cycle.get(cid) or tree_by_sha.get(cid)
        node_link_html = ''
        if tree_node_match:
            outcome_kind = 'integrated'
            tree_node = tree_node_match
            fitness = tree_node.get('fitness') if isinstance(tree_node.get('fitness'), dict) else {}
            if fitness.get('reward') is not None:
                metric_delta = f"reward: {fitness.get('reward')}"
            sha_val = ''
            for s, n in tree_by_sha.items():
                if n is tree_node and len(s) > 8:
                    sha_val = s
                    break
            if sha_val:
                node_link_html = f'<a href="#node-{esc(short_sha(sha_val))}" class="feed-tree-link">tree &#8599;</a>'

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
            badge_class = 'badge-partial'
            # Issue #59: surface the reason when the ledger carries one.
            outcome_label = f'PARTIAL{(": " + reason) if reason else ""}'
        elif outcome_kind == 'skipped':
            # Issue #59: terminal outcome, never rendered as running.
            badge_class = 'badge-skipped'
            outcome_label = f'SKIPPED{(": " + reason) if reason else ""}'

        # If title is missing from cycle_titles/merge commits, derive human-readable reason
        if not title:
            if outcome_status:
                if outcome_reason:
                    derived_title = f"{outcome_status}: {outcome_reason}"
                elif outcome_status == 'partial':
                    # Issue #59: never a bare word -- say what is missing.
                    derived_title = 'partial: no artifact recorded'
                else:
                    derived_title = outcome_status
            elif gate_fail_reason:
                derived_title = f"gate blocked: {gate_fail_reason}"
            elif reject_reason:
                derived_title = f"rejected: {reject_reason}"
            elif dedup_reason:
                derived_title = f"skipped: {dedup_reason}"
            elif idle_reason:
                derived_title = f"idle: {idle_reason}"
            elif started_seen:
                derived_title = "in progress"
            else:
                derived_title = cid
            title = derived_title

        files_html = ''
        if files_changed:
            if len(files_changed) > 3:
                # Issue #45: full list must be reachable without hover --
                # expandable <details>, summary keeps the first-3 + '+N more'.
                files_str = ', '.join(files_changed[:3]) + f' +{len(files_changed)-3} more'
                items = ''.join(f'<li>{esc(p)}</li>' for p in files_changed)
                files_html = (
                    f'<details class="feed-files"><summary>&#128193; {esc(files_str)}</summary>'
                    f'<ul class="feed-files-list">{items}</ul></details>'
                )
            else:
                files_html = f'<div class="feed-files">&#128193; {esc(", ".join(files_changed))}</div>'

        delta_html = ''
        if metric_delta:
            try:
                delta_html = f'<span class="feed-delta">{fmt_compact(float(metric_delta), signed=True)}</span>'
            except (TypeError, ValueError):
                delta_html = f'<span class="feed-delta">{esc(metric_delta)}</span>'
        ts_html = f'<span class="feed-ts" title="{esc(str(ts_val))}">{fmt_ts_short(ts_val)}</span>' if ts_val else ''

        # Issue #60: per-cycle LLM cost line (calls / tokens / duration),
        # plus a budget-pressure marker when a call hit finish_reason=length.
        cost_html = ''
        st = None
        if isinstance(llm_stats, dict):
            st = llm_stats.get(cid)
            if st is None:
                st = llm_stats.get(cid.replace('cycle-', '', 1))
        if isinstance(st, dict) and st.get('calls'):
            parts_cost = [f'&#9889; {st["calls"]} calls']
            tok = st.get('total_tokens')
            if isinstance(tok, (int, float)) and not isinstance(tok, bool) and tok > 0:
                parts_cost.append(f'{fmt_compact(tok)} tok')
            dur = _fmt_duration_ms(st.get('duration_ms'))
            if dur:
                parts_cost.append(f'dur {dur}')
            cost_html = '<div class="feed-cost">' + ' &middot; '.join(parts_cost)
            if st.get('any_length'):
                cost_html += (
                    ' <span class="cost-pressure" title="a call in this cycle ended with '
                    'finish_reason=length (context/budget limit hit)">&#9888; context overflow</span>'
                )
            cost_html += '</div>'

        rows.append(f'''
        <li class="feed-row feed-outcome-{outcome_kind}" id="cycle-{esc(cid)}">
          <div class="feed-header">
            <span class="badge {badge_class}">{esc(outcome_label)}</span>
            <strong class="feed-title">{esc(title)}</strong>
            <span class="feed-cid copyable" translate="no">({esc(cid)})</span>
            {delta_html}
            {node_link_html}
            {ts_html}
          </div>
          {files_html}
          {cost_html}
        </li>
        ''')

    return f'''
    <section class="panel panel-feed" id="panel-feed">
      <h2 class="panel-title">Cycle Feed (Recent {len(rows)})</h2>
      <ul class="feed-list">
        {''.join(rows)}
      </ul>
    </section>
    '''


def build_hypotheses_panel(
    hypotheses_lifecycle: dict[str, Any] | None,
    hypotheses: dict[str, Any] | None = None,
    feed_cycles: set[str] | None = None,
    now: datetime | None = None,
) -> str:
    # Accept either hypotheses_lifecycle or legacy hypotheses dict
    entries_dict = {}
    if isinstance(hypotheses_lifecycle, dict) and isinstance(hypotheses_lifecycle.get('entries'), dict):
        entries_dict = hypotheses_lifecycle.get('entries')
    elif isinstance(hypotheses, dict) and isinstance(hypotheses.get('entries'), dict):
        entries_dict = hypotheses.get('entries')
    elif hypotheses_lifecycle is None and hypotheses is None:
        return unavailable_panel('Hypotheses Lifecycle', 'hypotheses unavailable')

    if not entries_dict:
        return '''
        <section class="panel panel-hypotheses" id="panel-hypotheses">
          <h2 class="panel-title">Hypotheses Lifecycle</h2>
          <p class="unavailable-note">no hypotheses recorded</p>
        </section>
        '''

    valid_feed_cycles = set(feed_cycles) if feed_cycles else set()

    # Partition raw entries into active candidates and answered candidates
    raw_active: list[dict[str, Any]] = []
    raw_answered: list[dict[str, Any]] = []

    for hid, info in entries_dict.items():
        if not isinstance(info, dict):
            continue
        status = str(info.get('status') or 'open').lower()
        verdict = info.get('verdict')
        answered_at = info.get('answered_at')
        item = dict(info)
        item['id'] = hid

        if (
            'answered' in status
            or status in ('supported', 'refuted', 'inconclusive', 'accepted', 'rejected', 'abandoned')
            or verdict
            or answered_at
        ):
            raw_answered.append(item)
        else:
            raw_active.append(item)

    # Deduplicate within each group by exact title, keeping the most recently touched
    def dedupe_by_title(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            t = str(it.get('title') or it.get('id') or '')
            groups.setdefault(t, []).append(it)
        result: list[dict[str, Any]] = []
        for t, g in groups.items():
            best = max(g, key=lambda x: str(x.get('last_touched') or x.get('first_seen') or ''))
            result.append(best)
        return result

    deduped_active = dedupe_by_title(raw_active)
    deduped_answered = dedupe_by_title(raw_answered)

    # Reference time for stale detection (>14 days)
    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)

    # Classify active entries into active vs stale
    def is_stale(item: dict[str, Any]) -> bool:
        st = str(item.get('status') or '').lower()
        if st == 'stale':
            return True
        lt = str(item.get('last_touched') or item.get('first_seen') or '').strip()
        if lt:
            dt = _parse_iso_ts(lt)
            if dt:
                if (ref_now - dt).total_seconds() > 14 * 86400:
                    return True
        return False

    fresh_active: list[dict[str, Any]] = []
    stale_active: list[dict[str, Any]] = []
    for it in deduped_active:
        if is_stale(it):
            stale_active.append(it)
        else:
            fresh_active.append(it)

    # Sort each subgroup by most recently touched first
    def sort_key(it: dict[str, Any]) -> str:
        return str(it.get('last_touched') or it.get('first_seen') or '')

    fresh_active.sort(key=sort_key, reverse=True)
    stale_active.sort(key=sort_key, reverse=True)
    deduped_answered.sort(key=lambda it: str(it.get('answered_at') or it.get('last_touched') or ''), reverse=True)

    def render_active_item(info: dict[str, Any], is_stale_flag: bool) -> str:
        hid = info.get('id') or ''
        status = str(info.get('status') or 'open').lower()
        title = info.get('title') or hid
        first_seen = info.get('first_seen') or ''
        last_touched = info.get('last_touched') or ''
        badge_cls = 'badge-stale' if is_stale_flag else 'badge-researching'
        badge_lbl = 'STALE' if (is_stale_flag and status != 'stale') else status.upper()
        return f'''
            <li class="hypo-row active">
              <span class="badge {badge_cls}">{esc(badge_lbl)}</span>
              <strong class="hypo-title">{esc(title)}</strong>
              <div class="hypo-meta">
                {f'<span class="hypo-ts" title="{esc(first_seen)}">seen {fmt_ts_short(first_seen, now=ref_now)}</span>' if first_seen else ""}
                {f'<span class="hypo-ts" title="{esc(last_touched)}">touched {fmt_ts_short(last_touched, now=ref_now)}</span>' if last_touched else ""}
              </div>
            </li>
        '''

    def render_answered_item(info: dict[str, Any]) -> str:
        hid = info.get('id') or ''
        status = str(info.get('status') or 'open').lower()
        title = info.get('title') or hid
        answered_ev = info.get('answered_evidence') or ''
        answered_at = info.get('answered_at') or ''
        verdict = info.get('verdict')

        ev_html = ''
        if answered_ev:
            ev_str = str(answered_ev)
            target_cid = ev_str if ev_str.startswith('cycle-') else f"cycle-{ev_str}"
            clean_cid = ev_str[6:] if ev_str.startswith('cycle-') else ev_str
            if target_cid in valid_feed_cycles or clean_cid in valid_feed_cycles or ev_str in valid_feed_cycles:
                ev_html = f'<span class="hypo-ev">evidence: <a href="#cycle-{esc(ev_str)}">{esc(ev_str)}</a></span>'
            else:
                ev_html = f'<span class="hypo-ev">evidence: {esc(ev_str)}</span>'

        v_label = str(verdict or status).upper()
        badge_class = {
            'SUPPORTED': 'verdict-supported',
            'REFUTED': 'verdict-refuted',
            'INCONCLUSIVE': 'verdict-inconclusive',
            'ANSWERED': 'badge-integrated',
        }.get(v_label, 'badge-integrated')
        return f'''
            <li class="hypo-row answered">
              <span class="badge {badge_class}">{esc(v_label)}</span>
              <strong class="hypo-title">{esc(title)}</strong>
              <div class="hypo-meta">
                {ev_html}
                {f'<span class="hypo-ts" title="{esc(answered_at)}">answered {fmt_ts_short(answered_at, now=ref_now)}</span>' if answered_at else ""}
              </div>
            </li>
        '''

    fresh_rendered = [render_active_item(x, False) for x in fresh_active]
    stale_rendered = [render_active_item(x, True) for x in stale_active]
    answered_rendered = [render_answered_item(x) for x in deduped_answered]

    total_active_count = len(fresh_active) + len(stale_active)

    if not fresh_rendered and not stale_rendered:
        active_body = '<li class="unavailable-note">none active</li>'
    elif len(stale_rendered) > 6:
        stale_count = len(stale_rendered)
        fresh_part = "".join(fresh_rendered)
        stale_list_html = f'<ul class="hypo-list">{"".join(stale_rendered)}</ul>'
        collapse_part = f'''
        <details class="hypo-details">
          <summary class="hypo-summary">{stale_count} stale hypotheses &mdash; show</summary>
          {stale_list_html}
        </details>
        '''
        active_body = f'{fresh_part}{collapse_part}'
    else:
        active_body = "".join(fresh_rendered + stale_rendered)

    active_html = f'<div class="hypo-group"><h3>Active ({total_active_count})</h3><ul class="hypo-list">{active_body}</ul></div>'

    if answered_rendered:
        ans_count = len(answered_rendered)
        answered_list_html = f'<ul class="hypo-list">{"".join(answered_rendered)}</ul>'
        answered_html = f'''<div class="hypo-group">
          <details class="hypo-details">
            <summary class="hypo-summary"><h3>Answered ({ans_count}) &mdash; show</h3></summary>
            {answered_list_html}
          </details>
        </div>'''
    else:
        answered_html = '<div class="hypo-group"><h3>Answered (0)</h3><ul class="hypo-list"><li class="unavailable-note">none answered</li></ul></div>'

    return f'''
    <section class="panel panel-hypotheses" id="panel-hypotheses">
      <h2 class="panel-title">Hypotheses Lifecycle</h2>
      <div class="hypo-split">
        {active_html}
        {answered_html}
      </div>
    </section>
    '''


def _host_identity(host: str | None, agents_md: str | None) -> str:
    """Issue #39: hardware identity line derived ONLY from already-collected
    data (host name + patterns found in the instance AGENTS.md text).
    Renders nothing when there is less than one hardware fact -- never
    fabricates specs, never performs new host reads."""
    parts: list[str] = []
    if host:
        parts.append(str(host))
    if agents_md:
        text = str(agents_md)[:4000]
        for pattern in (r'\b(?:i386|x86_64|amd64|armv?\d+)\b', r'Debian \d+', r'\b\d+ ?GB RAM\b', r'Python 3\.\d+'):
            m = re.search(pattern, text)
            if m:
                parts.append(m.group(0))
    if len(parts) <= 1:
        return ''
    # Issue #62: join with a literal middle dot BEFORE esc() -- joining with
    # the entity '&middot;' got double-escaped to '&amp;middot;' and rendered
    # as literal text. One escape at render, from plain characters only.
    sep = ' \u00b7 '
    return f'<div class="host-identity" translate="no">{esc(sep.join(parts))}</div>'


def build_agent_panel(
    agents_md: str | None,
    goal_text: dict[str, Any] | None,
    skill_reads: dict[str, Any] | None,
    portfolio: dict[str, Any] | None = None,
    ledger_tail: list[dict[str, Any]] | None = None,
    host: str | None = None,
) -> str:
    # 1. AGENTS.md
    if agents_md is not None:
        md_text = agents_md.strip()
        md_body = esc(md_text[:2000]) + ('...' if len(md_text) > 2000 else '')
        # Issue #44: capped scroll boxes are scroll-traps; native <details>
        # keeps the page one scrolling document, closed by default.
        agents_html = (
            f'<details class="charter-details agents-md-box">'
            f'<summary>AGENTS.md charter ({len(md_text.splitlines())} lines)</summary>'
            f'<pre><code>{md_body}</code></pre></details>'
        )
    else:
        agents_html = '<p class="unavailable-note">AGENTS.md unavailable</p>'

    # 2. Goals charter
    goals_html = '<p class="unavailable-note">goals charter unavailable</p>'
    if isinstance(goal_text, dict):
        g_text = goal_text.get('charter') or goal_text.get('goal_text') or goal_text.get('text') or str(goal_text)
        g_body = esc(str(g_text)[:1500])
        goals_html = (
            f'<details class="charter-details goal-text-box">'
            f'<summary>Goals charter ({len(str(g_text).splitlines())} lines)</summary>'
            f'<pre><code>{g_body}</code></pre></details>'
        )

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
    <section class="panel panel-agent" id="panel-agent">
      <h2 class="panel-title">Agent Configuration & Fitness</h2>
      {_host_identity(host, agents_md)}
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


def build_empire_stats_strip(
    scorecard: dict[str, Any] | None,
    age_seconds: float | None = None,
    generated_at: str | None = None,
) -> str:
    if not isinstance(scorecard, dict):
        return unavailable_panel('Empire Stats', 'scorecard unavailable')

    loop = scorecard.get('loop') if isinstance(scorecard.get('loop'), dict) else {}
    cost = scorecard.get('cost') if isinstance(scorecard.get('cost'), dict) else {}
    heldout = scorecard.get('heldout') if isinstance(scorecard.get('heldout'), dict) else {}

    stats = [
        ('integrations', esc(loop.get('integrations', 'n/a'))),
        ('confirmed ratio', humanize_ratio(loop.get('confirmed_integration_ratio'))),
        # Issue #58: the header KPI and the Now-panel lever are DIFFERENT
        # computations of failure rate (scorecard snapshot vs last cycle
        # measurement). Annotate the source visibly + via tooltip instead of
        # recomputing anything dashboard-side.
        ('repeat failure rate · scorecard', humanize_ratio(loop.get('repeat_failure_rate'))),
        ('tokens / integration', humanize_number(cost.get('tokens_per_integration'))),
        ('held-out', f"{esc(heldout.get('passed', 'n/a'))}/{esc(heldout.get('checked', 'n/a'))}"),
    ]

    computed_ts = ''
    if scorecard.get('computed_at_utc'):
        computed_ts = fmt_ts(scorecard.get('computed_at_utc'))
    # Issue #61: every headline number carries a one-line definition so a
    # first-time viewer can tell health from sickness.
    kpi_tooltips = {
        'integrations': 'total cycles whose changes were merged into the evolution lineage',
        'confirmed ratio': 'share of integrations whose effects were later confirmed working in live operation',
        'repeat failure rate · scorecard': (
            'share of cycles repeating a previously seen failure; source: scorecard snapshot'
            + (f' (computed {computed_ts})' if computed_ts else '')
            + ' over all recorded cycles; the Now-panel lever shows the last cycle measurement and may differ'
        ),
        'tokens / integration': 'average LLM tokens spent per integrated cycle',
        'held-out': (
            'held-out validation: pass rate on tasks excluded from the agent\'s own '
            'selection and training; X/Y = passed/checked'
        ),
    }

    # Issue #61: render scorecard-defined targets when present (scorecard
    # carries none today; the page lights up automatically if one appears).
    targets = scorecard.get('targets') if isinstance(scorecard.get('targets'), dict) else {}
    target_key = {
        'integrations': 'integrations',
        'confirmed ratio': 'confirmed_integration_ratio',
        'repeat failure rate · scorecard': 'repeat_failure_rate',
        'tokens / integration': 'tokens_per_integration',
        'held-out': 'heldout',
    }

    def _target_html(label: str) -> str:
        t = targets.get(target_key.get(label, ''))
        if not isinstance(t, (int, float)) or isinstance(t, bool):
            return ''
        return f'<span class="kpi-target">target &le;{esc(fmt_compact(t))}</span>'

    stat_html = ''
    for label, value in stats:
        tip = kpi_tooltips.get(label)
        tip_attr = f' title="{esc(tip)}"' if tip else ''
        stat_html += (
            f'<div class="empire-stat"{tip_attr}>'
            f'<span class="stat-label">{esc(label)}</span>'
            f'<span class="stat-value">{value}</span>'
            f'{_target_html(label)}</div>'
        )

    # Issue #48: visible freshness badge in the header. Age is real
    # (collected mtime) or explicitly unknown -- never fabricated.
    if isinstance(age_seconds, (int, float)) and not isinstance(age_seconds, bool):
        if age_seconds < 3600:
            level = 'fresh'
        elif age_seconds < 21600:
            level = 'stale'
        else:
            level = 'very-stale'
        gen_hhmm = generated_at[11:16] if generated_at and len(generated_at) >= 16 else ''
        freshness_html = (
            f'<span class="freshness freshness-{level}" data-age-seconds="{float(age_seconds):.0f}">'
            f'data: {esc(humanize_age(float(age_seconds)))} old'
            + (f' &middot; generated {esc(gen_hhmm)} UTC' if gen_hhmm else '')
            + '</span>'
        )
    else:
        freshness_html = '<span class="freshness freshness-unknown">data: age unknown</span>'

    return f'''
    <header class="empire-strip">
      <h1 class="empire-title"># eeebot / tech-tree</h1>
      <div class="empire-stats">{stat_html}</div>
      <div class="empire-fresh">{freshness_html}</div>
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
      background: radial-gradient(ellipse at top, #0c1512 0%, #080f0c 60%, #050a08 100%);
      color: #dcebe1;
      font-family: 'Consolas', 'Menlo', 'DejaVu Sans Mono', monospace;
      padding: 0 0 32px 0;
    }
    h1, h2, h3, .panel-title, .empire-title {
      font-family: 'Consolas', 'Menlo', 'DejaVu Sans Mono', monospace;
    }
    a { color: #56d364; }

    /* --- top strip: slim, one line --- */
    .empire-strip {
      background: linear-gradient(180deg, #0f2018 0%, #0a1811 100%);
      border-bottom: 2px solid #56d364;
      padding: 8px 20px;
      display: flex;
      flex-wrap: nowrap;
      overflow-x: auto;
      align-items: center;
      gap: 18px;
    }
    .empire-title {
      color: #56d364;
      font-size: 1.05em;
      letter-spacing: 2px;
      text-shadow: 0 0 10px rgba(86, 211, 100, 0.45);
      white-space: nowrap;
      margin: 0;
      font-weight: 700;
    }
    .empire-stats {
      display: flex;
      flex-wrap: nowrap;
      gap: 18px;
    }
    .empire-stat { display: flex; flex-direction: column; align-items: center; min-width: 78px; white-space: nowrap; }

    /* --- freshness badge (issue #48) --- */
    .empire-fresh { margin-left: auto; white-space: nowrap; }
    .freshness {
      font-size: 0.78em;
      padding: 2px 8px;
      border-radius: 4px;
      border: 1px solid #3d6b52;
      font-variant-numeric: tabular-nums;
    }
    .freshness-fresh { color: #2fd3c4; border-color: rgba(47, 211, 196, 0.5); background: rgba(47, 211, 196, 0.08); }
    .freshness-stale { color: #d19a66; border-color: rgba(209, 154, 102, 0.5); background: rgba(209, 154, 102, 0.08); }
    .freshness-very-stale { color: #e06c75; border-color: rgba(224, 108, 117, 0.5); background: rgba(224, 108, 117, 0.08); }
    .freshness-unknown { color: #9db4a6; }

    .stat-label {
      text-transform: uppercase;
      font-size: 0.64em;
      letter-spacing: 1px;
      color: #9db4a6;
    }
    .stat-value {
      font-size: 1.02em;
      color: #e2f0e6;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
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
      overscroll-behavior: contain;
      border: 1px solid #1e3a2d;
      border-radius: 10px;
      background: linear-gradient(180deg, rgba(15, 32, 24, 0.55) 0%, rgba(8, 15, 11, 0.7) 100%);
      padding-bottom: 4px;
    }
    .tech-canvas { display: block; }
    .canvas-jump {
      display: inline-block;
      margin: 8px 12px 0;
      color: #2fd3c4;
      font-family: 'Consolas', monospace;
      font-size: 0.82em;
      text-decoration: underline;
    }
    .lane-label {
      fill: #56d364;
      font-family: 'Consolas', 'Menlo', 'DejaVu Sans Mono', monospace;
      font-size: 13px;
      letter-spacing: 2px;
    }
    .lane-note { fill: #7fa08d; font-style: italic; font-size: 11px; }
    .lane-unavailable { fill: #7fa08d; font-style: italic; font-size: 12px; }

    .panel {
      background: linear-gradient(180deg, rgba(15, 32, 24, 0.85) 0%, rgba(8, 15, 11, 0.9) 100%);
      border: 1px solid #1e3a2d;
      border-radius: 10px;
      padding: 14px 18px;
      box-shadow: 0 4px 18px rgba(0, 0, 0, 0.35);
    }
    .panel-title {
      margin: 0 0 12px 0;
      color: #56d364;
      font-size: 1.05em;
      letter-spacing: 1px;
      border-bottom: 1px solid #28503c;
      padding-bottom: 6px;
    }
    .panel-unavailable {
      text-align: center;
      color: #7fa08d;
    }
    .unavailable-note {
      color: #7fa08d;
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
      color: #9db4a6;
      min-width: 140px;
      text-transform: uppercase;
      font-size: 0.78em;
      letter-spacing: 0.5px;
    }
    .now-detail {
      color: #b8d0c2;
      font-size: 0.85em;
      margin-left: 6px;
    }
    .now-sub {
      color: #8aa695;
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
      color: #8aa695;
      font-size: 0.8em;
      min-width: 75px;
    }
    .demand-chip {
      font-size: 0.75em;
      padding: 2px 8px;
      border-radius: 4px;
      font-family: 'Consolas', monospace;
      border: 1px solid #28503c;
      background: rgba(12, 22, 17, 0.8);
    }
    .demand-chip.served {
      color: #2fd3c4;
      border-color: rgba(47, 211, 196, 0.4);
    }
    .demand-chip.completed {
      color: #56d364;
      border-color: rgba(86, 211, 100, 0.4);
    }

    /* --- Cycle Feed --- */
    .feed-list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .feed-row {
      background: rgba(12, 22, 17, 0.7);
      border: 1px solid #1e3a2d;
      border-radius: 6px;
      padding: 8px 12px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .feed-row:hover {
      border-color: #2f5c46;
      background: rgba(15, 32, 24, 0.8);
    }
    .feed-outcome-integrated { border-left: 4px solid #2fd3c4; }
    .feed-outcome-failed { border-left: 4px solid #b23a3a; }
    .feed-outcome-gate_blocked { border-left: 4px solid #e06c75; }
    .feed-outcome-rejected { border-left: 4px solid #d19a66; }
    .feed-outcome-idle { border-left: 4px solid #7d9c8a; }
    .feed-outcome-partial { border-left: 4px solid #56d364; }
    .feed-outcome-skipped { border-left: 4px solid #7d9c8a; }
    .feed-outcome-in_progress { border-left: 4px solid #61afef; }

    .feed-header {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      font-size: 0.88em;
    }
    .feed-title {
      color: #e2f0e6;
      font-size: 0.95em;
    }
    /* Issue #38: terminal-style log prefix on feed lines. */
    .feed-title::before {
      content: '>> ';
      color: #56d364;
    }
    .feed-cid {
      color: #8aa695;
      font-family: 'Consolas', monospace;
      font-size: 0.82em;
    }
    .feed-delta {
      color: #2fd3c4;
      font-family: 'Consolas', monospace;
      font-size: 0.8em;
      margin-left: auto;
      font-variant-numeric: tabular-nums;
    }
    .feed-ts {
      color: #8aa695;
      font-size: 0.78em;
      white-space: nowrap;
    }
    .feed-tree-link {
      color: #2fd3c4;
      font-size: 0.78em;
      text-decoration: underline;
      font-family: 'Consolas', monospace;
    }
    .feed-files {
      font-size: 0.78em;
      color: #9db4a6;
      font-family: 'Consolas', monospace;
      padding-left: 4px;
    }
    /* Issue #60: per-cycle LLM cost line + budget-pressure marker. */
    .feed-cost {
      font-size: 0.75em;
      color: #8aa695;
      font-family: 'Consolas', monospace;
      padding-left: 4px;
      font-variant-numeric: tabular-nums;
    }
    .cost-pressure {
      color: #d19a66;
      font-weight: 700;
    }
    .feed-files > summary {
      cursor: pointer;
      user-select: none;
    }
    .feed-files-list {
      margin: 4px 0 0;
      padding-left: 20px;
      font-family: 'Consolas', monospace;
    }
    .feed-files-list li { padding: 1px 0; word-break: break-all; }

    /* --- panel nav + copyable ids (issue #47) --- */
    .panel-nav {
      padding: 6px 20px;
      font-family: 'Consolas', monospace;
      font-size: 0.82em;
      border-bottom: 1px solid #182a20;
    }
    .panel-nav a { color: #2fd3c4; text-decoration: none; }
    .panel-nav a:hover { text-decoration: underline; }
    .copyable { cursor: pointer; }
    .copyable.copied { color: #2fd3c4; }

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
      color: #9db4a6;
      text-transform: uppercase;
      letter-spacing: 1px;
      margin: 0 0 8px 0;
      border-bottom: 1px solid #182a20;
      padding-bottom: 4px;
    }
    .hypo-details { margin-top: 8px; }
    .hypo-summary {
      font-size: 0.82em;
      color: #9db4a6;
      cursor: pointer;
      padding: 4px 0;
      user-select: none;
    }
    .hypo-summary:hover { color: #56d364; }
    .hypo-details ul.hypo-list { margin-top: 6px; }
    .hypo-list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .hypo-row {
      background: rgba(12, 22, 17, 0.7);
      border: 1px solid #1e3a2d;
      border-radius: 6px;
      padding: 8px 10px;
      font-size: 0.85em;
    }
    .hypo-title {
      color: #e2f0e6;
      margin-left: 6px;
    }
    .hypo-meta {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 0.8em;
      color: #8aa695;
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
      color: #9db4a6;
      text-transform: uppercase;
      letter-spacing: 1px;
      margin: 0 0 8px 0;
      border-bottom: 1px solid #182a20;
      padding-bottom: 4px;
    }
    /* Issue #39: hardware identity line (derived from collected data only). */
    .host-identity {
      font-size: 0.8em;
      color: #8aa695;
      margin: 0 0 10px 0;
      padding: 3px 8px;
      border: 1px dashed #28503c;
      border-radius: 4px;
      display: inline-block;
    }
    .agents-md-box pre, .goal-text-box pre {
      margin: 0;
      background: rgba(8, 15, 11, 0.9);
      border: 1px solid #1e3a2d;
      border-radius: 6px;
      padding: 10px;
      font-family: 'Consolas', monospace;
      font-size: 0.78em;
      color: #cfe3d7;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .charter-details > summary {
      font-size: 0.82em;
      color: #9db4a6;
      cursor: pointer;
      padding: 4px 0;
      user-select: none;
    }
    .charter-details > summary:hover { color: #56d364; }
    .skills-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.8em;
      background: rgba(8, 15, 11, 0.6);
      border-radius: 6px;
      overflow: hidden;
      font-variant-numeric: tabular-nums;
    }
    .skills-table th {
      text-align: left;
      padding: 6px 8px;
      background: #0f2018;
      color: #9db4a6;
      font-size: 0.75em;
      text-transform: uppercase;
      border-bottom: 1px solid #1e3a2d;
    }
    .skills-table td {
      padding: 6px 8px;
      border-bottom: 1px solid #182a20;
    }
    .skill-high-ratio {
      background: rgba(86, 211, 100, 0.08);
    }
    .skill-reads {
      font-weight: 700;
      color: #56d364;
      font-family: 'Consolas', monospace;
      text-align: center;
    }
    .skill-untracked {
      color: #7d9c8a;
      font-style: italic;
    }

    /* --- direction boxes (Lane A / RESEARCH) --- */
    .dir-box {
      width: 100%;
      height: 100%;
      background: rgba(12, 22, 17, 0.94);
      border: 1px solid #28503c;
      border-radius: 8px;
      padding: 10px;
      position: relative;
      font-family: 'Consolas', 'Menlo', 'DejaVu Sans Mono', monospace;
      color: #dcebe1;
    }
    .dir-box-current {
      border-color: #56d364;
      box-shadow: 0 0 16px rgba(86, 211, 100, 0.65);
    }
    .dir-box-plateaued { opacity: 0.7; border-color: #6d3232; }
    .dir-box-dim { opacity: 0.55; }
    .dir-box-head { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
    .dir-glyph { font-size: 1.1em; color: #56d364; }
    .dir-name { font-weight: 600; color: #e2f0e6; font-size: 0.92em; }
    .dir-lever {
      font-variant: small-caps;
      letter-spacing: 0.5px;
      color: #9db4a6;
      font-size: 0.76em;
      margin-bottom: 6px;
    }
    .ribbon {
      position: absolute;
      top: -8px;
      right: -8px;
      background: #56d364;
      color: #04240f;
      font-size: 0.6em;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 4px;
      transform: rotate(4deg);
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.4);
    }
    .cooldown { font-size: 0.68em; color: #9db4a6; margin-top: 4px; }

    .dir-elbow { fill: none; stroke: #2fd3c4; stroke-width: 2.2; opacity: 0.9; }
    .mint-elbow { fill: none; stroke: #56d364; stroke-width: 2; opacity: 0.85; }
    .mint-glyph { fill: #56d364; font-size: 15px; }

    /* --- evolution boxes (Lane B / DGM LINEAGE) --- */
    .evo-box {
      width: 100%;
      height: 100%;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 2px;
      background: rgba(12, 22, 17, 0.9);
      border: 1.5px solid #3d6b52;
      border-radius: 7px;
      padding: 4px 8px;
      font-family: 'Consolas', 'Courier New', monospace;
      font-size: 11px;
      color: #cfe3d7;
    }
    .evo-box-current {
      border-color: #56d364;
      box-shadow: 0 0 12px rgba(86, 211, 100, 0.75);
      background: rgba(47, 211, 196, 0.12);
    }
    .evo-box-abandoned { opacity: 0.45; }
    .evo-header {
      display: flex;
      align-items: center;
      gap: 4px;
      overflow: hidden;
    }
    .evo-diamond { color: #56d364; font-weight: 700; }
    .evo-box-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; font-weight: 600; color: #e2f0e6; }
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
    .evo-sha { color: #8aa695; }
    .evo-fitness { font-size: 9px; color: #b8d0c2; }
    .evo-elbow { fill: none; stroke: #2f5c46; stroke-width: 1.6; }
    /* Issue #53: thick bright edge along the current node's ancestry. */
    .evo-elbow-best { fill: none; stroke: #e2f0e6; stroke-width: 3.4; opacity: 1; }
    .evo-legend-label { fill: #8aa695; font-size: 9px; font-family: 'Consolas', monospace; }
    .evo-fallback { color: #dcebe1; }

    .badge {
      display: inline-block;
      font-size: 0.64em;
      font-weight: 700;
      letter-spacing: 0.5px;
      padding: 2px 7px;
      border-radius: 4px;
    }
    .badge-partial { background: rgba(139, 150, 173, 0.15); color: #9db4a6; border: 1px solid #3d6b52; }
    .badge-skipped { background: rgba(139, 150, 173, 0.15); color: #9db4a6; border: 1px solid #3d6b52; }
    .badge-stale { background: rgba(139, 150, 173, 0.15); color: #9db4a6; border: 1px solid #3d6b52; }
    .badge-researching { background: rgba(86, 211, 100, 0.22); color: #56d364; border: 1px solid #56d364; }
    .badge-available { background: rgba(139, 150, 173, 0.18); color: #c6dacc; border: 1px solid #3d6b52; }
    .badge-plateaued { background: rgba(178, 58, 58, 0.15); color: #d97b7b; border: 1px solid #6d3232; }
    .badge-integrated { background: rgba(47, 211, 196, 0.2); color: #2fd3c4; border: 1px solid #2fd3c4; }
    .badge-failed { background: rgba(178, 58, 58, 0.2); color: #e06c75; border: 1px solid #b23a3a; }
    .badge-blocked { background: rgba(224, 108, 117, 0.2); color: #e06c75; border: 1px solid #e06c75; }
    .badge-rejected { background: rgba(209, 154, 102, 0.2); color: #d19a66; border: 1px solid #d19a66; }
    .verdict-supported { background: rgba(86, 211, 100, 0.18); color: #56d364; border: 1px solid #56d364; }
    .verdict-refuted { background: rgba(178, 58, 58, 0.18); color: #d97b7b; border: 1px solid #6d3232; }
    .verdict-inconclusive { background: rgba(139, 150, 173, 0.18); color: #c6dacc; border: 1px solid #3d6b52; }

    .spark { margin-bottom: 2px; }
    .spark-top, .spark-bottom {
      display: flex;
      align-items: flex-end;
      gap: 2px;
      height: 14px;
    }
    .spark-bottom { align-items: flex-start; }
    .spark-baseline { border-top: 1px dashed #2f5c46; }
    .bar { width: 5px; border-radius: 1px; display: block; }
    .bar-pos { background: #56d364; }
    .bar-neg { background: #b23a3a; }
    .bar-placeholder { height: 0; background: transparent; }
    .spark-empty { color: #7fa08d; font-style: italic; font-size: 0.74em; margin-bottom: 4px; }
    .spark-mean { font-size: 0.7em; margin-top: 2px; }
    /* Issue #61: scorecard-defined KPI target badge. */
    .kpi-target {
      font-size: 0.62em;
      color: #8aa695;
      font-family: 'Consolas', monospace;
      white-space: nowrap;
    }    .mean-pos { color: #56d364; }
    .mean-neg { color: #d97b7b; }

    footer.page-footer {
      text-align: center;
      color: #6f9480;
      font-size: 0.78em;
      margin-top: 20px;
      padding-top: 14px;
    }
    .footer-computed {
      color: #7d8aa3;
    }
'''

PAGE_TEMPLATE = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>eeebot Tech Tree</title>
<style>{css}</style>
</head>
<body>
{empire_strip}
{panel_nav}
<main class="dashboard-main">
{now_panel}
{canvas}
{cycle_feed}
{hypotheses_panel}
{agent_panel}
</main>
<footer class="page-footer">generated {generated_at} UTC &middot; host {host} &middot; newest source {source_age}{computed_note}{error_note}{titles_note}</footer>
<script>
document.querySelectorAll('.copyable').forEach(function (el) {{
  el.addEventListener('click', function () {{
    navigator.clipboard.writeText(el.textContent.trim()).then(function () {{
      el.classList.add('copied');
      setTimeout(function () {{ el.classList.remove('copied'); }}, 800);
    }});
  }});
}});
</script>
<script>
(function () {{
  var el = document.querySelector('.freshness[data-age-seconds]');
  if (!el) return;
  var t0 = Date.now();
  var a0 = parseFloat(el.getAttribute('data-age-seconds'));
  setInterval(function () {{
    var a = a0 + (Date.now() - t0) / 1000;
    var txt = a < 90 ? a.toFixed(0) + 's' : a < 5400 ? (a / 60).toFixed(0) + 'm' : a < 172800 ? (a / 3600).toFixed(0) + 'h' : (a / 86400).toFixed(0) + 'd';
    el.textContent = el.textContent.replace(/^data: [^ ]+ old/, 'data: ' + txt + ' old');
  }}, 30000);
}})();
</script>
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

    computed_note = ''
    if isinstance(scorecard, dict) and scorecard.get('computed_at_utc'):
        computed_note = f' &middot; <span class="footer-computed">scorecard computed {fmt_ts(scorecard.get("computed_at_utc"))}</span>'

    # Age of the newest source file this page was built from (issue #27) --
    # the second half of the freshness marker. Never fabricated: if no
    # reader (local or remote) could establish a real mtime, say so plainly
    # instead of printing a 0 or omitting the field silently.
    age_seconds = data.get('_newest_source_age_seconds')
    if isinstance(age_seconds, (int, float)):
        source_age = f'{humanize_age(age_seconds)} old'
    else:
        source_age = 'age unknown'

    empire_strip = build_empire_stats_strip(
        scorecard,
        age_seconds=age_seconds if isinstance(age_seconds, (int, float)) else None,
        generated_at=generated_at,
    )
    # Issue #47: compact in-page section navigation.
    panel_nav = (
        '<nav class="panel-nav" aria-label="Sections">'
        '<a href="#panel-now">now</a> &middot; '
        '<a href="#panel-lineage">lineage</a> &middot; '
        '<a href="#panel-feed">feed</a> &middot; '
        '<a href="#panel-hypotheses">hypotheses</a> &middot; '
        '<a href="#panel-agent">agent</a>'
        '</nav>'
    )
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
    # Determine which cycle IDs are rendered in the feed (for evidence linking)
    feed_cycles = set()
    if isinstance(ledger_tail, list):
        feed_cycles = {
            str(r.get('cycle_id') or '')
            for r in ledger_tail
            if isinstance(r, dict) and r.get('cycle_id')
        }

    cycle_feed = build_cycle_feed(
        ledger_tail=ledger_tail,
        demand_completed=demand_completed,
        task_titles=cycle_titles,
        evolution_tree=evolution_tree,
        cycle_files=data.get('cycle_files'),
        llm_stats=data.get('llm_stats'),
    )
    hypotheses_panel = build_hypotheses_panel(hypotheses, feed_cycles=feed_cycles)
    agent_panel = build_agent_panel(
        agents_md=agents_md,
        goal_text=goal_text,
        skill_reads=skill_reads,
        portfolio=portfolio,
        ledger_tail=ledger_tail,
        host=host,
    )

    return PAGE_TEMPLATE.format(
        css=CSS,
        empire_strip=empire_strip,
        panel_nav=panel_nav,
        now_panel=now_panel,
        canvas=canvas_html,
        cycle_feed=cycle_feed,
        hypotheses_panel=hypotheses_panel,
        agent_panel=agent_panel,
        generated_at=esc(generated_at),
        host=esc(host),
        source_age=esc(source_age),
        computed_note=computed_note,
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
