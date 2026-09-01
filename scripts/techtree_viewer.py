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
import gzip
import hashlib
import html
import json
import re
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone, timedelta
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
LEDGER_TAIL_LIMIT = 5000
LEDGER_SCAN_WINDOW = 20000
# Issue #72/#73: full-history window (days) for ledger + lessons archives.
LEDGER_HISTORY_DAYS = 90

# Baked-in generator SHA (issue #101).
# This sentinel is replaced with the real short git SHA by deploy_generator.sh
# at deploy time (via `sed -i`).  When running directly from the repo the value
# is empty and _generator_sha() falls back to `git rev-parse --short HEAD`.
# Format: exactly 7 hex chars, no surrounding whitespace.  Never edit manually.
_BAKED_GENERATOR_SHA: str = ''

# Read every source fail-soft, from a single remote python3 process fed over
# stdin. This keeps the whole fetch to exactly one SSH round-trip and avoids
# leaving any temp files behind on the remote host.
REMOTE_READER_SCRIPT = r'''
import gzip
import json
import os
import subprocess
import time

STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"
INSTANCE_REPO = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"
LEDGER_PHASES = {
    "started", "outcome", "gate", "proposer_reject", "dedup", "idle",
    "evolution_tree", "tech_tree", "hypothesis",
}
LEDGER_TAIL_LIMIT = 5000
LEDGER_SCAN_WINDOW = 20000
LEDGER_HISTORY_DAYS = 90

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


def read_proposer_stats():
    """Issue #63: proposer visibility -- aggregate llm_calls rows with
    component == 'proposer' (per-day calls/tokens/mean latency + last model).
    Fail-soft per file and per line; mirrors read_llm_stats_local's proposer
    pass in techtree_viewer.py -- keep in sync."""
    ldir = os.path.join(STATE_ROOT, "llm_calls")
    out = {"calls": 0, "total_tokens": 0, "duration_ms": 0.0,
           "last_model": None, "last_ts": "", "days": {}}
    try:
        names = sorted(f for f in os.listdir(ldir) if f.endswith(".jsonl"))
    except Exception:
        return None
    found = False
    for name in names[-7:]:
        day = name[:-6]
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
            if not isinstance(row, dict) or row.get("component") != "proposer":
                continue
            found = True
            out["calls"] += 1
            tok = row.get("total_tokens")
            if isinstance(tok, (int, float)) and not isinstance(tok, bool):
                out["total_tokens"] += tok
            dur = row.get("duration_ms")
            if isinstance(dur, (int, float)) and not isinstance(dur, bool):
                out["duration_ms"] += dur
            model = row.get("model")
            ts = str(row.get("ts") or "")
            if ts >= out["last_ts"]:
                out["last_ts"] = ts
                if model:
                    out["last_model"] = str(model)
            d = out["days"].setdefault(day, {"calls": 0, "total_tokens": 0, "duration_ms": 0.0})
            d["calls"] += 1
            if isinstance(tok, (int, float)) and not isinstance(tok, bool):
                d["total_tokens"] += tok
            if isinstance(dur, (int, float)) and not isinstance(dur, bool):
                d["duration_ms"] += dur
    return out if found else None


def read_ledger_history():
    """Issue #72: FULL cycle history for cycles.html -- live ledger plus
    daily .gz archives, capped at LEDGER_HISTORY_DAYS (documented on the
    page when the cap binds). Fail-soft per file/line (#29 pattern)."""
    ldir = os.path.join(STATE_ROOT, "ledger")
    cutoff = time.time() - LEDGER_HISTORY_DAYS * 86400
    files = []
    try:
        for name in os.listdir(ldir):
            if name == "cycles.jsonl":
                files.append((None, os.path.join(ldir, name)))
            elif name.startswith("cycles-") and name.endswith(".jsonl.gz"):
                day = name[len("cycles-"):-len(".jsonl.gz")]
                try:
                    ts = time.mktime(time.strptime(day, "%Y-%m-%d"))
                except ValueError:
                    continue
                if ts >= cutoff:
                    files.append((day, os.path.join(ldir, name)))
    except Exception:
        return []
    files.sort(key=lambda item: item[0] or "9999", reverse=True)
    matched = []
    for _day, path in files:
        try:
            opener = gzip.open if path.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            _mtimes.append(os.path.getmtime(path))
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("phase") in LEDGER_PHASES:
                matched.append(obj)
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


def read_jsonl(relpath):
    path = os.path.join(STATE_ROOT, relpath)
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        _mtimes.append(os.path.getmtime(path))
    except Exception:
        pass
    return rows


def _parse_lessons_text(text):
    """Issue #73: minimal line-based parser for the machine-written flat
    lessons.yaml shape ('- id:' blocks with 2-space-indented fields and
    deeper-indented continuations). Used when PyYAML is unavailable; also
    the test path so the fallback is always exercised."""
    lessons = []
    cur = None
    last_key = None
    for raw in text.splitlines():
        if not raw.strip() or raw.strip() == 'lessons:':
            continue
        if raw.lstrip().startswith('- id:'):
            if cur:
                lessons.append(cur)
            cur = {"id": raw.split(':', 1)[1].strip(), "date": "", "cycle_id": "",
                   "task_id": "", "hypothesis": "", "result": "", "insight": "",
                   "problem": "", "solution": ""}
            last_key = "id"
            continue
        if cur is None:
            continue
        stripped = raw.strip()
        if raw.startswith('    ') and not stripped.startswith('- ') and last_key:
            # continuation of the previous field (folded multi-line string)
            cur[last_key] = (str(cur[last_key]) + ' ' + stripped).strip()
            continue
        if stripped.startswith('- '):
            continue  # files_changed list items -- not rendered
        if ':' in stripped and raw.startswith('  '):
            key, _, val = stripped.partition(':')
            key = key.strip()
            mapped = {'generalized_insight': 'insight'}.get(key, key)
            if mapped in cur:
                cur[mapped] = val.strip().strip("'").strip()
                last_key = mapped
    if cur:
        lessons.append(cur)
    return lessons


def read_lessons():
    """Issue #73: collect lessons from lessons/lessons.yaml plus
    lessons/archive/*.yaml.gz (rotation archives), dedupe by id, newest
    first. Fail-soft everywhere (#29): an unreadable lessons file must
    never break the publish."""
    lessons_dir = os.path.join(INSTANCE_REPO, "lessons")
    texts = []
    try:
        with open(os.path.join(lessons_dir, "lessons.yaml"), "r", encoding="utf-8", errors="replace") as fh:
            texts.append(fh.read())
        _mtimes.append(os.path.getmtime(os.path.join(lessons_dir, "lessons.yaml")))
    except Exception:
        pass
    try:
        archive_dir = os.path.join(lessons_dir, "archive")
        names = sorted(f for f in os.listdir(archive_dir) if f.endswith(".yaml.gz"))
        cutoff = time.mktime(time.strptime(
            (datetime.now(timezone.utc) - timedelta(days=LEDGER_HISTORY_DAYS)).strftime("%Y-%m-%d"),
            "%Y-%m-%d"))
        for name in names:
            day = name[len("lessons-"):-len(".yaml.gz")]
            try:
                if time.mktime(time.strptime(day, "%Y-%m-%d")) < cutoff:
                    continue
            except Exception:
                pass
            try:
                with gzip.open(os.path.join(archive_dir, name), "rt", encoding="utf-8", errors="replace") as fh:
                    texts.append(fh.read())
                _mtimes.append(os.path.getmtime(os.path.join(archive_dir, name)))
            except Exception:
                continue
    except Exception:
        pass
    by_id = {}
    for text in texts:
        try:
            import yaml  # noqa: PLS290 - optional on the host
            data = yaml.safe_load(text)
            rows = data.get('lessons', []) if isinstance(data, dict) else []
            rows = [r for r in rows if isinstance(r, dict)]
        except Exception:
            rows = _parse_lessons_text(text)
        for row in rows:
            lid = str(row.get('id') or '')
            if not lid:
                continue
            by_id[lid] = {
                "id": lid,
                "date": str(row.get('date') or ''),
                "cycle_id": str(row.get('cycle_id') or ''),
                "task_id": str(row.get('task_id') or ''),
                "hypothesis": str(row.get('hypothesis') or ''),
                "result": str(row.get('result') or ''),
                "insight": str(row.get('generalized_insight') or ''),
                "problem": str(row.get('problem') or ''),
                "solution": str(row.get('solution') or ''),
            }
    return sorted(by_id.values(), key=lambda r: (r.get('date') or '', r.get('id') or ''), reverse=True)


def extract_git_titles(node_shas=None):
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
    if node_shas:
        try:
            joined = subprocess.run(
                ["git", "-C", INSTANCE_REPO, "-c", f"safe.directory={INSTANCE_REPO}", "log", "--no-walk", "--format=%H %s", *node_shas],
                capture_output=True, text=True, timeout=10,
            )
            if joined.returncode == 0:
                for line in joined.stdout.strip().splitlines():
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1].strip():
                        titles[parts[0]] = parts[1].strip()
        except Exception:
            pass
    return titles, cycle_files, None


_tree_for_titles = read_json("evolution/tree.json")
_node_shas_for_titles = list((_tree_for_titles or {}).get("nodes", {}).keys()) if isinstance(_tree_for_titles, dict) else []
_cycle_titles, _cycle_files, _cycle_titles_error = extract_git_titles(_node_shas_for_titles)

result = {
    "portfolio": read_json("tech_tree/portfolio.json"),
    "scorecard": read_json("scorecard/latest.json"),
    "evolution_tree": _tree_for_titles,
    "hypotheses": read_json("hypotheses/lifecycle.json"),
    "hypotheses_durable": read_json("hypotheses/durable.json"),
    "ledger_tail": read_ledger_tail("ledger/cycles.jsonl"),
    "demand_rotation": read_json("demand/rotation.json"),
    "demand_completed": read_json("demand/completed.json"),
    "skill_reads": read_json("skill_fitness/reads.json"),
    "skill_evals": read_jsonl("skill_fitness/evals.jsonl"),
    "llm_stats": read_llm_stats(),
    "proposer_stats": read_proposer_stats(),
    "lessons": read_lessons(),
    "reflections": read_jsonl("reflector/reflections.jsonl"),
    "ledger_history": read_ledger_history(),
    "goal_text": read_json("goals/goal_text.json"),
    "agents_md": read_file_text("AGENTS.md"),
    "cycle_titles": _cycle_titles,
    "cycle_files": _cycle_files,
    "cycle_titles_error": _cycle_titles_error,
    "_source_mtimes": _mtimes,
    "generator_sha": "",
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
        'hypotheses_durable': None,
        'ledger_tail': None,
        'ledger_history': [],
        'demand_rotation': None,
        'demand_completed': None,
        'skill_reads': None,
        'skill_evals': [],
        'llm_stats': {},
        'proposer_stats': None,
        'lessons': [],
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


def extract_git_titles_local(repo_root: Path, node_shas: list[str] | None = None) -> tuple[dict[str, str], dict[str, list[str]], str | None]:
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
    if node_shas:
        try:
            joined = subprocess.run(
                ['git', '-C', repo_str, '-c', f'safe.directory={repo_str}', 'log', '--no-walk', '--format=%H %s', *node_shas],
                capture_output=True, text=True, timeout=10,
            )
            if joined.returncode == 0:
                for line in joined.stdout.strip().splitlines():
                    parts = line.split(' ', 1)
                    if len(parts) == 2 and parts[1].strip():
                        titles[parts[0]] = parts[1].strip()
        except Exception:
            pass
    return titles, cycle_files, None


def _parse_lessons_flat(text: str) -> list[dict[str, str]]:
    """Issue #73: minimal line-based parser for the machine-written flat
    lessons.yaml shape (used when PyYAML is unavailable). Tolerates folded
    multi-line continuations and truncated archive heads."""
    fields = ('date', 'cycle_id', 'task_id', 'hypothesis', 'result', 'generalized_insight', 'problem', 'solution')
    entries: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    last_key: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('- id:'):
            if cur and cur.get('id'):
                entries.append(cur)
            cur = {'id': stripped[len('- id:'):].strip().strip('"\''), 'date': '', 'cycle_id': '',
                   'task_id': '', 'hypothesis': '', 'result': '', 'insight': '',
                   'problem': '', 'solution': ''}
            last_key = None
            continue
        if cur is None:
            continue
        if line.startswith('  - ') or stripped == 'lessons:':
            continue
        if line.startswith('    ') and last_key:
            cur[last_key] = (cur[last_key] + ' ' + stripped).strip()
            continue
        matched = False
        for key in fields:
            prefix = key + ':'
            if stripped.startswith(prefix):
                cur['insight' if key == 'generalized_insight' else key] = stripped[len(prefix):].strip().strip('"\'')
                last_key = 'insight' if key == 'generalized_insight' else key
                matched = True
                break
        if not matched:
            last_key = None
    if cur and cur.get('id'):
        entries.append(cur)
    return entries


def read_local_state(state_root: str, instance_repo: str | None = None) -> dict[str, Any]:
    """Read all state sources directly from `state_root` -- no SSH."""
    empty: dict[str, Any] = {
        'portfolio': None,
        'scorecard': None,
        'evolution_tree': None,
        'hypotheses': None,
        'hypotheses_durable': None,
        'ledger_tail': None,
        'demand_rotation': None,
        'demand_completed': None,
        'skill_reads': None,
        'skill_evals': [],
        'goal_text': None,
        'agents_md': None,
        'cycle_titles': None,
        'cycle_files': {},
        'cycle_titles_error': None,
        'llm_stats': {},
        'proposer_stats': None,
        'lessons': [],
        'reflections': [],
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

    def read_jsonl(relpath: str) -> list[dict[str, Any]]:
        path = root / relpath
        try:
            with path.open('r', encoding='utf-8', errors='replace') as fh:
                rows = []
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        continue
            mtimes.append(path.stat().st_mtime)
        except Exception:  # noqa: BLE001
            return []
        return [row for row in rows if isinstance(row, dict)]

    def read_ledger_history_local() -> list[Any]:
        """Issue #72: full cycle history from ledger/cycles.jsonl plus daily
        cycles-YYYY-MM-DD.jsonl.gz archives (last LEDGER_HISTORY_DAYS days).
        Local mirror of the REMOTE_READER_SCRIPT read_ledger_history() --
        keep in sync. Fail-soft per file/line (issue #29 pattern)."""
        ledger_dir = root / 'ledger'
        try:
            names = sorted(p.name for p in ledger_dir.iterdir())
        except OSError:
            return []
        cutoff = 0.0
        try:
            cutoff = time.mktime(time.strptime(
                (datetime.now(timezone.utc) - timedelta(days=LEDGER_HISTORY_DAYS)).strftime('%Y-%m-%d'),
                '%Y-%m-%d'))
        except Exception:  # noqa: BLE001
            cutoff = 0.0
        matched: list[Any] = []
        for name in reversed(names):
            rows: list[str] = []
            path = ledger_dir / name
            try:
                if name == 'cycles.jsonl':
                    with path.open('r', encoding='utf-8', errors='replace') as fh:
                        rows = fh.readlines()
                    mtimes.append(path.stat().st_mtime)
                elif name.startswith('cycles-') and name.endswith('.jsonl.gz'):
                    day = name[len('cycles-'):-len('.jsonl.gz')]
                    try:
                        file_ts = time.mktime(time.strptime(day, '%Y-%m-%d'))
                    except Exception:  # noqa: BLE001
                        continue
                    if file_ts < cutoff:
                        continue
                    with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as fh:
                        rows = fh.readlines()
                    mtimes.append(path.stat().st_mtime)
                else:
                    continue
            except Exception:  # noqa: BLE001
                continue
            for line in rows:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(obj, dict) and obj.get('phase') in LEDGER_PHASES:
                    matched.append(obj)
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

    def read_proposer_stats_local() -> dict[str, Any] | None:
        """Issue #63: proposer visibility -- local mirror of the
        REMOTE_READER_SCRIPT read_proposer_stats(); keep in sync."""
        out: dict[str, Any] = {'calls': 0, 'total_tokens': 0, 'duration_ms': 0.0,
                               'last_model': None, 'last_ts': '', 'days': {}}
        llm_dir = root / 'llm_calls'
        try:
            names = sorted(p.name for p in llm_dir.iterdir() if p.name.endswith('.jsonl'))
        except OSError:
            return None
        found = False
        for name in names[-7:]:
            day = name[:-6]
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
                if not isinstance(row, dict) or row.get('component') != 'proposer':
                    continue
                found = True
                out['calls'] += 1
                tok = row.get('total_tokens')
                if isinstance(tok, (int, float)) and not isinstance(tok, bool):
                    out['total_tokens'] += tok
                dur = row.get('duration_ms')
                if isinstance(dur, (int, float)) and not isinstance(dur, bool):
                    out['duration_ms'] += dur
                model = row.get('model')
                ts = str(row.get('ts') or '')
                if ts >= str(out.get('last_ts') or ''):
                    out['last_ts'] = ts
                    if model:
                        out['last_model'] = str(model)
                d = out['days'].setdefault(day, {'calls': 0, 'total_tokens': 0, 'duration_ms': 0.0})
                d['calls'] += 1
                if isinstance(tok, (int, float)) and not isinstance(tok, bool):
                    d['total_tokens'] += tok
                if isinstance(dur, (int, float)) and not isinstance(dur, bool):
                    d['duration_ms'] += dur
        return out if found else None

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

    tree_for_titles = read_json('evolution/tree.json')
    node_shas = list(tree_for_titles.get('nodes', {}).keys()) if isinstance(tree_for_titles, dict) else []
    titles, cycle_files, titles_error = extract_git_titles_local(repo_path, node_shas=node_shas)

    def read_lessons_local() -> list[dict[str, Any]]:
        """Issue #73: lessons from the instance repo (lessons.yaml + daily
        .yaml.gz archives). Local mirror of the REMOTE_READER_SCRIPT
        read_lessons() -- keep in sync. PyYAML when available, minimal
        flat-shape parser otherwise; fail-soft everywhere (#29)."""
        lessons_dir = repo_path / 'lessons'
        texts: list[tuple[str, str]] = []
        try:
            live = lessons_dir / 'lessons.yaml'
            texts.append(('live', live.read_text(encoding='utf-8', errors='replace')))
            mtimes.append(live.stat().st_mtime)
        except Exception:  # noqa: BLE001
            pass
        try:
            archives = sorted(p.name for p in (lessons_dir / 'archive').iterdir() if p.name.endswith('.yaml.gz'))
        except OSError:
            archives = []
        cutoff = time.mktime(time.strptime(
            (datetime.now(timezone.utc) - timedelta(days=LEDGER_HISTORY_DAYS)).strftime('%Y-%m-%d'),
            '%Y-%m-%d'))
        for name in archives:
            day = name[len('lessons-'):-len('.yaml.gz')]
            try:
                if time.mktime(time.strptime(day, '%Y-%m-%d')) < cutoff:
                    continue
            except Exception:  # noqa: BLE001
                pass
            try:
                with gzip.open(lessons_dir / 'archive' / name, 'rt', encoding='utf-8', errors='replace') as fh:
                    texts.append((name, fh.read()))
                mtimes.append((lessons_dir / 'archive' / name).stat().st_mtime)
            except Exception:  # noqa: BLE001
                continue
        entries: dict[str, dict[str, Any]] = {}

        def _absorb(parsed: Any) -> None:
            # Issue #73 fix: the flat fallback returns a LIST; rotation
            # archives are truncated mid-entry so PyYAML raises and the
            # fallback path is the one that actually runs for them.
            if isinstance(parsed, dict) and isinstance(parsed.get('lessons'), list):
                items = parsed['lessons']
            elif isinstance(parsed, list):
                items = parsed
            else:
                items = []
            for item in items:
                if isinstance(item, dict) and item.get('id'):
                    entries[str(item['id'])] = {
                        'id': str(item.get('id') or ''),
                        'date': str(item.get('date') or ''),
                        'cycle_id': str(item.get('cycle_id') or ''),
                        'task_id': str(item.get('task_id') or ''),
                        'hypothesis': str(item.get('hypothesis') or ''),
                        'result': str(item.get('result') or ''),
                        'insight': str(item.get('generalized_insight') or ''),
                        'problem': str(item.get('problem') or ''),
                        'solution': str(item.get('solution') or ''),
                    }

        try:
            import yaml  # type: ignore
            for _src, text in texts:
                try:
                    parsed = yaml.safe_load(text)
                except Exception:  # noqa: BLE001
                    parsed = _parse_lessons_flat(text)
                _absorb(parsed)
        except Exception:  # noqa: BLE001
            for _src, text in texts:
                _absorb(_parse_lessons_flat(text))
        return sorted(entries.values(), key=lambda e: (e.get('date') or '', e.get('id') or ''), reverse=True)

    data: dict[str, Any] = {
        'portfolio': read_json('tech_tree/portfolio.json'),
        'scorecard': read_json('scorecard/latest.json'),
        'evolution_tree': tree_for_titles,
        'hypotheses': read_json('hypotheses/lifecycle.json'),
        'hypotheses_durable': read_json('hypotheses/durable.json'),
        'ledger_tail': read_ledger_tail('ledger/cycles.jsonl'),
        'ledger_history': read_ledger_history_local(),
        'demand_rotation': read_json('demand/rotation.json'),
        'demand_completed': read_json('demand/completed.json'),
        'skill_reads': read_json('skill_fitness/reads.json'),
        'skill_evals': read_jsonl('skill_fitness/evals.jsonl'),
        'llm_stats': read_llm_stats_local(),
        'proposer_stats': read_proposer_stats_local(),
        'lessons': read_lessons_local(),
        'reflections': read_jsonl('reflector/reflections.jsonl'),
        'goal_text': read_json('goals/goal_text.json'),
        'agents_md': agents_text,
        'cycle_titles': titles,
        'cycle_files': cycle_files,
        'cycle_titles_error': titles_error,
        'generator_sha': '',
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
LINEAGE_DAYS = 14
LINEAGE_DAY_CAP = 120
EVO_MAX_DISPLAY = 30  # legacy canvas path; lineage.html uses day buckets

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
        f'<div class="evo-date">{esc(fmt_ts_short(node.get("ts")))}</div>'
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


_ARCHIVE_RING = {
    'integrated': '#2fd3c4',
    'failed': '#b23a3a',
    'partial': '#d19a66',
    'skipped': '#7d9c8a',
    'running': '#61afef',
}


def _ledger_outcome_kind(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Issue #71: outcome class + reason for a ledger-only cycle (no tree
    node). Scans the cycle's ledger rows chronologically; most decisive
    phase wins."""
    kind, reason = 'running', ''
    for row in rows:
        phase = row.get('phase')
        if phase == 'dedup':
            decision = str(row.get('decision') or '')
            if row.get('duplicate') or 'skip' in decision.lower() or 'duplicate' in decision.lower():
                return 'skipped', decision or 'duplicate'
        elif phase == 'proposer_reject':
            kind, reason = 'skipped', str(row.get('reason') or 'rejected')
        elif phase == 'gate' and row.get('status') == 'fail':
            kind, reason = 'failed', str(row.get('reason') or 'gate failed')
        elif phase == 'outcome':
            # Issue #77: the live ledger carries the decisive value in the
            # `outcome` field ('success'/'failed'/'partial'/
            # 'skipped-duplicate'); `status` is None there. Keep the legacy
            # `status` vocabulary as fallback for older shapes.
            status = str(row.get('outcome') or row.get('status') or '')
            if status in ('fail', 'failed'):
                return 'failed', str(row.get('reason') or 'failed')
            if status == 'partial':
                return 'partial', str(row.get('reason') or 'partial')
            if status in ('skipped', 'skipped-duplicate'):
                return 'skipped', str(row.get('reason') or status)
            if status == 'success':
                return 'integrated', ''
    return kind, reason


def _lineage_day(ts: Any) -> str:
    value = str(ts or '')
    return value[:10] if len(value) >= 10 else ''


def _build_vertical_day_lineage(
    ledger_rows: list[Any],
    fallback_tree: dict[str, Any] | None,
    task_titles: dict[str, str] | None,
    now: str | None,
    cycle_details: dict[str, dict[str, Any]] | None = None,
) -> str:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    leaves: dict[str, list[dict[str, Any]]] = {}
    for row in ledger_rows:
        if not isinstance(row, dict):
            continue
        day = _lineage_day(row.get('ts'))
        if not day:
            continue
        if row.get('phase') == 'evolution_tree' and row.get('sha'):
            sha = str(row['sha'])
            grouped.setdefault(day, {})[sha] = {
                'sha': sha, 'cycle_id': str(row.get('cycle_id') or ''),
                'parent_sha': str(row.get('parent_sha') or ''), 'ts': str(row.get('ts') or ''),
            }
        elif row.get('cycle_id') and row.get('phase') in {'outcome', 'gate', 'proposer_reject', 'dedup'}:
            leaves.setdefault(day, []).append(row)
    if not grouped and isinstance(fallback_tree, dict) and isinstance(fallback_tree.get('nodes'), dict):
        for sha, node in fallback_tree['nodes'].items():
            if isinstance(node, dict) and _lineage_day(node.get('ts')):
                day = _lineage_day(node.get('ts'))
                grouped.setdefault(day, {})[str(sha)] = {
                    'sha': str(sha), 'cycle_id': str(node.get('cycle_id') or ''),
                    'parent_sha': str(node.get('parent_sha') or ''), 'ts': str(node.get('ts') or ''),
                }
    days = sorted(grouped)[-LINEAGE_DAYS:]
    all_nodes = {sha: node for day in days for sha, node in grouped[day].items()}
    max_day = max(days, default='')
    current_day = min(_lineage_day(now) or max_day, max_day) if max_day else ''
    prior = [day for day in days if day < current_day]
    default_days = {day for day in (prior[-1:] + ([current_day] if current_day else []))}
    parts = ['<div class="lineage-day-filter" data-default-filter="yesterday-today"><div class="lineage-day-controls">',
             '<button type="button" data-lineage-filter="today">Today</button>',
             '<button type="button" data-lineage-filter="24h">24h</button>',
             '<button type="button" data-lineage-filter="yesterday-today" class="active">Yesterday+Today</button>',
             '<label>from <input type="date" data-lineage-from></label><label>to <input type="date" data-lineage-to></label>',
             '<button type="button" data-lineage-filter="range">Apply</button></div>',
             '<div class="lineage-day-groups" data-lineage-default="' + ','.join(sorted(default_days)) + '">']
    all_ordered = sorted(all_nodes.values(), key=lambda node: node['ts'])
    current_sha = str((fallback_tree or {}).get('current_sha') or '')
    for day in days:
        trunk = sorted(grouped[day].values(), key=lambda node: node['ts'])
        by_sha = {node['sha']: node for node in trunk}
        depth: dict[str, int] = {}

        def node_depth(sha: str, guard: set[str] | None = None) -> int:
            if sha in depth:
                return depth[sha]
            guard = guard or set()
            if sha in guard:
                return 0
            parent = by_sha[sha]['parent_sha']
            if parent in by_sha:
                depth[sha] = node_depth(parent, guard | {sha}) + 1
            else:
                previous = next((item for item in reversed(trunk) if item['ts'] < by_sha[sha]['ts']), None)
                depth[sha] = node_depth(previous['sha'], guard | {sha}) + 1 if previous else 0
            return depth[sha]

        for node in trunk:
            node_depth(node['sha'])
        children: dict[str, int] = {}
        for node in trunk:
            parent = node['parent_sha']
            if parent in by_sha:
                children[parent] = children.get(parent, 0) + 1
        seen = {node['cycle_id'] for node in trunk}
        side = []
        for row in sorted(leaves.get(day, []), key=lambda item: str(item.get('ts') or '')):
            cid = str(row['cycle_id'])
            if cid not in seen:
                side.append({'sha': 'leaf:' + cid, 'cycle_id': cid, 'ts': str(row.get('ts') or ''),
                             'outcome': 'failed' if row.get('outcome') in {'failed', 'fail'} or row.get('status') in {'failed', 'fail'} else 'partial' if row.get('outcome') == 'partial' else 'skipped'})
                seen.add(cid)
        truncated = len(trunk) > LINEAGE_DAY_CAP
        trunk = trunk[-LINEAGE_DAY_CAP:]
        parts.append(f'<section class="lineage-day-group" data-day="{esc(day)}"><h3>{esc(day)}</h3>')
        parts.append('<p class="lineage-js-note">Enable JavaScript for the enhanced client-side lineage layout.</p>')
        if truncated:
            parts.append(f'<p class="lineage-day-truncated">truncated at {LINEAGE_DAY_CAP} nodes</p>')
        count = max(len(trunk), len(side), 1)
        height = 40 + (max(len(trunk), len(side), 1) * 32)
        positions: dict[str, tuple[int, int]] = {}
        slots: dict[int, int] = {}
        for node in trunk:
            d = node_depth(node['sha'])
            slot = slots.get(d, 0)
            slots[d] = slot + 1
            positions[node['sha']] = (60 + slot * 70, 24 + d * 32)
        lane_last_y: list[int] = []
        for node in side[:LINEAGE_DAY_CAP]:
            base = max((j for j, trunk_node in enumerate(trunk) if trunk_node['ts'] <= node['ts']), default=0)
            base_y = 24 + base * 32
            y = base_y
            lane = next((i for i, last_y in enumerate(lane_last_y) if last_y + 32 <= y), len(lane_last_y))
            if lane == len(lane_last_y):
                lane_last_y.append(y)
            else:
                lane_last_y[lane] = y
            positions[node['sha']] = (160 + lane * 52, y)
        # Width follows the lanes actually used, not the worst-case
        # one-lane-per-leaf count: a fixed width/height pair that exceeds the
        # drawn content letterboxes under max-width scaling and renders as
        # large empty bands around the tree.
        max_x = max((x for x, _ in positions.values()), default=60)
        width = max(180, max_x + 40)
        parts.append(f'<svg class="lineage-day-svg arch-tree" width="{width}" height="{height}" viewBox="0 0 {width} {height}" data-lineage-renderer="d3-dag">')
        for i, node in enumerate(trunk):
            parent = node['parent_sha']
            if parent in positions:
                x1, y1 = positions[parent]; x2, y2 = positions[node['sha']]
                cls = 'lineage-edge arch-edge'
                if node['sha'] == current_sha or node['sha'] in by_sha:
                    cls = 'lineage-edge arch-edge' + (' evo-elbow-best' if current_sha and node['sha'] == current_sha else '')
                parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="{cls}"/>')
            elif parent and parent in all_nodes:
                label = datetime.strptime(_lineage_day(all_nodes[parent]['ts']), '%Y-%m-%d').strftime('%b %d')
                parts.append(f'<text class="lineage-hidden-parent" x="{positions[node["sha"]][0]}" y="16">&#8617; from {esc(label)}</text>')
            elif i:
                x1, y1 = positions[trunk[i - 1]['sha']]; x2, y2 = positions[node['sha']]
                parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="lineage-edge lineage-edge-chronological" stroke-dasharray="6 5"/>')
        for node in side[:LINEAGE_DAY_CAP]:
            base = max((trunk_node for trunk_node in trunk if trunk_node['ts'] <= node['ts']), key=lambda item: item['ts'], default=None)
            if base is not None:
                x1, y1 = positions[base['sha']]
                x2, y2 = positions[node['sha']]
                parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="lineage-edge arch-edge"/>')
        graph_nodes = trunk + side[:LINEAGE_DAY_CAP]
        day_payload = {
            'day': day,
            'current_sha': current_sha,
            'nodes': [
                {
                    'sha': node['sha'],
                    'cycle_id': node['cycle_id'],
                    'parent': node.get('parent_sha') or next((base['sha'] for base in trunk if base['ts'] <= node['ts']), None),
                    'ts': node['ts'],
                    'outcome': node.get('outcome', 'integrated'),
                    'kind': 'trunk' if node in trunk else 'leaf',
                }
                for node in graph_nodes
            ],
            'edges': [
                {'source': node['parent_sha'], 'target': node['sha']}
                for node in trunk if node['parent_sha'] in {item['sha'] for item in graph_nodes}
            ] + [
                {'source': base['sha'], 'target': node['sha']}
                for node in side[:LINEAGE_DAY_CAP]
                for base in [max((item for item in trunk if item['ts'] <= node['ts']), key=lambda item: item['ts'], default=None)]
                if base is not None
            ],
        }
        day_json = json.dumps(day_payload, ensure_ascii=True, separators=(',', ':')).replace('<', '\\u003c')
        parts.append(f'<script type="application/json" class="lineage-day-data" data-day="{esc(day)}">{day_json}</script>')
        if current_sha in positions:
            x, y = positions[current_sha]
            parts.append(f'<text x="{x}" y="{y - 14}" text-anchor="middle" class="arch-star">&#9733;</text>')
        for node in trunk + side[:LINEAGE_DAY_CAP]:
            x, y = positions[node['sha']]
            cid = node['cycle_id'] or node['sha']
            kind = node.get('outcome', 'integrated')
            title = (task_titles or {}).get(cid) or '(untitled cycle)'
            parts.append(f'<circle class="arch-node arch-{esc(kind)} lineage-node" data-cycle-id="{esc(cid)}" cx="{x}" cy="{y}" r="9"><title>{esc(title)}</title></circle>')
        parts.append('</svg></section>')
    parts.append('</div></div>')
    details_json = json.dumps(cycle_details or {}, ensure_ascii=True).replace('<', '\\u003c')
    card_template = """<template id="cycle-detail-card-template"><div><p><b>Cycle</b></p><p><b>Outcome</b></p><p><b>Reason</b></p><p><b>Timestamp</b></p><p><b>SHA</b></p><p><b>Parent SHA</b></p><h3>Files changed</h3><ul></ul><h3>Lesson insight</h3><a>open in Cycle Feed</a> · <a>related lessons</a></div></template>"""
    parts.append(f'''<section class="cycle-details-panel" id="cycle-details-panel" hidden><h2>Cycle details</h2><div class="cycle-details-body"></div></section>{card_template}<script type="application/json" id="cycle-details-data">{details_json}</script><script src="assets/vendor/d3.min.js"></script><script src="assets/vendor/d3-dag.iife.min.js"></script><script src="assets/vendor/lineage-renderer.js"></script><script>
(function () {{
  var data = JSON.parse(document.getElementById('cycle-details-data').textContent), panel = document.getElementById('cycle-details-panel');
  function line(label, value) {{ return value ? '<p><b>' + label + ':</b> ' + String(value) + '</p>' : ''; }}
  function open(node) {{ var item = data[node.getAttribute('data-cycle-id')] || {{}}; var html = line('Cycle', item.cycle_id) + line('Outcome', item.outcome) + line('Reason', item.reason) + line('Timestamp', item.ts) + line('SHA', item.sha) + line('Parent SHA', item.parent_sha); if (item.files_changed && item.files_changed.length) html += '<h3>Files changed</h3><ul>' + item.files_changed.map(function (f) {{ return '<li>' + f + '</li>'; }}).join('') + '</ul>'; if (item.lesson_insight) html += '<h3>Lesson insight</h3><p>' + item.lesson_insight + '</p>'; html += '<p><a href="cycles.html#cycle-' + encodeURIComponent(item.cycle_id || '') + '">open in Cycle Feed</a> · <a href="lessons.html#q-' + encodeURIComponent(item.cycle_id || '') + '">related lessons</a></p>'; panel.hidden = false; panel.querySelector('.cycle-details-body').innerHTML = html; }}
  document.querySelectorAll('.lineage-node').forEach(function (node) {{ node.addEventListener('click', function (event) {{ event.preventDefault(); open(node); }}); }});
  var root = document.querySelector('.lineage-day-groups'); if (!root) return;
  var groups = Array.prototype.slice.call(root.querySelectorAll('.lineage-day-group'));
  function apply(mode) {{ var days = groups.map(function (g) {{ return g.getAttribute('data-day'); }}), keep = days.slice(-2); if (mode === 'today') keep = days.slice(-1); if (mode === 'range') {{ var a = document.querySelector('[data-lineage-from]').value, b = document.querySelector('[data-lineage-to]').value; keep = days.filter(function (d) {{ return (!a || d >= a) && (!b || d <= b); }}); }} groups.forEach(function (g) {{ g.hidden = keep.indexOf(g.getAttribute('data-day')) === -1; }}); }}
   document.querySelectorAll('[data-lineage-filter]').forEach(function (button) {{ button.addEventListener('click', function () {{ apply(button.getAttribute('data-lineage-filter')); }}); }}); apply('yesterday-today');
}})();
</script>''')
    return '<div class="canvas-outer" id="panel-lineage">' + ''.join(parts) + '</div>'


def _build_day_bucketed_lineage(
    ledger_rows: list[Any],
    fallback_tree: dict[str, Any] | None,
    task_titles: dict[str, str] | None,
    now: str | None,
    cycle_details: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Issue #107: render recent ledger evolution rows grouped by UTC day."""
    return _build_vertical_day_lineage(ledger_rows, fallback_tree, task_titles, now, cycle_details)
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    leaves: dict[str, list[dict[str, Any]]] = {}
    for row in ledger_rows:
        if not isinstance(row, dict) or row.get('phase') != 'evolution_tree' or not row.get('sha'):
            if isinstance(row, dict) and row.get('cycle_id') and row.get('phase') in {'outcome', 'gate', 'proposer_reject', 'dedup'}:
                day = _lineage_day(row.get('ts'))
                if day:
                    leaves.setdefault(day, []).append(row)
            continue
        day = _lineage_day(row.get('ts'))
        if not day:
            continue
        sha = str(row['sha'])
        grouped.setdefault(day, {})[sha] = {
            'sha': sha, 'cycle_id': str(row.get('cycle_id') or ''),
            'parent_sha': str(row.get('parent_sha') or ''), 'ts': str(row.get('ts') or ''),
        }
    if not grouped and isinstance(fallback_tree, dict) and isinstance(fallback_tree.get('nodes'), dict):
        for sha, node in fallback_tree['nodes'].items():
            if isinstance(node, dict):
                day = _lineage_day(node.get('ts'))
                if day:
                    grouped.setdefault(day, {})[str(sha)] = {
                        'sha': str(sha), 'cycle_id': str(node.get('cycle_id') or ''),
                        'parent_sha': str(node.get('parent_sha') or ''), 'ts': str(node.get('ts') or ''),
                    }
    days = sorted(grouped)[-LINEAGE_DAYS:]
    all_nodes = {sha: node for day in days for sha, node in grouped[day].items()}
    ordered_all = sorted(all_nodes.values(), key=lambda node: node['ts'])
    all_shas = set(all_nodes)
    nodes_by_sha = all_nodes
    max_day = max(days, default='')
    current_day = _lineage_day(now) or max_day
    if current_day > max_day:
        current_day = max_day
    prior_days = sorted(day for day in days if day < current_day)
    previous_day = prior_days[-1] if prior_days else ''
    default_days = {day for day in (previous_day, current_day) if day}
    parts: list[str] = ['<div class="lineage-day-filter" data-default-filter="yesterday-today"><div class="lineage-day-controls" data-default-filter="yesterday-today">',
                        '<button type="button" data-lineage-filter="today">Today</button>',
                        '<button type="button" data-lineage-filter="24h">24h</button>',
                        '<button type="button" data-lineage-filter="yesterday-today" class="active">Yesterday+Today</button>',
                        '<label>from <input type="date" data-lineage-from></label><label>to <input type="date" data-lineage-to></label>',
                        '<button type="button" data-lineage-filter="range">Apply</button></div>']
    parts.append('<div class="lineage-day-groups" data-lineage-default="' + ','.join(sorted(default_days)) + '">')
    for day in days:
        nodes = list(sorted(grouped[day].values(), key=lambda n: n['ts']))
        trunk_shas = {node['sha'] for node in nodes}
        leaf_records = []
        for row in leaves.get(day, []):
            cid = str(row['cycle_id'])
            if cid not in {node['cycle_id'] for node in nodes}:
                leaf_records.append({'sha': 'leaf:' + cid, 'cycle_id': cid, 'parent_sha': '', 'ts': str(row.get('ts') or ''), 'outcome': 'failed' if row.get('outcome') in {'failed', 'fail'} or row.get('status') in {'failed', 'fail'} else 'partial' if row.get('outcome') == 'partial' else 'skipped'})
        nodes.extend(leaf_records)
        truncated = len(nodes) > LINEAGE_DAY_CAP
        nodes = nodes[-LINEAGE_DAY_CAP:]
        parts.append(f'<section class="lineage-day-group" data-day="{esc(day)}" data-node-count="{len(nodes)}">')
        parts.append(f'<h3>{esc(day)}</h3>')
        if truncated:
            parts.append(f'<p class="lineage-day-truncated">truncated at {LINEAGE_DAY_CAP} nodes</p>')
        depth: dict[str, int] = {}
        def get_depth(sha: str, guard: set[str] | None = None) -> int:
            if sha in depth:
                return depth[sha]
            guard = guard or set()
            if sha in guard:
                return 0
            parent = nodes_by_sha[sha]['parent_sha']
            if parent in all_shas:
                depth[sha] = get_depth(parent, guard | {sha}) + 1
            else:
                prior = next((i - 1 for i, item in enumerate(ordered_all) if item['sha'] == sha), -1)
                depth[sha] = 0 if prior < 0 else depth[ordered_all[prior]['sha']] + 1
            return depth[sha]
        for node in all_nodes.values():
            get_depth(node['sha'])
        for node in leaf_records:
            depth[node['sha']] = depth.get(node['sha'], max(depth.values(), default=0) + 1)
        slots: dict[int, int] = {}
        for node in sorted(nodes, key=lambda item: (depth[item['sha']], item['ts'])):
            slots[depth[node['sha']]] = slots.get(depth[node['sha']], 0) + 1
        max_slot = max(slots.values(), default=1)
        svg_height = 40 + max_slot * 32
        min_depth = min((depth[node['sha']] for node in nodes), default=0)
        pitch = 40
        max_x = max((30 + (depth[node['sha']] - min_depth) * pitch for node in nodes), default=30)
        svg_width = max(60, max_x + 30)
        parts.append(f'<svg class="lineage-day-svg" width="{svg_width}" height="{svg_height}" viewBox="0 0 {svg_width} {svg_height}">')
        positions: dict[str, tuple[int, int]] = {}
        depth_slots: dict[int, int] = {}
        for node in sorted(nodes, key=lambda item: (depth[item['sha']], item['ts'])):
            d = depth[node['sha']]
            slot = depth_slots.get(d, 0)
            depth_slots[d] = slot + 1
            positions[node['sha']] = (30 + (d - min_depth) * pitch, 24 + slot * 32)
        for node in nodes:
            parent = node['parent_sha']
            if parent in positions:
                x1, y1 = positions[parent]; x2, y2 = positions[node['sha']]
                parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="lineage-edge"/>')
            elif parent:
                if parent in all_shas:
                    parent_node = all_nodes[parent]
                    label = datetime.strptime(_lineage_day(parent_node['ts']), '%Y-%m-%d').strftime('%b %d')
                    parts.append(f'<text class="lineage-hidden-parent" x="{positions[node["sha"]][0]}" y="20">&#8617; from {esc(label)}</text>')
                else:
                    previous = next(
                        (item for item in reversed(ordered_all)
                         if item['ts'] < node['ts'] and _lineage_day(item['ts']) == day),
                        None,
                    )
                    if previous:
                        x1, y1 = positions.get(previous['sha'], (positions[node['sha']][0] - 80, positions[node['sha']][1]))
                        x2, y2 = positions[node['sha']]
                        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="lineage-edge lineage-edge-chronological" stroke-dasharray="6 5"/>')
        for node in nodes:
            x, y = positions[node['sha']]
            cid = node['cycle_id'] or node['sha']
            title = (task_titles or {}).get(cid) or (task_titles or {}).get(node['sha']) or '(untitled cycle)'
            kind = node.get('outcome', 'integrated')
            parts.append(f'<circle class="arch-node arch-{esc(kind)} lineage-node" data-cycle-id="{esc(cid)}" cx="{x}" cy="{y}" r="9"><title>{esc(title)}</title></circle>')
        parts.append('</svg></section>')
    parts.append('</div></div>')
    parts.append('''<script>
(function () {
  var root = document.querySelector('.lineage-day-groups'); if (!root) return;
  var groups = Array.prototype.slice.call(root.querySelectorAll('.lineage-day-group'));
  function apply(mode) {
    var days = groups.map(function (g) { return g.getAttribute('data-day'); });
    var keep = days.slice(-2);
    if (mode === 'today') keep = days.slice(-1);
    if (mode === '24h') keep = days.slice(-2);
    if (mode === 'range') { var a = document.querySelector('[data-lineage-from]').value, b = document.querySelector('[data-lineage-to]').value; keep = days.filter(function (d) { return (!a || d >= a) && (!b || d <= b); }); }
    groups.forEach(function (g) { g.hidden = keep.indexOf(g.getAttribute('data-day')) === -1; });
    document.querySelectorAll('[data-lineage-filter]').forEach(function (b) { b.classList.toggle('active', b.getAttribute('data-lineage-filter') === mode); });
  }
  document.querySelectorAll('[data-lineage-filter]').forEach(function (b) { b.addEventListener('click', function () { apply(b.getAttribute('data-lineage-filter')); }); });
  apply('yesterday-today');
})();
</script>''')
    return '<div class="canvas-outer" id="panel-lineage">' + ''.join(parts) + '</div>'


def build_cycle_details(
    ledger_rows: list[Any] | None,
    evolution_tree: dict[str, Any] | None,
    lessons: list[Any] | None,
    reflections: list[Any] | None,
    cycle_titles: dict[str, str] | None = None,
    cycle_files: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build bounded, JSON-safe records for the lineage details panel."""
    records: dict[str, dict[str, Any]] = {}

    def record(cid: str) -> dict[str, Any]:
        return records.setdefault(cid, {'cycle_id': cid, 'files_changed': [], 'gate_violations': []})

    def text(value: Any, limit: int = 500) -> str:
        return str(value or '')[:limit]

    rows_by_cycle: dict[str, list[dict[str, Any]]] = {}
    for row in ledger_rows or []:
        if isinstance(row, dict) and row.get('cycle_id'):
            rows_by_cycle.setdefault(str(row['cycle_id']), []).append(row)
    for cid, rows in rows_by_cycle.items():
        out = record(cid)
        for row in rows:
            for key in ('task_title', 'target_path', 'serves', 'demand_id', 'outcome', 'reason', 'ts', 'sha', 'parent_sha', 'branch'):
                if row.get(key) not in (None, ''):
                    out[key] = text(row[key])
            files = row.get('files_changed')
            if isinstance(files, list):
                out['files_changed'] = [text(item, 300) for item in files[:20]]
            violations = row.get('violations')
            if isinstance(violations, list):
                out['gate_violations'] = [text(item, 500) for item in violations[:20]]

    if isinstance(evolution_tree, dict) and isinstance(evolution_tree.get('nodes'), dict):
        for sha, node in evolution_tree['nodes'].items():
            if not isinstance(node, dict) or not node.get('cycle_id'):
                continue
            cid = str(node['cycle_id'])
            out = record(cid)
            out.setdefault('sha', text(sha, 80))
            for key in ('parent_sha', 'branch', 'ts'):
                if node.get(key) not in (None, ''):
                    out.setdefault(key, text(node[key], 120))

    for lesson in lessons or []:
        if not isinstance(lesson, dict) or not lesson.get('cycle_id'):
            continue
        out = record(str(lesson['cycle_id']))
        insight = lesson.get('insight') or lesson.get('generalized_insight') or lesson.get('reusable_insight') or lesson.get('result')
        if insight:
            out['lesson_insight'] = text(insight)
        # Issue #92: v2 schema fields supersede legacy insight when present.
        if lesson.get('problem'):
            out['lesson_problem'] = text(lesson['problem'])
        if lesson.get('solution'):
            out['lesson_solution'] = text(lesson['solution'])

    for reflection in reflections or []:
        if not isinstance(reflection, dict) or not reflection.get('cycle_id'):
            continue
        out = record(str(reflection['cycle_id']))
        payload = {'summary': text(reflection.get('summary'))}
        for key in ('findings', 'recommendations'):
            value = reflection.get(key)
            if isinstance(value, list):
                payload[key] = [text(item) for item in value[:20]]
            elif value:
                payload[key] = [text(value)]
        if any(payload.values()):
            out['reflection'] = payload

    for cid, files in (cycle_files or {}).items():
        if cid in records and not records[cid].get('files_changed'):
            records[cid]['files_changed'] = [text(item, 300) for item in files[:20]]

    for cid, out in records.items():
        title = out.get('task_title') or (cycle_titles or {}).get(cid) or (cycle_titles or {}).get(cid.replace('cycle-', '', 1))
        if not title and out.get('files_changed'):
            title = out['files_changed'][0]
        out['title'] = text(title or '(untitled cycle)')
        if out.get('outcome') in ('failed', 'partial') and not out.get('gate_violations'):
            out.pop('gate_violations', None)
        else:
            out.pop('gate_violations', None)
    return records


def _cycle_details_panel(details: dict[str, dict[str, Any]]) -> str:
    payload = json.dumps(details, ensure_ascii=True, separators=(',', ':')).replace('<', '\\u003c')
    lesson_href = 'lessons.html#q-' + next(iter(details), 'cycle')
    return f'''<section class="cycle-details-panel" id="cycle-details-panel" aria-live="polite" hidden>
  <button type="button" class="cycle-details-close" aria-label="Close cycle details">close</button>
  <h2 class="cycle-details-title">Cycle details</h2>
  <div class="cycle-details-body"></div>
</section>
<script type="application/json" id="cycle-details-data">{payload}</script>
<script>
(function () {{
  var data = JSON.parse(document.getElementById('cycle-details-data').textContent);
  var panel = document.getElementById('cycle-details-panel');
  var body = panel.querySelector('.cycle-details-body');
  var selected = null;
  function esc(v) {{ var d = document.createElement('div'); d.textContent = v == null ? '' : String(v); return d.innerHTML; }}
  function line(label, value) {{ return value ? '<p><b>' + esc(label) + ':</b> ' + esc(value) + '</p>' : ''; }}
  function list(label, values) {{ return values && values.length ? '<h3>' + esc(label) + '</h3><ul>' + values.map(function (v) {{ return '<li>' + esc(v) + '</li>'; }}).join('') + '</ul>' : ''; }}
  function close() {{ panel.hidden = true; if (selected) selected.classList.remove('cycle-node-selected'); selected = null; }}
  function open(node) {{
    var cid = node.getAttribute('data-cycle-id'), item = data[cid] || {{cycle_id: cid, title: '(untitled cycle)'}};
    var nodeId = node.getAttribute('data-node-id') || cid;
    if (selected) selected.classList.remove('cycle-node-selected'); selected = node; node.classList.add('cycle-node-selected');
    panel.querySelector('.cycle-details-title').textContent = item.title || '(untitled cycle)';
    var html = line('Cycle', item.cycle_id) + line('Outcome', item.outcome) + line('Reason', item.reason) + line('Timestamp', item.ts) + line('SHA', item.sha) + line('Parent SHA', item.parent_sha) + line('Target path', item.target_path) + line('Serves / demand', item.serves || item.demand_id) + list('Files changed', item.files_changed) + list('Gate violations', item.gate_violations);
    if (item.lesson_problem || item.lesson_solution) {{ html += '<h3>Lesson</h3>' + (item.lesson_problem ? '<p><b>Problem:</b> ' + esc(item.lesson_problem) + '</p>' : '') + (item.lesson_solution ? '<p><b>Solution:</b> ' + esc(item.lesson_solution) + '</p>' : ''); }}
    else if (item.lesson_insight) {{ var _li = item.lesson_insight, _ll = _li.toLowerCase(); var _skip = _ll.indexOf('task completed with') !== -1 || _ll.indexOf('short utility scripts implementable') !== -1 || _ll.indexOf('improves operator value') !== -1; if (!_skip) html += '<h3>Lesson insight</h3><p>' + esc(_li) + '</p>'; }}
    if (item.reflection) html += '<h3>Reflector</h3>' + line('Summary', item.reflection.summary) + list('Findings', item.reflection.findings) + list('Recommendations', item.reflection.recommendations);
    html += '<p class="cycle-details-links"><a class="cycle-feed-link" href="cycles.html#cycle-' + encodeURIComponent(cid) + '">open in Cycle Feed</a> · <a href="lessons.html#q-' + encodeURIComponent(cid) + '">related lessons</a></p>';
    body.innerHTML = html; panel.hidden = false; history.replaceState(null, '', '#node-' + encodeURIComponent(nodeId)); panel.scrollIntoView({{block: 'nearest'}});
  }}
  document.querySelectorAll('.arch-node[data-cycle-id]').forEach(function (node) {{ node.addEventListener('click', function (event) {{ event.preventDefault(); open(node); }}); }});
  panel.querySelector('.cycle-details-close').addEventListener('click', close);
  document.addEventListener('keydown', function (event) {{ if (event.key === 'Escape') close(); }});
  function selectHash() {{ var match = decodeURIComponent(location.hash).match(/^#node-(.+)$/); if (!match) return; var val = match[1]; var node = document.querySelector('[data-node-id="' + CSS.escape(val) + '"]') || document.querySelector('[data-cycle-id="' + CSS.escape(val) + '"]'); if (node) {{ open(node); node.scrollIntoView({{block: 'center'}}); }} }}
  window.addEventListener('hashchange', selectHash); selectHash();
}})();
</script><template id="cycle-details-link-template" data-lesson-href="{lesson_href}"></template>'''


def build_archive_tree(
    evolution_tree: dict[str, Any] | None,
    ledger_tail: list[Any] | None,
    task_titles: dict[str, str] | None = None,
    cycle_details: dict[str, dict[str, Any]] | None = None,
    ledger_history: list[Any] | None = None,
    now: str | None = None,
) -> str:
    """Issue #71: DGM archive tree -- FULL history, layered top-down.
    Trunk = evolution/tree.json nodes (integrated chain); every ledger-only
    cycle (failed/partial/skipped) attaches as a dead-end leaf under its
    chronological base trunk node. No last-N cap."""
    if isinstance(ledger_history, list) and any(
        isinstance(row, dict) and row.get('phase') == 'evolution_tree' for row in ledger_history
    ):
        return _build_day_bucketed_lineage(ledger_history, evolution_tree, task_titles, now or datetime.now(timezone.utc).isoformat(), cycle_details)
    if not isinstance(evolution_tree, dict) or not isinstance(evolution_tree.get('nodes'), dict):
        return unavailable_panel('Evolution Lineage', 'evolution tree unavailable')
    nodes: dict[str, dict[str, Any]] = {
        sha: n for sha, n in evolution_tree['nodes'].items() if isinstance(n, dict)
    }
    if not nodes:
        return unavailable_panel('Evolution Lineage', 'no evolution nodes recorded yet')
    current_sha = evolution_tree.get('current_sha')

    # Chronological order + effective parents (recorded parent when it maps
    # to a node, else previous-by-time -- same fallback as issue #53).
    order = sorted(nodes.items(), key=lambda kv: str(kv[1].get('ts') or ''))
    chrono_prev: dict[str, str] = {}
    _last: str | None = None
    for sha, _n in order:
        if _last is not None:
            chrono_prev[sha] = _last
        _last = sha

    def _eff_parent(sha: str) -> str | None:
        parent = (nodes[sha].get('parent_sha') or '')
        return str(parent) if parent in nodes else chrono_prev.get(sha)

    # Unified node records: trunk + ledger-only leaves.
    recs: dict[str, dict[str, Any]] = {}
    for sha, n in order:
        cid = str(n.get('cycle_id') or '')
        recs[sha] = {
            'kind': 'node', 'cid': cid, 'ts': str(n.get('ts') or ''),
            'parent': _eff_parent(sha), 'sha': sha,
        }
    trunk_cids = {r['cid'] for r in recs.values() if r['cid']}
    trunk_ts = [(r['ts'], key) for key, r in recs.items()]
    if isinstance(ledger_tail, list):
        by_cycle: dict[str, list[dict[str, Any]]] = {}
        for row in ledger_tail:
            if isinstance(row, dict) and row.get('cycle_id'):
                by_cycle.setdefault(str(row['cycle_id']), []).append(row)
        for cid, rows in by_cycle.items():
            if cid in trunk_cids:
                continue
            kind, reason = _ledger_outcome_kind(rows)
            ts = str(rows[-1].get('ts') or '')
            base = None
            for t, key in trunk_ts:
                if t <= ts:
                    base = key
            if base is None and trunk_ts:
                base = trunk_ts[0][1]
            title = ''
            if task_titles:
                title = task_titles.get(cid) or task_titles.get(cid.replace('cycle-', '', 1)) or ''
            recs['ledger:' + cid] = {
                'kind': 'leaf', 'cid': cid, 'ts': ts, 'parent': base,
                'outcome': kind, 'reason': reason, 'title': title,
            }

    # Layered top-down layout: depth = longest path from a root.
    depth: dict[str, int] = {}

    def _depth(key: str, guard: frozenset[str] = frozenset()) -> int:
        if key in depth:
            return depth[key]
        if key in guard:
            depth[key] = 0
            return 0
        parent = recs[key].get('parent')
        depth[key] = _depth(parent, guard | {key}) + 1 if parent in recs else 0
        return depth[key]

    for key in recs:
        _depth(key)
    layers: dict[int, list[str]] = {}
    for key in sorted(recs, key=lambda k: (str(recs[k].get('ts') or ''))):
        layers.setdefault(depth[key], []).append(key)
    max_d = max(layers, default=0)
    max_slot = max((len(v) for v in layers.values()), default=1)

    ROW_H, COL_W, R = 64, 44, 9
    pos: dict[str, tuple[float, float]] = {}
    for d, keys in layers.items():
        for i, key in enumerate(keys):
            pos[key] = (60 + i * COL_W, 40 + d * ROW_H)

    # Best path = ancestry of current_sha (trunk nodes only).
    best: set[str] = set()
    if current_sha in recs:
        cur: str | None = current_sha
        guard: set[str] = set()
        while cur and cur in recs and cur not in guard:
            guard.add(cur)
            best.add(cur)
            cur = recs[cur].get('parent')

    # Score fills: fitness.reward when recorded, neutral otherwise.
    rewards = [
        float(n['fitness']['reward']) for n in nodes.values()
        if isinstance(n.get('fitness'), dict)
        and isinstance(n['fitness'].get('reward'), (int, float))
        and not isinstance(n['fitness'].get('reward'), bool)
    ]
    rmin = min(rewards) if rewards else None
    rmax = max(rewards) if rewards else None

    def _fill(key: str) -> str:
        r = recs[key]
        if r['kind'] != 'node':
            return '#1f3a2d'
        fit = nodes[key].get('fitness')
        val = fit.get('reward') if isinstance(fit, dict) else None
        if isinstance(val, (int, float)) and not isinstance(val, bool) and rmin is not None and rmax and rmax > rmin:
            return _score_color((float(val) - rmin) / (rmax - rmin))
        return '#1f3a2d'

    # Issue #93: day separators -- horizontal dashed lines between depth layers
    # that cross a date boundary. Each depth layer is one time-step; when the
    # first node in layer d is on a different UTC date than the first node in
    # layer d-1, a separator line + date label go between the two rows.
    lx = max(120 + max_slot * COL_W, 240)
    day_separators: list[str] = []
    _layer_dates: dict[int, str] = {}
    for d, keys in layers.items():
        ts_list = [recs[k].get('ts') or '' for k in keys if recs[k].get('ts')]
        if ts_list:
            first_ts = sorted(ts_list)[0]
            _dt = _parse_iso_ts(first_ts)
            if _dt is not None:
                _layer_dates[d] = _dt.strftime('%Y-%m-%d')
    for d in sorted(layers):
        if d == 0:
            continue
        prev_date = _layer_dates.get(d - 1, '')
        cur_date = _layer_dates.get(d, '')
        if prev_date and cur_date and cur_date != prev_date:
            sep_y = 40 + d * ROW_H - ROW_H // 2
            sep_w = max(lx - 20, 100)
            day_separators.append(
                f'<line x1="0" y1="{sep_y}" x2="{sep_w}" y2="{sep_y}" '
                f'class="arch-day-sep"/>'
                f'<text x="4" y="{sep_y - 2}" class="arch-day-label">{esc(cur_date)}</text>'
            )

    edges = []
    for key, r in recs.items():
        parent = r.get('parent')
        if parent in pos:
            x1, y1 = pos[parent]
            x2, y2 = pos[key]
            cls = 'arch-edge'
            if key in best and parent in best:
                cls = 'arch-edge arch-edge-best'
            edges.append(
                f'<line x1="{x1:.0f}" y1="{y1 + R:.0f}" x2="{x2:.0f}" y2="{y2 - R:.0f}" class="{cls}"/>'
            )

    circles = []
    for key, r in recs.items():
        x, y = pos[key]
        if r['kind'] == 'node':
            kind = 'integrated'
            reason = ''
        else:
            kind = r.get('outcome') or 'running'
            reason = r.get('reason') or ''
        ring = _ARCHIVE_RING.get(kind, '#7d9c8a')
        cid = r['cid'] or key
        title_txt = ''
        if task_titles:
            title_txt = task_titles.get(cid) or task_titles.get(cid.replace('cycle-', '', 1)) or task_titles.get(key) or ''
        if not title_txt and cycle_details and isinstance(cycle_details.get(cid), dict):
            title_txt = str(cycle_details[cid].get('title') or '')
        tip = esc(f'{cid} | {title_txt or "no title"} | {kind}' + (f': {reason}' if reason else '') + f' | {r["ts"]}')
        star = f'<text x="{x:.0f}" y="{y - R - 5:.0f}" text-anchor="middle" class="arch-star">&#9733;</text>' if key == current_sha else ''
        detail_attr = f' data-cycle-id="{esc(cid)}"' if cycle_details and cid in cycle_details else ''
        node_sha_attr = f' data-node-id="{esc(short_sha(key))}"' if r['kind'] == 'node' and len(key) >= 7 else ''
        circles.append(
            f'<a href="cycles.html#cycle-{esc(cid)}">'
            f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{R}" fill="{_fill(key)}" '
            f'stroke="{ring}" stroke-width="3" class="arch-node arch-{kind}"{detail_attr}{node_sha_attr}>'
            f'<title>{tip}</title></circle>{star}</a>'
        )

    # Legends (right side): outcome rings. Reward is not recorded for archive
    # nodes, so do not render a permanently empty score gauge (issue #94).
    lx = max(120 + max_slot * COL_W, 240)
    if rmin is not None and rmax and rmax > rmin:
        metric_label = 'score: fitness.reward'
        legend = (
            f'<defs><linearGradient id="archgrad" x1="0" y1="1" x2="0" y2="0">'
            f'<stop offset="0" stop-color="{_score_color(0)}"/>'
            f'<stop offset="1" stop-color="{_score_color(1)}"/>'
            '</linearGradient></defs>'
            f'<rect x="{lx}" y="40" width="14" height="120" rx="3" fill="url(#archgrad)"/>'
            f'<text x="{lx + 20}" y="52" class="arch-legend-label">score {fmt_compact(rmax)}</text>'
            f'<text x="{lx + 20}" y="158" class="arch-legend-label">score {fmt_compact(rmin)}</text>'
            f'<text x="{lx}" y="176" class="arch-legend-label">{metric_label}</text>'
        )
    else:
        legend = (
            f'<text x="{lx}" y="176" class="arch-legend-label">score gauge hidden: reward data gap</text>'
        )
    ring_legend_y = 220
    # Issue #77: running can legitimately appear (in-flight cycle), so the
    # legend must cover every ring class that can render.
    ring_kinds = list(_ARCHIVE_RING.items())
    ring_items = ''.join(
        f'<circle cx="{lx + 6}" cy="{ring_legend_y + i * 20}" r="6" fill="none" stroke="{color}" stroke-width="3"/>'
        f'<text x="{lx + 20}" y="{ring_legend_y + i * 20 + 4}" class="arch-legend-label">{kind}</text>'
        for i, (kind, color) in enumerate(ring_kinds)
    )
    legend += f'<g>{ring_items}</g>'

    width = max(420, lx + 170)
    height = 60 + (max_d + 1) * ROW_H + 40
    svg = (
        f'<svg class="tech-canvas arch-tree" role="img" '
        f'aria-label="DGM archive tree: full evolution history, {len(recs)} nodes" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'{"".join(day_separators)}{"" .join(edges)}{"" .join(circles)}{legend}</svg>'
    )
    total = len(recs)
    panel = _cycle_details_panel(cycle_details or {}) if cycle_details else ''
    return (
        f'<div class="canvas-outer" id="panel-lineage">'
        f'<div class="arch-note">archive tree: {total} nodes (full history, no cap) '
        f'&#183; bold = best path &#183; click a node for its cycle</div>{svg}{panel}</div>'
    )


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
                # Issue #94: do not render a permanently empty reward gauge. The
                # underlying product does not record per-node reward yet.
                legend_html = (
                    f'<text x="{max(canvas_width - 200, 200)}" y="14" class="lane-note" text-anchor="end">'
                    'score gauge hidden: reward data gap</text>'
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
    history_mode: bool = False,
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

    # Build rows (up to 50 newest cycles shown by default; full history in history_mode).
    # Issue #90: sort newest-first by maximum ts in phases regardless of ledger insertion order.
    rows = []
    def _max_ts(phases: list[dict[str, Any]]) -> str:
        return max((str(p.get('ts') or '') for p in phases if isinstance(p, dict)), default='')
    cycle_items = sorted(cycles_dict.items(), key=lambda kv: _max_ts(kv[1]), reverse=True)
    # Take newest 50 (history_mode renders everything -- issue #72)
    window = cycle_items if history_mode else cycle_items[:50]
    last_day = None
    for cid, phases in window:
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

        # Issue #72: day grouping (newest-first) + outcome filter attribute.
        day_html = ''
        if history_mode and ts_val:
            day = str(ts_val)[:10]
            if day != last_day:
                last_day = day
                day_html = f'<li class="feed-day-header">{esc(day)}</li>\n        '

        rows.append(f'''
        {day_html}<li class="feed-row feed-outcome-{outcome_kind}" data-outcome="{esc(outcome_kind)}" id="cycle-{esc(cid)}">
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

    if history_mode:
        # Issue #90: bounded visible window; show-all control reveals overflow rows.
        _visible_window = 50
        if len(rows) > _visible_window:
            overflow = rows[_visible_window:]
            rows = rows[:_visible_window] + [
                r.replace('<li class="feed-row ', '<li class="feed-row feed-overflow-row ', 1)
                for r in overflow
            ]
        # Issue #89/#72: client-side outcome filter, state in URL hash (#f-<kind>).
        filter_buttons = ''.join(
            f'<button class="filter-btn" data-filter="{k}">{k}</button>'
            for k in ('all', 'integrated', 'failed', 'partial', 'skipped', 'running')
        )
        show_all_btn = (
            '<button class="feed-show-all" type="button" onclick="(function(){'
            'document.querySelectorAll(&quot;#panel-feed .feed-overflow-row&quot;).forEach(function(r){r.classList.remove(&quot;feed-overflow-row&quot;);});'
            'this.remove();}).call(this)">show all history</button>'
            if any('feed-overflow-row' in r for r in rows) else ''
        )
        filter_html = (
            f'<div class="filter-bar">{filter_buttons}</div>'
            f'{show_all_btn}'
            '<script>'
            '(function(){'
            'var bar=document.querySelector(".filter-bar");if(!bar)return;'
            'function getRows(){return document.querySelectorAll("#panel-feed li[data-outcome]");}'
            'function apply(k){'
            'getRows().forEach(function(r){r.classList.toggle("filtered-out",k!=="all"&&r.getAttribute("data-outcome")!==k);});'
            'bar.querySelectorAll(".filter-btn").forEach(function(b){b.classList.toggle("active",b.getAttribute("data-filter")===k);});}'
            'var h=location.hash||"",init=h.indexOf("#f-")===0?h.slice(3):"";'
            'apply(bar.querySelector("[data-filter=\'"+init+"\']")?init:"all");'
            'bar.addEventListener("click",function(e){'
            'var b=e.target.closest(".filter-btn");if(!b)return;'
            'var k=b.getAttribute("data-filter");'
            'if(k==="all"){history.replaceState(null,"",location.pathname+location.search);}'
            'else{location.hash="f-"+k;}'
            'apply(k);});'
            'window.addEventListener("hashchange",function(){'
            'var h=location.hash||"",k=h.indexOf("#f-")===0?h.slice(3):"";'
            'if(bar.querySelector("[data-filter=\'"+k+"\']")){apply(k);}});'
            '})();'
            '</script>'
        )
        title_line = f'Cycle History ({len(rows)} cycles)'
    else:
        filter_html = ''
        title_line = f'Cycle Feed (Recent {len(rows)})'

    return f'''
    <section class="panel panel-feed" id="panel-feed">
      <h2 class="panel-title">{title_line}</h2>
      {filter_html}
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
    hypotheses_durable: dict[str, Any] | None = None,
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

    # Issue #95: render strategist durable HADI hypotheses from backlog.json
    # as a separate section -- clearly labelled and separated from live data.
    durable_html = _build_durable_hadi_section(hypotheses_durable)

    return f'''
    <section class="panel panel-hypotheses" id="panel-hypotheses">
      <h2 class="panel-title">Hypotheses Lifecycle</h2>
      <div class="hypo-split">
        {active_html}
        {answered_html}
      </div>
      {durable_html}
    </section>
    '''


def _build_durable_hadi_section(hypotheses_durable: dict[str, Any] | None) -> str:
    """Issue #95: render the strategist durable HADI backlog (hypotheses/backlog.json)
    as a separate, clearly-labelled read-only section inside the hypotheses panel.

    - If the source is unavailable, renders a compact unavailable note (not an error).
    - Live lifecycle data and durable backlog are kept visually separated.
    - Stale vs selected entries are distinguished by badge class.
    - The selected hypothesis is surfaced prominently.
    """
    if not isinstance(hypotheses_durable, dict):
        return '<div class="hypo-durable-section"><p class="unavailable-note">strategist backlog (HADI): not available</p></div>'

    entries = hypotheses_durable.get('entries') or []
    model = str(hypotheses_durable.get('model') or hypotheses_durable.get('schema') or 'HADI')
    selected_id = str(hypotheses_durable.get('selected_hypothesis_id') or '')

    # backlog.json entries may be a list or a dict -- normalise to list
    if isinstance(entries, dict):
        entry_list: list[dict[str, Any]] = []
        for hid, val in entries.items():
            if isinstance(val, dict):
                item = dict(val)
                item.setdefault('hypothesis_id', hid)
                entry_list.append(item)
        entries = entry_list

    if not isinstance(entries, list) or not entries:
        return (
            '<div class="hypo-durable-section">'
            f'<h3 class="hypo-durable-title">Strategist ({model}) backlog</h3>'
            '<ul class="hypo-list"><li class="unavailable-note">no backlog entries recorded</li></ul>'
            '</div>'
        )

    rows: list[str] = []
    # Total count used exactly once -- single formula for the header counter.
    total = len(entries)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hid = str(entry.get('hypothesis_id') or entry.get('id') or '')
        title = str(entry.get('title') or hid or '(untitled)')
        sel_status = str(entry.get('selection_status') or 'candidate').lower()
        is_selected = hid == selected_id or sel_status == 'selected'
        badge_cls = 'badge-integrated' if is_selected else 'badge-researching'
        badge_lbl = 'SELECTED' if is_selected else sel_status.upper()

        # Render WSJF score if present
        wsjf = entry.get('wsjf')
        wsjf_html = ''
        if isinstance(wsjf, dict) and wsjf.get('score') is not None:
            wsjf_html = f'<span class="hypo-meta-item">WSJF&nbsp;{esc(wsjf["score"])}</span>'
        elif isinstance(wsjf, (int, float)):
            wsjf_html = f'<span class="hypo-meta-item">WSJF&nbsp;{esc(wsjf)}</span>'

        # Render HADI statement, action, success criterion, and creation time.
        hadi = entry.get('hadi') if isinstance(entry.get('hadi'), dict) else {}
        statement = entry.get('hypothesis') or hadi.get('hypothesis') or ''
        action = entry.get('action') or hadi.get('action') or ''
        criterion = entry.get('insight_criterion') or entry.get('success_criterion') or ''
        created = entry.get('created_at') or entry.get('created_ts') or ''
        hadi_html = ''.join(
            f'<div class="hypo-durable-detail"><span class="hypo-label">{label}:</span> {esc(value)}</div>'
            for label, value in (
                ('Statement', str(statement)),
                ('Action', str(action)),
                ('Success criterion', str(criterion)),
                ('Created', str(created)),
            ) if value
        )

        rows.append(f'''
            <li class="hypo-row hypo-durable{" hypo-selected" if is_selected else ""}">
              <span class="badge {badge_cls}">{esc(badge_lbl)}</span>
              <strong class="hypo-title">{esc(title)}</strong>
              <div class="hypo-meta">{wsjf_html}</div>
              {hadi_html}
            </li>
        ''')

    if not rows:
        rows_html = '<li class="unavailable-note">no valid backlog entries</li>'
    else:
        rows_html = ''.join(rows)

    return (
        '<div class="hypo-durable-section">'
        f'<h3 class="hypo-durable-title">Strategist ({esc(model)}) backlog ({total})</h3>'
        f'<ul class="hypo-list">{rows_html}</ul>'
        '</div>'
    )


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


def _build_proposer_block(
    proposer_stats: dict[str, Any] | None,
    ledger_tail: list[dict[str, Any]] | None,
) -> str:
    """Issue #63: proposer visibility -- last proposal decision (title or
    skip reason) from the ledger + per-day cost aggregates from llm_calls
    rows with component == 'proposer'. Read-only pass-through."""
    decision_html = ''
    if isinstance(ledger_tail, list):
        for row in reversed(ledger_tail):
            if not isinstance(row, dict):
                continue
            phase = row.get('phase')
            if phase == 'proposed' and row.get('task_title'):
                decision_html = (
                    f'<p class="proposer-decision">last proposal: '
                    f'<strong>{esc(str(row["task_title"]))}</strong></p>'
                )
                break
            if phase == 'proposer_reject':
                reason = row.get('reason') or row.get('decision') or 'no_valuable_task'
                decision_html = (
                    f'<p class="proposer-decision">last decision: '
                    f'<strong>skipped ({esc(str(reason))})</strong></p>'
                )
                break
            if phase == 'proposed' and row.get('decision'):
                decision_html = (
                    f'<p class="proposer-decision">last decision: '
                    f'<strong>{esc(str(row["decision"]))}</strong></p>'
                )
                break

    stats_html = ''
    if isinstance(proposer_stats, dict) and proposer_stats.get('calls'):
        model_html = (
            f'<span class="proposer-model">model: {esc(str(proposer_stats.get("last_model") or "n/a"))}</span>'
        )
        day_rows = ''
        days = proposer_stats.get('days')
        if isinstance(days, dict):
            for day in sorted(days, reverse=True)[:7]:
                d = days[day]
                calls = d.get('calls', 0)
                mean_ms = (d.get('duration_ms', 0.0) / calls) if calls else 0.0
                day_rows += (
                    f'<tr><td>{esc(day)}</td>'
                    f'<td class="skill-reads">{calls}</td>'
                    f'<td>{fmt_compact(d.get("total_tokens", 0))}</td>'
                    f'<td>{_fmt_duration_ms(mean_ms) or "n/a"}</td></tr>'
                )
        if day_rows:
            stats_html = (
                f'<p class="proposer-model-line">{model_html} &middot; '
                f'{proposer_stats.get("calls", 0)} calls in the last 7 days</p>'
                '<table class="skills-table proposer-table">'
                '<thead><tr><th>Day</th><th>Calls</th><th>Tokens</th><th>Mean latency</th></tr></thead>'
                f'<tbody>{day_rows}</tbody></table>'
            )
    if not decision_html and not stats_html:
        return '<p class="unavailable-note">no proposer data recorded</p>'
    missing = '' if stats_html else '<p class="unavailable-note">no proposer llm calls recorded yet</p>'
    missing_d = '' if decision_html else '<p class="unavailable-note">no proposal decisions in the recent ledger</p>'
    return f'{decision_html}{missing_d}{stats_html}{missing}'


def _is_v2_lesson(lesson: dict[str, Any]) -> bool:
    """Return True when the lesson record has a non-empty 'problem' field,
    which is the sentinel for the v2 schema (ozand/eeebot#1071)."""
    return bool(lesson.get('problem'))


def build_lessons_panel(lessons: list[dict[str, Any]] | None) -> str:
    """Issue #73/#96: lessons history page — v2 entries rendered as
    problem→solution cards (with tags/severity/seen_count); legacy
    protocol records folded under 'legacy (pre-v2, frozen)'."""
    entries = [l for l in (lessons or []) if isinstance(l, dict)]
    if not entries:
        return (
            '<section class="panel panel-lessons" id="panel-lessons">'
            '<h2 class="panel-title">Lessons History</h2>'
            '<p class="unavailable-note">no lessons data recorded</p>'
            '</section>'
        )

    v2_entries = [l for l in entries if _is_v2_lesson(l)]
    legacy_entries = [l for l in entries if not _is_v2_lesson(l)]

    today = datetime.now(timezone.utc).date()
    recent = 0
    for l in v2_entries:
        d = _parse_iso_ts(l.get('date'))
        if d is not None and (today - d.date()).days <= 7:
            recent += 1

    # --- v2 card rows ---
    v2_rows: list[str] = []
    last_date: str | None = None
    for l in v2_entries:
        date = str(l.get('date') or 'unknown')
        if date != last_date:
            last_date = date
            v2_rows.append(f'<li class="lesson-day-header">{esc(date)}</li>')
        cid = str(l.get('cycle_id') or '')
        cycle_link = (
            f'<a href="cycles.html#cycle-{esc(cid)}" class="lesson-cycle">{esc(cid)}</a>'
            if cid else '<span class="lesson-cycle">n/a</span>'
        )
        problem = str(l.get('problem') or '')
        solution = str(l.get('solution') or '')
        tags = l.get('tags') or []
        tags_list = tags if isinstance(tags, list) else [str(tags)]
        severity = str(l.get('severity') or '')
        seen_count = l.get('seen_count')

        severity_html = (
            f'<span class="lesson-severity lesson-severity-{esc(severity.lower())}">{esc(severity)}</span>'
            if severity else ''
        )
        tags_html = (
            ''.join(f'<span class="lesson-tag">{esc(str(t))}</span>' for t in tags_list)
            if tags_list else ''
        )
        seen_html = (
            f'<span class="lesson-seen" title="times this pattern was observed">×{esc(str(seen_count))}</span>'
            if seen_count is not None else ''
        )
        meta_chips = severity_html + tags_html + seen_html
        meta_chips_html = f'<div class="lesson-chips">{meta_chips}</div>' if meta_chips else ''

        problem_html = f'<div class="lesson-problem"><span class="lesson-label">Problem:</span> {esc(problem[:400])}{"..." if len(problem) > 400 else ""}</div>' if problem else ''
        solution_html = f'<div class="lesson-solution"><span class="lesson-label">Solution:</span> {esc(solution[:400])}{"..." if len(solution) > 400 else ""}</div>' if solution else ''

        search_text = esc((' '.join([
            l.get('id') or '', str(l.get('task_id') or ''), problem, solution,
            severity, ' '.join(str(t) for t in tags_list), cid,
        ])).lower())
        v2_rows.append(
            f'<li class="lesson-row lesson-row-v2" data-text="{search_text}">'
            f'<div class="lesson-meta"><span class="lesson-id" translate="no">{esc(l.get("id") or "")}</span>'
            f' {cycle_link}</div>'
            f'{meta_chips_html}'
            f'{problem_html}'
            f'{solution_html}'
            f'</li>'
        )

    # --- legacy rows (pre-v2, frozen) ---
    legacy_rows: list[str] = []
    last_date = None
    for l in legacy_entries:
        date = str(l.get('date') or 'unknown')
        if date != last_date:
            last_date = date
            legacy_rows.append(f'<li class="lesson-day-header">{esc(date)}</li>')
        cid = str(l.get('cycle_id') or '')
        cycle_link = (
            f'<a href="cycles.html#cycle-{esc(cid)}" class="lesson-cycle">{esc(cid)}</a>'
            if cid else '<span class="lesson-cycle">n/a</span>'
        )
        result = str(l.get('result') or '')
        result_body = esc(result[:400]) + ('...' if len(result) > 400 else '')
        result_html = (
            f'<details class="lesson-result"><summary>result</summary>'
            f'<pre>{result_body}</pre></details>' if result else ''
        )
        insight = str(l.get('insight') or '')
        insight_html = f'<div class="lesson-insight">{esc(insight[:300])}</div>' if insight else ''
        search_text = esc((' '.join([
            l.get('id') or '', str(l.get('task_id') or ''), str(l.get('hypothesis') or ''),
            result, insight, cid,
        ])).lower())
        legacy_rows.append(
            f'<li class="lesson-row" data-text="{search_text}">'
            f'<div class="lesson-meta"><span class="lesson-id" translate="no">{esc(l.get("id") or "")}</span>'
            f' {cycle_link}</div>'
            f'<div class="lesson-title">{esc(str(l.get("task_id") or "n/a"))}</div>'
            f'<div class="lesson-hypothesis">{esc(str(l.get("hypothesis") or ""))}</div>'
            f'{result_html}{insight_html}</li>'
        )

    # --- assemble page ---
    v2_count = len(v2_entries)
    legacy_count = len(legacy_entries)
    total = len(entries)

    title_parts = [f'{v2_count} v2' if v2_count else 'no v2 entries']
    if legacy_count:
        title_parts.append(f'{legacy_count} legacy')
    if recent:
        title_parts.append(f'{recent} recent')
    heading_detail = ' &middot; '.join(title_parts)

    v2_section = ''.join(v2_rows) if v2_rows else '<li class="lesson-row"><p class="unavailable-note">no v2 lessons recorded yet</p></li>'

    legacy_section_html = ''
    if legacy_rows:
        legacy_section_html = (
            f'<li class="lesson-legacy-fold">'
            f'<details class="lesson-legacy-details">'
            f'<summary class="lesson-legacy-summary">legacy (pre-v2, frozen) &mdash; {legacy_count} historical protocol records</summary>'
            f'<ul class="lessons-list lessons-list-legacy">{"" .join(legacy_rows)}</ul>'
            f'</details></li>'
        )

    return f'''
    <section class="panel panel-lessons" id="panel-lessons">
      <h2 class="panel-title">Lessons History ({total} total &middot; {heading_detail})</h2>
      <input class="lessons-filter" type="text" placeholder="filter lessons...">
      <ul class="lessons-list">
        {v2_section}
        {legacy_section_html}
      </ul>
      <script>
      (function(){{
        var input = document.querySelector('.lessons-filter');
        if (!input) return;
        var rowsL = document.querySelectorAll('#panel-lessons .lesson-row');
        function apply(q) {{
          var t = (q || '').toLowerCase();
          rowsL.forEach(function (r) {{
            r.style.display = (!t || r.getAttribute('data-text').indexOf(t) !== -1) ? '' : 'none';
          }});
          input.value = t;
        }}
        var init = decodeURIComponent((location.hash || '').replace('#q-', ''));
        apply(init);
        input.addEventListener('input', function () {{
          var q = input.value.trim();
          if (q) {{ location.hash = 'q-' + encodeURIComponent(q); }} else {{ history.replaceState(null, '', location.pathname + location.search); }}
          apply(q);
        }});
        window.addEventListener('hashchange', function () {{
          apply(decodeURIComponent((location.hash || '').replace('#q-', '')));
        }});
      }})();
      </script>
    </section>
    '''


def build_agent_panel(
    agents_md: str | None,
    goal_text: dict[str, Any] | None,
    skill_reads: dict[str, Any] | None,
    skill_evals: list[dict[str, Any]] | None = None,
    portfolio: dict[str, Any] | None = None,
    ledger_tail: list[dict[str, Any]] | None = None,
    host: str | None = None,
    proposer_stats: dict[str, Any] | None = None,
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
    eval_by_skill: dict[str, list[Any]] = {}
    if isinstance(skill_evals, list):
        for row in skill_evals:
            if isinstance(row, dict) and row.get('skill'):
                skill_name = str(row['skill'])
                eval_by_skill.setdefault(skill_name, []).append(row)
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
                    confirmed = sum(
                        1 for row in reads_list
                        if isinstance(row, dict) and row.get('skill') == sname and row.get('confirmed') is True
                    )
                    usage_note = f'<span class="skill-usage-confirmed">{confirmed} confirmed</span>'
                    eval_rows = eval_by_skill.get(sname, [])
                    if eval_rows:
                        deltas = [row.get('delta') for row in eval_rows]
                        numeric = [float(delta) for delta in deltas if isinstance(delta, (int, float)) and not isinstance(delta, bool)]
                        eval_text = f'eval delta: {sum(numeric):+.3g}' if numeric else f'eval rows: {len(eval_rows)}'
                        usage_note += f' <span class="skill-eval-delta">{eval_text}</span>'
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
                    <tr><th>Skill</th><th>Reads (skill_fitness/reads.json)</th><th>Confirmed Usage (skill_fitness/reads.json; skill_fitness/evals.jsonl when present)</th></tr>
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
      <div class="proposer-block">
        <h3>Proposer</h3>
        {_build_proposer_block(proposer_stats, ledger_tail)}
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
        ('confirmed integration ratio', humanize_ratio(loop.get('confirmed_integration_ratio'))),
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
        'confirmed integration ratio': 'scorecard key: loop.confirmed_integration_ratio; share of confirmable integrations later confirmed working',
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
        'confirmed integration ratio': 'confirmed_integration_ratio',
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
    /* Issue #72: full-history page — day grouping + outcome filter. */
    .feed-day-header {
      font-size: 0.72em;
      text-transform: uppercase;
      letter-spacing: 1px;
      color: #8aa695;
      border-top: 1px dashed #2f5c46;
      padding: 10px 4px 2px;
      list-style: none;
    }
    .filter-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 0 0 10px 0;
    }
    .filter-btn {
      font-family: 'Consolas', monospace;
      font-size: 0.75em;
      color: #9db4a6;
      background: rgba(8, 15, 11, 0.8);
      border: 1px solid #28503c;
      border-radius: 4px;
      padding: 2px 10px;
      cursor: pointer;
    }
    .filter-btn.active {
      color: #56d364;
      border-color: #56d364;
    }
    .feed-row.filtered-out { display: none; }
    .feed-overflow-row { display: none; }
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
    /* Issue #70: multi-page site nav -- current page highlighted. */
    .site-nav a.nav-current {
      color: #56d364;
      font-weight: 700;
      text-decoration: underline;
    }
    /* Issue #70: index teasers. */
    .teaser-list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 0.88em;
    }
    .teaser-list a { color: #2fd3c4; font-family: 'Consolas', monospace; }
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
    /* Issue #63: proposer visibility block. */
    .proposer-block {
      margin-top: 14px;
      padding-top: 10px;
      border-top: 1px solid #182a20;
    }
    .proposer-block h3 {
      font-size: 0.9em;
      color: #9db4a6;
      text-transform: uppercase;
      letter-spacing: 1px;
      margin: 0 0 8px 0;
    }
    .proposer-decision { font-size: 0.85em; margin: 0 0 8px 0; color: #dcebe1; }
    .proposer-model-line { font-size: 0.8em; color: #8aa695; margin: 0 0 8px 0; }
    .proposer-model { color: #56d364; font-family: 'Consolas', monospace; }
    .proposer-table { max-width: 480px; }
    /* Issue #73: lessons history page. */
    .lessons-filter {
      font-family: 'Consolas', monospace;
      font-size: 0.82em;
      color: #dcebe1;
      background: rgba(8, 15, 11, 0.9);
      border: 1px solid #28503c;
      border-radius: 4px;
      padding: 4px 8px;
      margin: 0 0 10px 0;
      width: min(420px, 100%);
    }
    .lessons-list { list-style: none; margin: 0; padding: 0; }
    .lesson-day-header {
      font-size: 0.72em;
      text-transform: uppercase;
      letter-spacing: 1px;
      color: #8aa695;
      border-top: 1px dashed #2f5c46;
      padding: 10px 4px 2px;
    }
    .lesson-row {
      background: rgba(12, 22, 17, 0.7);
      border: 1px solid #1e3a2d;
      border-radius: 6px;
      padding: 8px 12px;
      margin: 6px 0;
      font-size: 0.85em;
    }
    .lesson-meta { font-size: 0.8em; color: #8aa695; }
    .lesson-id { font-family: 'Consolas', monospace; color: #56d364; }
    .lesson-cycle { font-family: 'Consolas', monospace; color: #2fd3c4; margin-left: 8px; }
    .lesson-title { color: #e2f0e6; font-weight: 600; margin: 4px 0 2px; }
    .lesson-hypothesis { color: #9db4a6; font-size: 0.92em; }
    .lesson-result summary { cursor: pointer; color: #8aa695; font-size: 0.85em; }
    .lesson-result pre {
      margin: 4px 0 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 0.85em;
      color: #cfe3d7;
    }
    .lesson-insight { color: #d19a66; font-size: 0.85em; margin-top: 4px; }
    /* Issue #96: v2 lesson card styles */
    .lesson-row-v2 { border-color: #1e4a38; }
    .lesson-problem {
      color: #e2b2b2;
      font-size: 0.87em;
      margin: 4px 0 2px;
    }
    .lesson-solution {
      color: #a8d9b2;
      font-size: 0.87em;
      margin: 2px 0 4px;
    }
    .lesson-label { font-weight: 600; color: #8aa695; }
    .lesson-chips { display: flex; flex-wrap: wrap; gap: 4px; margin: 4px 0; }
    .lesson-tag {
      display: inline-block;
      font-size: 0.72em;
      background: rgba(38, 80, 60, 0.5);
      border: 1px solid #2f5c46;
      border-radius: 3px;
      padding: 1px 5px;
      color: #9db4a6;
      font-family: 'Consolas', monospace;
    }
    .lesson-severity {
      display: inline-block;
      font-size: 0.72em;
      border-radius: 3px;
      padding: 1px 6px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .lesson-severity-critical { background: rgba(180, 60, 60, 0.25); color: #e57373; border: 1px solid #b03030; }
    .lesson-severity-high     { background: rgba(180, 120, 40, 0.25); color: #e0a060; border: 1px solid #a06020; }
    .lesson-severity-medium   { background: rgba(180, 160, 40, 0.20); color: #d4c060; border: 1px solid #8a7820; }
    .lesson-severity-low      { background: rgba(60, 120, 80, 0.20); color: #8aa695; border: 1px solid #2f5c46; }
    .lesson-seen {
      display: inline-block;
      font-size: 0.72em;
      color: #8aa695;
      background: rgba(30, 58, 45, 0.5);
      border: 1px solid #1e3a2d;
      border-radius: 3px;
      padding: 1px 5px;
      font-family: 'Consolas', monospace;
    }
    .lesson-legacy-fold { list-style: none; margin: 12px 0 0; }
    .lesson-legacy-details > summary.lesson-legacy-summary {
      cursor: pointer;
      font-size: 0.78em;
      color: #5a7a68;
      padding: 6px 4px;
      border-top: 1px dashed #2f5c46;
      user-select: none;
    }
    .lesson-legacy-details > summary.lesson-legacy-summary:hover { color: #8aa695; }
    .lessons-list-legacy { margin-top: 6px; }
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
    /* Issue #71: DGM archive tree. */
    .arch-edge { stroke: #2f5c46; stroke-width: 1.4; }
    .arch-edge-best { stroke: #e2f0e6; stroke-width: 3.2; }
    .arch-node { cursor: pointer; }
    .lineage-day-controls { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding: 8px 12px; }
    .lineage-day-controls button { background: #10271a; color: #b8d0c2; border: 1px solid #2f5c46; padding: 4px 8px; cursor: pointer; }
    .lineage-day-controls button.active { border-color: #56d364; color: #56d364; }
    .lineage-day-controls input { background: #08110c; color: #dcebe1; border: 1px solid #2f5c46; padding: 3px; }
    .lineage-day-group { padding: 4px 12px 10px; }
    .lineage-day-group[hidden] { display: none; }
    .lineage-day-group h3 { color: #56d364; font-size: .8rem; margin: 4px 0; }
    .lineage-day-svg { display: block; max-width: 100%; height: auto; overflow: visible; }
    .lineage-node { fill: #2fd3c4; stroke: #dcebe1; stroke-width: 2; }
    .lineage-edge { stroke: #2f5c46; stroke-width: 2; }
    .lineage-hidden-parent { fill: #d19a66; font-size: 10px; }
    .lineage-day-truncated { color: #d19a66; font-size: .75rem; }
    .arch-node.cycle-node-selected { stroke: #ffffff; stroke-width: 6; }
    .cycle-details-panel { max-width: 760px; margin: 14px 12px; padding: 14px 16px; border: 1px solid #2f5c46; border-left: 4px solid #56d364; background: #0c1912; box-shadow: 0 8px 24px rgba(0,0,0,.28); }
    .cycle-details-panel[hidden] { display: none; }
    .cycle-details-close { float: right; background: transparent; color: #8aa695; border: 1px solid #2f5c46; padding: 4px 8px; cursor: pointer; }
    .cycle-details-title { margin: 0 0 12px; color: #e2f0e6; font-size: 1rem; }
    .cycle-details-body { color: #b8d0c2; font-size: .82rem; line-height: 1.5; }
    .cycle-details-body h3 { color: #56d364; font-size: .78rem; margin: 12px 0 4px; }
    .cycle-details-body p { margin: 4px 0; }
    .cycle-details-body ul { margin: 4px 0 8px 20px; padding: 0; }
    .cycle-details-links { border-top: 1px solid #1e3b2b; padding-top: 10px; }
    .arch-star { fill: #56d364; font-size: 14px; }
    .arch-legend-label { fill: #8aa695; font-size: 10px; font-family: 'Consolas', monospace; }
    /* Issue #93: day separator lines and date labels in the archive tree. */
    .arch-day-sep { stroke: #2f5c46; stroke-width: 1; stroke-dasharray: 4 3; }
    .arch-day-label { fill: #5a7a68; font-size: 9px; font-family: 'Consolas', monospace; }
    /* Issue #93: date sub-label in evolution node meta. */
    .evo-date { color: #5a7a68; font-size: 8px; margin-left: 4px; }
    .arch-note {
      font-size: 0.78em;
      color: #8aa695;
      font-family: 'Consolas', monospace;
      padding: 8px 12px 0;
    }
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
<footer class="page-footer">generator {generator_sha} · generated {generated_at} UTC &middot; host {host} &middot; newest source {source_age}{computed_note}{error_note}{titles_note}</footer>
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


def _generator_sha() -> str:
    """Return the generator's git short SHA.

    Preference order (issue #101):
    1. Module-level ``_BAKED_GENERATOR_SHA`` — set by deploy_generator.sh at
       deploy time via ``sed -i``; non-empty when running from /opt, so no
       git repo is required on the host.
    2. ``git rev-parse --short HEAD`` — works when running directly from the
       repo (operator workstation / CI).
    3. ``'unknown'`` — neither source is available.
    """
    if _BAKED_GENERATOR_SHA:
        return _BAKED_GENERATOR_SHA
    try:
        result = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else 'unknown'
    except Exception:
        return 'unknown'


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
    hypotheses_durable = data.get('hypotheses_durable')
    ledger_tail = data.get('ledger_tail')
    demand_rotation = data.get('demand_rotation')
    demand_completed = data.get('demand_completed')
    skill_reads = data.get('skill_reads')
    skill_evals = data.get('skill_evals')
    goal_text = data.get('goal_text')
    agents_md = data.get('agents_md')
    cycle_titles = data.get('cycle_titles')
    cycle_details = build_cycle_details(
        ledger_tail, evolution_tree, data.get('lessons'), data.get('reflections'),
        cycle_titles, data.get('cycle_files'),
    )

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
    generator_sha = str(data.get('generator_sha') or '').strip() or _generator_sha()
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
    hypotheses_panel = build_hypotheses_panel(
        hypotheses, feed_cycles=feed_cycles, hypotheses_durable=hypotheses_durable
    )
    agent_panel = build_agent_panel(
        agents_md=agents_md,
        goal_text=goal_text,
        skill_reads=skill_reads,
        skill_evals=skill_evals,
        portfolio=portfolio,
        ledger_tail=ledger_tail,
        host=host,
        proposer_stats=data.get('proposer_stats'),
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
        generator_sha=generator_sha or 'unknown',
    )


# ---------------------------------------------------------------------------
# Multi-page site (issue #70): index / lineage / cycles / lessons / agent /
# hypotheses + techtree.html redirect. Same chrome, one shared data pass.
# ---------------------------------------------------------------------------

SITE_PAGES = [
    ('index.html', 'now'),
    ('lineage.html', 'lineage'),
    ('cycles.html', 'cycles'),
    ('lessons.html', 'lessons'),
    ('agent.html', 'agent'),
    ('hypotheses.html', 'hypotheses'),
]

SITE_TEMPLATE = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
{empire_strip}
{site_nav}
<main class="dashboard-main">
{page_main}
</main>
<footer class="page-footer">generator {generator_sha} · generated {generated_at} UTC &middot; host {host} &middot; newest source {source_age}{computed_note}{error_note}{titles_note}</footer>
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

TECHTREE_REDIRECT = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url=index.html">
<title>eeebot tech tree moved</title>
</head>
<body>
<p>The tech tree moved to <a href="index.html">index.html</a>.</p>
</body>
</html>
'''


def _site_nav(current: str) -> str:
    parts = []
    for fname, label in SITE_PAGES:
        cls = ' class="nav-current"' if fname == current else ''
        parts.append(f'<a href="{fname}"{cls}>{label}</a>')
    return '<nav class="panel-nav site-nav" aria-label="Sections">' + ' &middot; '.join(parts) + '</nav>'


def _site_page(title: str, current: str, empire_strip: str, page_main: str,
               generated_at: str, host: str, source_age: str,
               computed_note: str, error_note: str, titles_note: str, generator_sha: str = 'unknown') -> str:
    return SITE_TEMPLATE.format(
        css=CSS,
        title=esc(title),
        empire_strip=empire_strip,
        site_nav=_site_nav(current),
        page_main=page_main,
        generated_at=esc(generated_at),
        host=esc(host),
        source_age=esc(source_age),
        computed_note=computed_note,
        error_note=error_note,
        titles_note=titles_note,
        generator_sha=generator_sha,
    )


def _index_teasers(data: dict[str, Any], ledger_tail: list[Any] | None,
                   evolution_tree: dict[str, Any] | None,
                   hypotheses: dict[str, Any] | None) -> str:
    """Compact per-section teasers linking to the dedicated pages."""
    cycle_ids = {
        str(r.get('cycle_id')) for r in (ledger_tail or [])
        if isinstance(r, dict) and r.get('cycle_id')
    }
    node_count = 0
    if isinstance(evolution_tree, dict) and isinstance(evolution_tree.get('nodes'), dict):
        node_count = len(evolution_tree['nodes'])
    active = answered = 0
    if isinstance(hypotheses, dict) and isinstance(hypotheses.get('entries'), dict):
        for e in hypotheses['entries'].values():
            if not isinstance(e, dict):
                continue
            if e.get('status') == 'answered':
                answered += 1
            else:
                active += 1
    durable = data.get('hypotheses_durable')
    durable_entries = durable.get('entries', []) if isinstance(durable, dict) else []
    durable_count = len(durable_entries) if isinstance(durable_entries, (list, dict)) else 0
    return f'''
    <section class="panel panel-teasers">
      <h2 class="panel-title">Explore</h2>
      <ul class="teaser-list">
        <li><a href="cycles.html">cycles</a> &mdash; {len(cycle_ids)} cycles tracked in the recent ledger window</li>
        <li><a href="lineage.html">lineage</a> &mdash; {node_count} evolution nodes</li>
        <li><a href="hypotheses.html">hypotheses</a> &mdash; {active} active / {answered} answered + {durable_count} strategist durable</li>
        <li><a href="agent.html">agent</a> &mdash; charter, skills, proposer</li>
        <li><a href="lessons.html">lessons</a> &mdash; lessons history</li>
      </ul>
    </section>
    '''


def _last_cycles_subset(ledger_tail: list[Any] | None, want: int = 3) -> list[Any]:
    """Rows covering the last `want` distinct cycle ids (phase rows kept
    together so build_cycle_feed still groups them correctly)."""
    if not isinstance(ledger_tail, list):
        return []
    seen: set[str] = set()
    keep: set[str] = set()
    for row in reversed(ledger_tail):
        if not isinstance(row, dict):
            continue
        cid = str(row.get('cycle_id') or '')
        if not cid:
            continue
        if cid not in seen and len(seen) >= want:
            continue
        seen.add(cid)
        keep.add(cid)
    return [r for r in ledger_tail if isinstance(r, dict) and str(r.get('cycle_id') or '') in keep]


def render_pages(data: dict[str, Any], host: str, generated_at: str | None = None) -> dict[str, str]:
    """Issue #70: render the multi-page site. Returns {filename: html} with
    keys index/lineage/cycles/lessons/agent/hypotheses/techtree .html. All
    pages share the header KPI strip, freshness badge, site nav and theme;
    every page derives from the same data pass, so the existing publish
    digest (computed over all sources) still triggers on any input change."""
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    portfolio = data.get('portfolio')
    scorecard = data.get('scorecard')
    evolution_tree = data.get('evolution_tree')
    hypotheses = data.get('hypotheses')
    hypotheses_durable = data.get('hypotheses_durable')
    ledger_tail = data.get('ledger_tail')
    demand_rotation = data.get('demand_rotation')
    demand_completed = data.get('demand_completed')
    skill_reads = data.get('skill_reads')
    skill_evals = data.get('skill_evals')
    goal_text = data.get('goal_text')
    agents_md = data.get('agents_md')
    cycle_titles = data.get('cycle_titles')
    cycle_details = build_cycle_details(
        ledger_tail, evolution_tree, data.get('lessons'), data.get('reflections'),
        cycle_titles, data.get('cycle_files'),
    )

    error_note = ''
    if data.get('_error'):
        error_note = ' &middot; fetch note: state read failed (details in the publisher\'s logs)'
    titles_note = ''
    if data.get('cycle_titles_error'):
        titles_note = f' &middot; &#9888; task titles unavailable ({esc(str(data.get("cycle_titles_error")))})'
    computed_note = ''
    generator_sha = str(data.get('generator_sha') or '').strip() or _generator_sha()
    if isinstance(scorecard, dict) and scorecard.get('computed_at_utc'):
        computed_note = f' &middot; <span class="footer-computed">scorecard computed {fmt_ts(scorecard.get("computed_at_utc"))}</span>'

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
    now_panel = build_now_panel(
        portfolio=portfolio,
        evolution_tree=evolution_tree,
        demand_rotation=demand_rotation,
        demand_completed=demand_completed,
        task_titles=cycle_titles,
        ledger_tail=ledger_tail,
    )
    # Issue #71: lineage.html renders the DGM archive tree (full history);
    # the legacy single-page render keeps build_tech_canvas.
    canvas_html = build_archive_tree(
        evolution_tree,
        ledger_tail,
        task_titles=cycle_titles,
        cycle_details=cycle_details,
        ledger_history=data.get('ledger_history'),
        now=generated_at,
    )
    feed_cycles = set()
    if isinstance(ledger_tail, list):
        feed_cycles = {
            str(r.get('cycle_id') or '')
            for r in ledger_tail
            if isinstance(r, dict) and r.get('cycle_id')
        }
    # Issue #72: cycles.html renders FULL history (live ledger + .gz
    # archives) with day grouping and the outcome filter; index teaser
    # keeps the recent-tail feed.
    history_rows = data.get('ledger_history')
    history_source = ledger_tail
    if isinstance(history_rows, list) and history_rows:
        history_source = history_rows
    cycle_feed = build_cycle_feed(
        ledger_tail=history_source,
        demand_completed=demand_completed,
        task_titles=cycle_titles,
        evolution_tree=evolution_tree,
        cycle_files=data.get('cycle_files'),
        llm_stats=data.get('llm_stats'),
        history_mode=True,
    )
    hypotheses_panel = build_hypotheses_panel(
        hypotheses, feed_cycles=feed_cycles, hypotheses_durable=hypotheses_durable
    )
    agent_panel = build_agent_panel(
        agents_md=agents_md,
        goal_text=goal_text,
        skill_reads=skill_reads,
        skill_evals=skill_evals,
        portfolio=portfolio,
        ledger_tail=ledger_tail,
        host=host,
        proposer_stats=data.get('proposer_stats'),
    )
    lessons_panel = build_lessons_panel(data.get('lessons'))

    def _page(title: str, current: str, page_main: str) -> str:
        return _site_page(title, current, empire_strip, page_main,
                          generated_at, host, source_age,
                          computed_note, error_note, titles_note, generator_sha or 'unknown')

    teaser_html = _index_teasers(data, ledger_tail, evolution_tree, hypotheses)
    teaser_feed = build_cycle_feed(
        ledger_tail=_last_cycles_subset(ledger_tail, 3),
        demand_completed=demand_completed,
        task_titles=cycle_titles,
        evolution_tree=evolution_tree,
        cycle_files=data.get('cycle_files'),
        llm_stats=data.get('llm_stats'),
    )

    pages: dict[str, str] = {
        'index.html': _page('eeebot / now', 'index.html', now_panel + teaser_html + teaser_feed),
        'lineage.html': _page('eeebot / lineage', 'lineage.html', canvas_html),
        'cycles.html': _page('eeebot / cycles', 'cycles.html', cycle_feed),
        'lessons.html': _page('eeebot / lessons', 'lessons.html', lessons_panel),
        'agent.html': _page('eeebot / agent', 'agent.html', agent_panel),
        'hypotheses.html': _page('eeebot / hypotheses', 'hypotheses.html', hypotheses_panel),
        'techtree.html': TECHTREE_REDIRECT,
    }
    return pages


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


def publish_to_pages(pages: 'dict[str, str] | str') -> int:
    """Issue #70: publish the multi-page site ATOMICALLY -- one gh-pages
    commit carries every page (git Data API: blobs -> tree -> commit -> ref
    update; any failure leaves the ref untouched). Accepts a dict
    {filename: html}; a legacy single-page string is treated as
    index.html. Returns 0 on success, 1 on any failure."""
    import base64
    if isinstance(pages, str):
        pages = {'index.html': pages}
    if not pages:
        print('publish: nothing to publish', file=sys.stderr)
        return 1

    pages = dict(pages)
    if any('assets/vendor/lineage-renderer.js' in html for html in pages.values()):
        vendor_root = Path(__file__).resolve().parent.parent / 'assets' / 'vendor'
        for name in ('d3.min.js', 'd3-dag.iife.min.js', 'lineage-renderer.js'):
            path = vendor_root / name
            if path.is_file():
                pages[f'assets/vendor/{name}'] = path.read_text(encoding='utf-8')

    # Branch may not exist yet: bootstrap it from the default branch HEAD.
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

    head_tree = _gh(['api', f'repos/{PUBLISH_REPO}/branches/{PUBLISH_BRANCH}',
                     '--jq', '.commit.commit.tree.sha'])
    base_tree = head_tree.stdout.strip() if head_tree.returncode == 0 else None

    # 1. Create a blob per page.
    tree_entries = []
    for fname, html in sorted(pages.items()):
        blob_b64 = base64.b64encode(html.encode('utf-8')).decode('ascii')
        # Issue #72: full-history cycles.html exceeds the OS argv limit —
        # pass the blob payload via stdin (JSON body) instead of -f args.
        blob_body = json.dumps({'content': blob_b64, 'encoding': 'base64'})
        blob = _gh(['api', '-X', 'POST', f'repos/{PUBLISH_REPO}/git/blobs',
                    '--input', '-',
                    '--jq', '.sha'], input_text=blob_body)
        if blob.returncode != 0:
            print(f'publish: blob {fname} failed: {blob.stderr.strip()[:200]}',
                  file=sys.stderr)
            return 1
        entry = {'path': fname, 'mode': '100644', 'type': 'blob',
                 'sha': blob.stdout.strip()}
        tree_entries.append(entry)

    # 2. One tree carrying every page.
    import json as _json
    tree_payload = {'tree': tree_entries}
    if base_tree:
        tree_payload['base_tree'] = base_tree
    tree = _gh(['api', '-X', 'POST', f'repos/{PUBLISH_REPO}/git/trees',
                '--jq', '.sha', '--input', '-'], input_text=_json.dumps(tree_payload))
    if tree.returncode != 0:
        print(f'publish: tree create failed: {tree.stderr.strip()[:300]}',
              file=sys.stderr)
        return 1

    # 3. One commit.
    parent = _gh(['api', f'repos/{PUBLISH_REPO}/git/ref/heads/{PUBLISH_BRANCH}',
                  '--jq', '.object.sha'])
    commit_payload = {
        'message': 'techtree multi-page snapshot (techtree_viewer --publish)',
        'tree': tree.stdout.strip(),
    }
    if parent.returncode == 0 and parent.stdout.strip():
        commit_payload['parents'] = [parent.stdout.strip()]
    commit = _gh(['api', '-X', 'POST', f'repos/{PUBLISH_REPO}/git/commits',
                  '--jq', '.sha', '--input', '-'], input_text=_json.dumps(commit_payload))
    if commit.returncode != 0:
        print(f'publish: commit failed: {commit.stderr.strip()[:300]}',
              file=sys.stderr)
        return 1

    # 4. Single ref update -- the atomic switch.
    ref = _gh(['api', '-X', 'PATCH', f'repos/{PUBLISH_REPO}/git/refs/heads/{PUBLISH_BRANCH}',
               '-f', f'sha={commit.stdout.strip()}'])
    if ref.returncode != 0:
        print(f'publish: ref update failed: {ref.stderr.strip()[:300]}',
              file=sys.stderr)
        return 1

    # Enable Pages on gh-pages if not already (idempotent; 409 = already on).
    pages_enabled = _gh(['api', f'repos/{PUBLISH_REPO}/pages'])
    if pages_enabled.returncode != 0:
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
    pages = render_pages(data, args.host)

    out_path = Path(args.out)
    if out_path.suffix == '.html':
        # legacy single-file invocation: write the landing page there
        out_path.write_text(pages['index.html'], encoding='utf-8')
        print(f'wrote {out_path.resolve()}')
    else:
        out_path.mkdir(parents=True, exist_ok=True)
        for fname, html in pages.items():
            (out_path / fname).write_text(html, encoding='utf-8')
        vendor_root = Path(__file__).resolve().parent.parent / 'assets' / 'vendor'
        for name in ('d3.min.js', 'd3-dag.iife.min.js', 'lineage-renderer.js'):
            source = vendor_root / name
            if source.is_file():
                destination = out_path / 'assets' / 'vendor' / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
        print(f'wrote {len(pages)} pages to {out_path.resolve()}')

    if data.get('_error'):
        print(f'note: {data["_error"]}', file=sys.stderr)

    if args.open:
        webbrowser.open((out_path / 'index.html').resolve().as_uri())

    if args.publish:
        return publish_to_pages(pages)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
