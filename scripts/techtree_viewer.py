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

Now-panel health verdict (issue #128)
-------------------------------------
The Now panel opens with one deterministic verdict so the morning health check
is a glance, not an interpretation exercise. ``health_verdict`` derives it from
data the page already renders -- no new collection -- and the first matching
rule wins:

1. ``investigate`` -- the proposer reports ``llm_unavailable``.
2. ``investigate`` -- the newest HEALTH_FAILURE_STREAK_LENGTH (3) consecutive
   cycle outcomes are all ``failed`` or ``partial``.
3. ``degraded``    -- the newest source file is older than HEALTH_STALE_SECONDS
   (3600 s): the page itself is stale.
4. ``degraded``    -- the last integrated cycle is at least
   HEALTH_INTEGRATION_RECENCY_SECONDS (21600 s = 6 h) old: the loop is quiet.
5. ``healthy``     -- none of the above.

The last-integration timestamp comes from the current evolution-tree node when
there is one, and otherwise from the NEWEST ``evolution_tree`` row in the ledger
tail. That ordering matters: ``read_ledger_tail`` returns rows oldest-first over
a 5000-row window, so reading the first row reports an integration that is days
old and pins the verdict to ``degraded`` forever.
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
from urllib.parse import quote

try:
    from scripts.agent_context import (
        AGENT_CONTEXT_CSS,
        build_two_tier_context_html,
        read_agent_context_dict,
    )
except ImportError:
    from agent_context import (
        AGENT_CONTEXT_CSS,
        build_two_tier_context_html,
        read_agent_context_dict,
    )

MSK_TZ = timezone(timedelta(hours=3))

SSH_USER = 'ozand'
REMOTE_SUDO_USER = 'eeepc-agent'
SSH_TIMEOUT_SECONDS = 45

# Now-panel health verdict thresholds. The full rule, including precedence,
# lives in the module docstring above.
HEALTH_STALE_SECONDS = 3600
HEALTH_INTEGRATION_RECENCY_SECONDS = 6 * 3600
HEALTH_FAILURE_STREAK_LENGTH = 3


def health_verdict(
    age_seconds: float | None,
    last_integrated_ts: str | None,
    recent_outcomes: list[str],
    proposer_llm_unavailable: bool,
    now: str,
    bridge_exit_streak: dict[str, Any] | None = None,
    scorecard: dict[str, Any] | None = None,
) -> tuple[str, str]:
    if not recent_outcomes and not last_integrated_ts:
        return 'investigate', 'no cycle history available'
    if proposer_llm_unavailable:
        return 'investigate', 'proposer LLM is unavailable'
    if isinstance(bridge_exit_streak, dict):
        consecutive_failures = bridge_exit_streak.get('consecutive_failures')
        if isinstance(consecutive_failures, int) and consecutive_failures >= HEALTH_FAILURE_STREAK_LENGTH:
            err = bridge_exit_streak.get('last_error') or ''
            where = bridge_exit_streak.get('last_where') or ''
            detail = f': {err}' if err else ''
            if where:
                detail += f' at {where}'
            return 'investigate', f'bridge crash loop: {consecutive_failures} consecutive invocation failures{detail}'
    streak = 0
    for outcome in reversed(recent_outcomes):
        if outcome in {'failed', 'partial'}:
            streak += 1
        else:
            break
    if streak >= HEALTH_FAILURE_STREAK_LENGTH:
        return 'investigate', f'{streak} consecutive failed or partial cycles'
    if isinstance(age_seconds, (int, float)) and age_seconds > HEALTH_STALE_SECONDS:
        return 'degraded', f'data is {humanize_age(age_seconds)} old'
    if last_integrated_ts:
        try:
            now_dt = datetime.fromisoformat(now.replace('Z', '+00:00'))
            integrated_dt = datetime.fromisoformat(last_integrated_ts.replace('Z', '+00:00'))
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
            if integrated_dt.tzinfo is None:
                integrated_dt = integrated_dt.replace(tzinfo=timezone.utc)
            age = (now_dt - integrated_dt).total_seconds()
            if age >= HEALTH_INTEGRATION_RECENCY_SECONDS:
                return 'degraded', f'last integrated cycle is {humanize_age(age)} old'
        except ValueError:
            pass

    # Issue #196: make the verdict scope explicit rather than claiming the whole system is healthy.
    scope_feeds: list[str] = []
    if isinstance(scorecard, dict):
        feeds_dict = scorecard.get('feeds')
        if isinstance(feeds_dict, dict) and isinstance(feeds_dict.get('feeds'), dict):
            scope_feeds = sorted(feeds_dict['feeds'].keys())
        elif isinstance(scorecard.get('reader_status'), dict) and isinstance(scorecard['reader_status'].get('feeds'), dict):
            scope_feeds = sorted(scorecard['reader_status']['feeds'].keys())

    if scope_feeds:
        feed_summary = f'{len(scope_feeds)} monitored feeds ({", ".join(scope_feeds)})'
    else:
        feed_summary = 'monitored feeds'

    return 'healthy', f'all signals within thresholds across {feed_summary}'

# Authority-host state root. Shared by both read paths: fetch_remote_state
# (below) uses it only as the default embedded in REMOTE_READER_SCRIPT, and
# read_local_state (issue #27) takes it as a real default argument so the
# viewer can run *on* eeepc without SSHing to itself.
STATE_ROOT = '/var/lib/eeepc-agent/self-evolving-agent/state'
INSTANCE_REPO = '/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving'

# Ledger-tail filter, mirrored inside REMOTE_READER_SCRIPT's own copy of
# these constants.
LEDGER_PHASES: set[str] = {
    'started', 'proposed', 'outcome', 'gate', 'proposer_reject', 'dedup', 'idle',
    'evolution_tree', 'tech_tree', 'hypothesis', 'doc_only_budget',
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
from datetime import datetime, timezone, timedelta
import gzip
import json
import os
import subprocess
import time

STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"
INSTANCE_REPO = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"
LEDGER_PHASES = {
    "started", "proposed", "outcome", "gate", "proposer_reject", "dedup", "idle",
    "evolution_tree", "tech_tree", "hypothesis", "doc_only_budget",
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


def classify_model(model_str):
    s = str(model_str or "").strip()
    if s.startswith("openai/"):
        s = s[7:]
    if s.startswith("un/"):
        return "self_hosted"
    if s.startswith(("cl/", "an/")):
        return "vendor"
    return "other"


def compute_quantiles(values):
    pos = sorted(int(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)
    if not pos:
        return [1000, 5000, 20000, 100000]
    n = len(pos)
    return [
        pos[int(n * 0.25)],
        pos[int(n * 0.50)],
        pos[int(n * 0.75)],
        pos[min(int(n * 0.95), n - 1)],
    ]


def read_token_heatmap(max_days=62):
    ldir = os.path.join(STATE_ROOT, "llm_calls")
    if not os.path.isdir(ldir):
        return None
    try:
        names = [n for n in os.listdir(ldir) if n.endswith(".jsonl") and len(n) == 16 and n[:10].replace("-", "").isdigit()]
    except Exception:
        return None
    if not names:
        return None
    names.sort()
    active_names = names[-(max_days + 2):]

    msk_raw_hourly = {}
    msk_raw_5min = {}
    msk_records_dates = set()

    for n in active_names:
        fpath = os.path.join(ldir, n)
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    ts_raw = row.get("ts")
                    if not ts_raw:
                        continue
                    s = str(ts_raw).strip()
                    if len(s) < 16 or s[10] != "T":
                        continue
                    if s.endswith("Z") or s.endswith("z"):
                        s = s[:-1] + "+00:00"
                    try:
                        dt = datetime.fromisoformat(s)
                    except Exception:
                        continue
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt_msk = dt.astimezone(timezone(timedelta(hours=3)))
                    d_msk = dt_msk.date().isoformat()
                    h_msk = dt_msk.hour
                    m_msk = dt_msk.minute
                    b_idx = h_msk * 12 + (m_msk // 5)

                    tok = row.get("total_tokens")
                    if not isinstance(tok, (int, float)) or isinstance(tok, bool) or tok < 0:
                        tok = (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)
                    tok = int(tok) if isinstance(tok, (int, float)) and not isinstance(tok, bool) else 0
                    cat = classify_model(row.get("model"))
                    comp = str(row.get("component") or "unknown").strip().lower()

                    msk_records_dates.add(d_msk)

                    if d_msk not in msk_raw_hourly:
                        msk_raw_hourly[d_msk] = [[0, 0, 0, 0, 0, {}] for _ in range(24)]
                        msk_raw_5min[d_msk] = {}

                    h_cell = msk_raw_hourly[d_msk][h_msk]
                    if cat == "self_hosted":
                        h_cell[0] += tok
                    elif cat == "vendor":
                        h_cell[1] += tok
                    else:
                        h_cell[2] += tok
                    h_cell[3] += tok
                    h_cell[4] += 1
                    h_cell[5][comp] = h_cell[5].get(comp, 0) + tok

                    b_dict = msk_raw_5min[d_msk]
                    if b_idx not in b_dict:
                        b_dict[b_idx] = [0, 0, 0, 0, 0, {}]
                    b_cell = b_dict[b_idx]
                    if cat == "self_hosted":
                        b_cell[0] += tok
                    elif cat == "vendor":
                        b_cell[1] += tok
                    else:
                        b_cell[2] += tok
                    b_cell[3] += tok
                    b_cell[4] += 1
                    b_cell[5][comp] = b_cell[5].get(comp, 0) + tok
        except Exception:
            continue

    utc_files_dates = {n[:10] for n in active_names}
    days_present_set = set(utc_files_dates) | msk_records_dates
    if not days_present_set:
        return None

    min_d_str = min(days_present_set)
    max_d_str = max(days_present_set)
    try:
        d0 = datetime.strptime(min_d_str, "%Y-%m-%d").date()
        d1 = datetime.strptime(max_d_str, "%Y-%m-%d").date()
    except Exception:
        return None
    if (d1 - d0).days > max_days - 1:
        d0 = d1 - timedelta(days=max_days - 1)
    calendar_dates = []
    curr = d0
    while curr <= d1:
        calendar_dates.append(curr.isoformat())
        curr += timedelta(days=1)

    hourly = {}
    five_min = {}
    tot_tok_all = 0
    tot_calls_all = 0
    loc_tok_all = 0
    gw_tok_all = 0
    oth_tok_all = 0
    days_present_count = 0

    all_h_gw = []
    all_h_loc = []
    all_h_tot = []
    all_5m_gw = []
    all_5m_loc = []
    all_5m_tot = []

    for d_str in calendar_dates:
        if d_str not in days_present_set:
            hourly[d_str] = None
            five_min[d_str] = None
            continue
        days_present_count += 1
        h_data = msk_raw_hourly.get(d_str)
        if h_data is None:
            h_data = [[0, 0, 0, 0, 0, {}] for _ in range(24)]
        b_data = msk_raw_5min.get(d_str, {})

        fin_h = []
        for h in range(24):
            c_dict = h_data[h][5]
            top_c = max(c_dict.items(), key=lambda x: x[1])[0] if c_dict else ""
            loc_t, gw_t, oth_t, tot_t, calls = h_data[h][0], h_data[h][1], h_data[h][2], h_data[h][3], h_data[h][4]
            fin_h.append([loc_t, gw_t, oth_t, tot_t, calls, top_c])
            tot_tok_all += tot_t
            tot_calls_all += calls
            loc_tok_all += loc_t
            gw_tok_all += gw_t
            oth_tok_all += oth_t
            if gw_t > 0:
                all_h_gw.append(gw_t)
            if loc_t > 0:
                all_h_loc.append(loc_t)
            if tot_t > 0:
                all_h_tot.append(tot_t)
        hourly[d_str] = fin_h

        fin_b = {}
        for b_idx, vals in b_data.items():
            c_dict = vals[5]
            top_c = max(c_dict.items(), key=lambda x: x[1])[0] if c_dict else ""
            loc_t, gw_t, oth_t, tot_t, calls = vals[0], vals[1], vals[2], vals[3], vals[4]
            fin_b[str(b_idx)] = [loc_t, gw_t, oth_t, tot_t, calls, top_c]
            if gw_t > 0:
                all_5m_gw.append(gw_t)
            if loc_t > 0:
                all_5m_loc.append(loc_t)
            if tot_t > 0:
                all_5m_tot.append(tot_t)
        five_min[d_str] = fin_b

    return {
        "dates": calendar_dates,
        "hourly": hourly,
        "five_min": five_min,
        "summary": {
            "total_tokens": tot_tok_all,
            "total_calls": tot_calls_all,
            "self_hosted_tokens": loc_tok_all,
            "vendor_tokens": gw_tok_all,
            "other_tokens": oth_tok_all,
            "local_tokens": loc_tok_all,
            "gateway_tokens": gw_tok_all,
            "days_span": len(calendar_dates),
            "days_present": days_present_count,
            "days_missing": len(calendar_dates) - days_present_count,
            "timezone": "MSK",
            "timezone_offset_hours": 3,
            "source_timezone": "UTC",
            "quantiles_hourly": {
                "gateway": compute_quantiles(all_h_gw),
                "local": compute_quantiles(all_h_loc),
                "total": compute_quantiles(all_h_tot),
            },
            "quantiles_5min": {
                "gateway": compute_quantiles(all_5m_gw),
                "local": compute_quantiles(all_5m_loc),
                "total": compute_quantiles(all_5m_tot),
            },
        },
    }


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
    rows_all = []
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
            rows_all.append({
                "id": lid,
                "date": str(row.get('date') or ''),
                "cycle_id": str(row.get('cycle_id') or ''),
                "task_id": str(row.get('task_id') or ''),
                "hypothesis": str(row.get('hypothesis') or ''),
                "result": str(row.get('result') or ''),
                "insight": str(row.get('generalized_insight') or ''),
                "problem": str(row.get('problem') or ''),
                "solution": str(row.get('solution') or ''),
            })
    return sorted(rows_all, key=lambda r: (r.get('date') or '', r.get('id') or ''), reverse=True)


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
    "token_heatmap": read_token_heatmap(),
    "lessons": read_lessons(),
    "reflections": read_jsonl("reflector/reflections.jsonl"),
    "ledger_history": read_ledger_history(),
    "bridge_exit_streak": read_json("bridge/exit_streak.json"),
    "bridge_exits": read_jsonl("bridge/exits.jsonl"),
    "strategist_decisions": read_jsonl("strategist/decisions.jsonl"),
    "demand_futility": read_json("demand/futility.json"),
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
        'token_heatmap': None,
        'reflections': [],
        'bridge_exit_streak': None,
        'bridge_exits': None,
        'strategist_decisions': None,
        'demand_futility': None,
        'goal_text': None,
        'agents_md': None,
        'cycle_titles': None,
        'cycle_files': {},
        'cycle_titles_error': None,
        'agent_context': None,
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
        'bridge_exit_streak': None,
        'bridge_exits': None,
        'strategist_decisions': None,
        'demand_futility': None,
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

    def read_token_heatmap_local() -> dict[str, Any] | None:
        """Issue #223: local mirror of read_token_heatmap()."""
        res = read_token_heatmap(root)
        if res and res.get('dates'):
            llm_dir = root / 'llm_calls' if (root / 'llm_calls').is_dir() else (root / 'state' / 'llm_calls')
            try:
                for f in llm_dir.glob('*.jsonl'):
                    mtimes.append(f.stat().st_mtime)
            except Exception:
                pass
        return res

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
        entries: list[dict[str, Any]] = []

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
                    entries.append({
                        'id': str(item.get('id') or ''),
                        'date': str(item.get('date') or ''),
                        'cycle_id': str(item.get('cycle_id') or ''),
                        'task_id': str(item.get('task_id') or ''),
                        'hypothesis': str(item.get('hypothesis') or ''),
                        'result': str(item.get('result') or ''),
                        'insight': str(item.get('generalized_insight') or ''),
                        'problem': str(item.get('problem') or ''),
                        'solution': str(item.get('solution') or ''),
                    })

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
        return sorted(entries, key=lambda e: (e.get('date') or '', e.get('id') or ''), reverse=True)

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
        'token_heatmap': read_token_heatmap_local(),
        'lessons': read_lessons_local(),
        'reflections': read_jsonl('reflector/reflections.jsonl'),
        'bridge_exit_streak': read_json('bridge/exit_streak.json'),
        'bridge_exits': read_jsonl('bridge/exits.jsonl'),
        'strategist_decisions': read_jsonl('strategist/decisions.jsonl'),
        'demand_futility': read_json('demand/futility.json'),
        'goal_text': read_json('goals/goal_text.json'),
        'agents_md': agents_text,
        'cycle_titles': titles,
        'cycle_files': cycle_files,
        'cycle_titles_error': titles_error,
        'agent_context': read_agent_context_dict(root, instance_repo),
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
    dt = _parse_iso_ts(ts)
    if dt is not None:
        return esc(dt.astimezone(MSK_TZ).strftime('%Y-%m-%d %H:%M:%S MSK'))
    return esc(str(ts))


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
    """Format ISO timestamp into short glanceable string (HH:MM MSK if today MSK, else Mon DD MSK or Mon DD YYYY MSK)."""
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

    dt_msk = dt.astimezone(MSK_TZ)
    now_msk = now.astimezone(MSK_TZ)

    if dt_msk.date() == now_msk.date():
        return f'{dt_msk.strftime("%H:%M")} MSK'
    elif dt_msk.year == now_msk.year:
        return f'{dt_msk.strftime("%b %d")} MSK'.replace(' 0', ' ')
    else:
        return f'{dt_msk.strftime("%b %d %Y")} MSK'.replace(' 0', ' ')



def classify_model(model_str: str) -> str:
    """Issue #223: Classify LLM model string into:
    - 'self_hosted': un/* (Qwen on local LAN GPU 3090Ti)
    - 'vendor': cl/*, an/* (Cloud vendor APIs via LiteLLM)
    - 'other': unclassified / unknown model prefix
    """
    s = str(model_str or '').strip()
    if s.startswith('openai/'):
        s = s[7:]
    if s.startswith('un/'):
        return 'self_hosted'
    if s.startswith(('cl/', 'an/')):
        return 'vendor'
    return 'other'


def compute_quantiles(values: list[int | float]) -> list[int]:
    """Issue #223: Compute [p25, p50, p75, p95] thresholds for positive numbers."""
    pos = sorted(int(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)
    if not pos:
        return [1000, 5000, 20000, 100000]
    n = len(pos)
    return [
        pos[int(n * 0.25)],
        pos[int(n * 0.50)],
        pos[int(n * 0.75)],
        pos[min(int(n * 0.95), n - 1)],
    ]


def fmt_tokens(n: int | float | None) -> str:
    """Issue #223: format token counts compactly with B / M / K suffixes."""
    if n is None:
        return 'n/a'
    try:
        val = float(n)
    except (TypeError, ValueError):
        return 'n/a'
    if val >= 1_000_000_000:
        return f"{val / 1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}K"
    return str(int(val))


def read_token_heatmap(root: Path | str, max_days: int = 62) -> dict[str, Any] | None:
    """Issue #223, #225: aggregate hourly and 5-minute token consumption from llm_calls/*.jsonl.
    Source records store timestamps in UTC. Display buckets are aggregated into Moscow Time
    (MSK, UTC+3) so that each day row represents 00:00:00 to 23:59:59 MSK of that date,
    and 5-minute intervals cover the 288 slices of the MSK day.
    Distinguishes NO DATA (missing/unobserved day/hour) from ZERO TOKENS (quiet hour).
    Separates self-hosted (un/*, qwen) from vendor gateway (cl/*, an/*) and other models.
    """
    p = Path(root)
    llm_dir = p / 'llm_calls' if (p / 'llm_calls').is_dir() else (p / 'state' / 'llm_calls')
    if not llm_dir.is_dir():
        return None

    try:
        daily_files = [
            f for f in llm_dir.iterdir()
            if f.is_file() and f.name.endswith('.jsonl') and len(f.name) == 16 and f.name[:10].replace('-', '').isdigit()
        ]
    except Exception:
        return None

    if not daily_files:
        return None

    daily_files.sort(key=lambda f: f.name[:10])
    active_files = daily_files[-(max_days + 2):]

    msk_raw_hourly: dict[str, list[list[Any]]] = {}
    msk_raw_5min: dict[str, dict[int, list[Any]]] = {}
    msk_records_dates: set[str] = set()

    for file_path in active_files:
        try:
            with file_path.open('r', encoding='utf-8', errors='replace') as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue

                    dt = _parse_iso_ts(row.get('ts'))
                    if dt is None:
                        continue

                    dt_msk = dt.astimezone(MSK_TZ)
                    d_msk = dt_msk.date().isoformat()
                    h_msk = dt_msk.hour
                    m_msk = dt_msk.minute
                    b_idx = h_msk * 12 + (m_msk // 5)

                    tok = row.get('total_tokens')
                    if not isinstance(tok, (int, float)) or isinstance(tok, bool) or tok < 0:
                        tok = (row.get('prompt_tokens') or 0) + (row.get('completion_tokens') or 0)
                    tok = int(tok) if isinstance(tok, (int, float)) and not isinstance(tok, bool) else 0

                    cat = classify_model(str(row.get('model') or ''))
                    comp = str(row.get('component') or 'unknown').strip().lower()

                    msk_records_dates.add(d_msk)

                    if d_msk not in msk_raw_hourly:
                        msk_raw_hourly[d_msk] = [[0, 0, 0, 0, 0, {}] for _ in range(24)]
                        msk_raw_5min[d_msk] = {}

                    h_cell = msk_raw_hourly[d_msk][h_msk]
                    if cat == 'self_hosted':
                        h_cell[0] += tok
                    elif cat == 'vendor':
                        h_cell[1] += tok
                    else:
                        h_cell[2] += tok
                    h_cell[3] += tok
                    h_cell[4] += 1
                    h_cell[5][comp] = h_cell[5].get(comp, 0) + tok

                    b_dict = msk_raw_5min[d_msk]
                    if b_idx not in b_dict:
                        b_dict[b_idx] = [0, 0, 0, 0, 0, {}]
                    b_cell = b_dict[b_idx]
                    if cat == 'self_hosted':
                        b_cell[0] += tok
                    elif cat == 'vendor':
                        b_cell[1] += tok
                    else:
                        b_cell[2] += tok
                    b_cell[3] += tok
                    b_cell[4] += 1
                    b_cell[5][comp] = b_cell[5].get(comp, 0) + tok
        except Exception:
            continue

    utc_files_dates = {f.name[:10] for f in active_files}
    days_present_set = set(utc_files_dates) | msk_records_dates
    if not days_present_set:
        return None

    min_d_str = min(days_present_set)
    max_d_str = max(days_present_set)
    try:
        d0 = datetime.strptime(min_d_str, "%Y-%m-%d").date()
        d1 = datetime.strptime(max_d_str, "%Y-%m-%d").date()
    except Exception:
        return None

    if (d1 - d0).days > max_days - 1:
        d0 = d1 - timedelta(days=max_days - 1)

    calendar_dates: list[str] = []
    curr = d0
    while curr <= d1:
        calendar_dates.append(curr.isoformat())
        curr += timedelta(days=1)

    hourly: dict[str, list[list[Any]] | None] = {}
    five_min: dict[str, dict[str, list[Any]] | None] = {}
    total_tokens_all = 0
    total_calls_all = 0
    local_tokens_all = 0
    gateway_tokens_all = 0
    other_tokens_all = 0
    days_present_count = 0

    all_hourly_gateway_vals: list[int] = []
    all_hourly_local_vals: list[int] = []
    all_hourly_total_vals: list[int] = []

    all_5min_gateway_vals: list[int] = []
    all_5min_local_vals: list[int] = []
    all_5min_total_vals: list[int] = []

    for d_str in calendar_dates:
        if d_str not in days_present_set:
            hourly[d_str] = None
            five_min[d_str] = None
            continue

        days_present_count += 1
        h_data = msk_raw_hourly.get(d_str)
        if h_data is None:
            h_data = [[0, 0, 0, 0, 0, {}] for _ in range(24)]

        b_data = msk_raw_5min.get(d_str, {})

        fin_h: list[list[Any]] = []
        for h in range(24):
            c_dict = h_data[h][5]
            top_c = max(c_dict.items(), key=lambda x: x[1])[0] if c_dict else ''
            loc_t, gw_t, oth_t, tot_t, calls = h_data[h][0], h_data[h][1], h_data[h][2], h_data[h][3], h_data[h][4]
            fin_h.append([loc_t, gw_t, oth_t, tot_t, calls, top_c])
            total_tokens_all += tot_t
            total_calls_all += calls
            local_tokens_all += loc_t
            gateway_tokens_all += gw_t
            other_tokens_all += oth_t
            if gw_t > 0:
                all_hourly_gateway_vals.append(gw_t)
            if loc_t > 0:
                all_hourly_local_vals.append(loc_t)
            if tot_t > 0:
                all_hourly_total_vals.append(tot_t)
        hourly[d_str] = fin_h

        fin_b: dict[str, list[Any]] = {}
        for b_idx, vals in b_data.items():
            c_dict = vals[5]
            top_c = max(c_dict.items(), key=lambda x: x[1])[0] if c_dict else ''
            loc_t, gw_t, oth_t, tot_t, calls = vals[0], vals[1], vals[2], vals[3], vals[4]
            fin_b[str(b_idx)] = [loc_t, gw_t, oth_t, tot_t, calls, top_c]
            if gw_t > 0:
                all_5min_gateway_vals.append(gw_t)
            if loc_t > 0:
                all_5min_local_vals.append(loc_t)
            if tot_t > 0:
                all_5min_total_vals.append(tot_t)
        five_min[d_str] = fin_b

    return {
        'dates': calendar_dates,
        'hourly': hourly,
        'five_min': five_min,
        'summary': {
            'total_tokens': total_tokens_all,
            'total_calls': total_calls_all,
            'self_hosted_tokens': local_tokens_all,
            'vendor_tokens': gateway_tokens_all,
            'other_tokens': other_tokens_all,
            'local_tokens': local_tokens_all,
            'gateway_tokens': gateway_tokens_all,
            'days_span': len(calendar_dates),
            'days_present': days_present_count,
            'days_missing': len(calendar_dates) - days_present_count,
            'timezone': 'MSK',
            'timezone_offset_hours': 3,
            'source_timezone': 'UTC',
            'quantiles_hourly': {
                'gateway': compute_quantiles(all_hourly_gateway_vals),
                'local': compute_quantiles(all_hourly_local_vals),
                'total': compute_quantiles(all_hourly_total_vals),
            },
            'quantiles_5min': {
                'gateway': compute_quantiles(all_5min_gateway_vals),
                'local': compute_quantiles(all_5min_local_vals),
                'total': compute_quantiles(all_5min_total_vals),
            },
        },
    }


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
LINEAGE_MAX_NODES = 1500  # #218: unified lineage payload budget
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


# #208 review: _ARCHIVE_RING and _ledger_outcome_kind (#71/#77) served only the
# deleted archive tree and are gone with it. Leaf outcomes on the day lineage
# are classified inline in _build_vertical_day_lineage: failed / partial /
# skipped. There is no `running` there — a cycle that has started and not
# finished is not a leaf and does not appear on lineage.html (it does on the
# cycle feed).


def _lineage_day(ts: Any) -> str:
    dt = _parse_iso_ts(ts)
    if dt is not None:
        return dt.astimezone(MSK_TZ).strftime('%Y-%m-%d')
    value = str(ts or '')
    return value[:10] if len(value) >= 10 else ''


def _load_lineage_vendor_scripts() -> dict[str, str] | None:
    vendor_root = Path(__file__).resolve().parent.parent / 'assets' / 'vendor'
    # #208: d3 + d3-dag (390 KB inlined per page load) are gone; the renderer
    # lays the forest out itself and needs no library.
    names = ('lineage-renderer.js',)
    try:
        scripts = {name: (vendor_root / name).read_text(encoding='utf-8') for name in names}
    except (OSError, UnicodeError):
        return None
    return {name: source.replace('</script', '<\\/script') for name, source in scripts.items()}


LINEAGE_DETAILS_FILE = 'lineage-cycle-details.json'  # #208: published beside lineage.html, fetched on demand


def _parse_lineage_ts(ts: Any) -> str | None:
    value = str(ts or '').strip()
    if not value:
        return None
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return value


def _node_id_for_commit(sha: str) -> str:
    return 'c:' + str(sha)


def _safe_node_dom_id(node_id: str) -> str:
    # Match JavaScript encodeURIComponent exactly: RFC-3986 unescaped marks are
    # retained, while Python's quote() otherwise leaves '/' unescaped by default.
    encoded = quote(str(node_id), safe="-_.!~*'()")
    return 'node-' + encoded


def _leaf_outcome(row: dict[str, Any]) -> str:
    if row.get('outcome') in {'failed', 'fail'} or row.get('status') in {'failed', 'fail'}:
        return 'failed'
    if row.get('outcome') == 'partial':
        return 'partial'
    return 'skipped'


def _lineage_parent(
    node: dict[str, Any],
    trunk: list[dict[str, Any]],
    graph_shas: set[str],
    all_nodes: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None, str | None]:
    """#208: THE parent expression, shared by the server SVG, ``nodes[]`` and ``edges[]``.

    Returns ``(parent_sha, basis, parent_day)``:

    - ``(sha, 'recorded', None)`` — the row's own ``parent_sha`` is a node of this day.
    - ``(None, None, 'YYYY-MM-DD')`` — the recorded parent exists but in an earlier
      day bucket; the client draws a cross-day stub, nothing is guessed.
    - ``(sha, 'inferred', None)`` — the row has no usable parent (a failed attempt,
      or a parent the ledger never saw): the latest trunk node at or before the
      row's timestamp. This is adjacency in time, not provenance, and it is
      rendered dashed everywhere for that reason.
    - ``(None, None, None)`` — a root.

    Until #208 ``nodes[].parent`` used the FIRST trunk node of the day and
    ``edges[]`` the LATEST, so the two disagreed on 49.6% of the live edges.
    """
    sha = node['sha']
    recorded = str(node.get('parent_sha') or '')
    if recorded and recorded != sha and recorded in graph_shas:
        return recorded, 'recorded', None
    if recorded and recorded in all_nodes:
        parent_day = _lineage_day(all_nodes[recorded]['ts'])
        if parent_day != _lineage_day(node['ts']):
            return None, None, parent_day
        # Same day but not drawn (truncated at LINEAGE_DAY_CAP): a stub would
        # point at today; fall through to the chronological guess instead.
    order = {item['sha']: index for index, item in enumerate(trunk)}
    if sha in order:
        # A trunk row: the previous trunk node in timestamp order. Strictly
        # earlier by POSITION, so two rows with an identical timestamp cannot
        # become each other's parent (review finding on PR #209).
        previous = trunk[order[sha] - 1] if order[sha] > 0 else None
    else:
        # A ledger-only leaf: the latest trunk node at or before its timestamp.
        previous = max(
            (item for item in trunk if item['ts'] <= node['ts']),
            key=lambda item: (item['ts'], order[item['sha']]), default=None,
        )
    if previous is None:
        return None, None, None
    return previous['sha'], 'inferred', None


def _build_vertical_day_lineage(
    ledger_rows: list[Any],
    fallback_tree: dict[str, Any] | None,
    task_titles: dict[str, str] | None,
    now: str | None,
    cycle_details: dict[str, dict[str, Any]] | None = None,
) -> str:
    vendor_scripts = _load_lineage_vendor_scripts()

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
             '<button type="button" data-lineage-filter="range">Apply</button>',
             # #208 step 7: filled by lineageDayFilter when the requested calendar window has no data.
             '<span class="lineage-filter-note" hidden></span></div>',
             '<div class="lineage-legend" aria-label="Lineage Legend">',
             '  <div class="lineage-legend-group"><span class="lineage-legend-title">Edges:</span>',
             '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="28" height="12"><line x1="0" y1="6" x2="28" y2="6" class="lineage-legend-edge lineage-legend-edge-recorded"/></svg> recorded</span>',
             '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="28" height="12"><line x1="0" y1="6" x2="28" y2="6" class="lineage-legend-edge lineage-legend-edge-inferred"/></svg> inferred</span></div>',
             '  <div class="lineage-legend-group"><span class="lineage-legend-title">Nodes:</span>',
             '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="14" height="14"><circle cx="7" cy="7" r="5" class="arch-node arch-integrated lineage-legend-node"/></svg> integrated</span>',
             '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="14" height="14"><circle cx="7" cy="7" r="5" class="arch-node arch-skipped lineage-legend-node"/></svg> skipped</span>',
             '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="14" height="14"><circle cx="7" cy="7" r="5" class="arch-node arch-partial lineage-legend-node"/></svg> partial</span>',
             '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="14" height="14"><circle cx="7" cy="7" r="5" class="arch-node arch-failed lineage-legend-node"/></svg> failed</span></div>',
             '  <div class="lineage-legend-group"><span class="lineage-legend-title">Current:</span>',
             '    <span class="lineage-legend-item"><span class="arch-star" style="font-size:14px;line-height:1;">&#9733;</span> current sha</span></div>',
             '</div>',
             '<div class="lineage-day-groups" data-lineage-default-mode="yesterday-today" data-lineage-default="' + ','.join(sorted(default_days)) + '">']
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
        parts.append('<noscript><p class="lineage-js-note">Enable JavaScript for the enhanced client-side lineage layout.</p></noscript>')
        if truncated:
            parts.append(f'<p class="lineage-day-truncated">truncated at {LINEAGE_DAY_CAP} nodes</p>')
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
        parts.append(f'<svg class="lineage-day-svg arch-tree" width="{width}" height="{height}" viewBox="0 0 {width} {height}" data-lineage-renderer="lineage-tree">')
        # #208: ONE parent expression (_lineage_parent) feeds the server SVG,
        # nodes[] and edges[]; each edge carries its basis, and the client
        # keeps that basis in the rendered element.
        graph_nodes = trunk + side[:LINEAGE_DAY_CAP]
        graph_shas = {node['sha'] for node in graph_nodes}
        resolved = {node['sha']: _lineage_parent(node, trunk, graph_shas, all_nodes) for node in graph_nodes}
        for node in graph_nodes:
            parent, basis, parent_day = resolved[node['sha']]
            x2, y2 = positions[node['sha']]
            if parent is not None and parent in positions:
                x1, y1 = positions[parent]
                if basis == 'recorded':
                    cls = 'lineage-edge arch-edge' + (' evo-elbow-best' if current_sha and node['sha'] == current_sha else '')
                    parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="{cls}" data-basis="recorded"/>')
                else:
                    parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="lineage-edge lineage-edge-chronological" stroke-dasharray="6 5" data-basis="inferred"/>')
            elif parent_day:
                label = datetime.strptime(parent_day, '%Y-%m-%d').strftime('%b %d')
                parts.append(f'<text class="lineage-hidden-parent" x="{x2}" y="{y2 - 14}" text-anchor="middle">&#8617; from {esc(label)}</text>')

        def _node_title(node: dict[str, Any]) -> str:
            # #208 step 1: cycle_details carries the real title for every
            # rendered node (832/832 on the live page); task_titles (git merge
            # subjects, 6.4% coverage) is the fallback, never the source.
            cid = node['cycle_id'] or node['sha']
            detail = (cycle_details or {}).get(cid)
            title = str(detail.get('title') or '') if isinstance(detail, dict) else ''
            if not title or title == '(untitled cycle)':
                title = str((task_titles or {}).get(cid) or (task_titles or {}).get(cid.replace('cycle-', '', 1)) or '')
            return title

        payload_nodes: list[dict[str, Any]] = []
        for node in graph_nodes:
            parent, basis, parent_day = resolved[node['sha']]
            entry: dict[str, Any] = {
                'sha': node['sha'],
                'cycle_id': node['cycle_id'],
                'parent': parent,
                'parent_basis': basis,
                'ts': node['ts'],
                'outcome': node.get('outcome', 'integrated'),
                'kind': 'trunk' if node in trunk else 'leaf',
            }
            if parent_day:
                entry['parent_day'] = parent_day
            title = _node_title(node)
            if title:
                entry['title'] = title
            if current_sha and node['sha'] == current_sha:
                entry['current'] = True
            payload_nodes.append(entry)
        day_payload = {
            'day': day,
            'current_sha': current_sha,
            'nodes': payload_nodes,
            'edges': [
                {'source': entry['parent'], 'target': entry['sha'], 'basis': entry['parent_basis']}
                for entry in payload_nodes if entry['parent']
            ],
        }
        day_json = json.dumps(day_payload, ensure_ascii=True, separators=(',', ':')).replace('<', '\\u003c')
        parts.append(f'<script type="application/json" class="lineage-day-data" data-day="{esc(day)}" hidden aria-hidden="true">{day_json}</script>')
        if current_sha in positions:
            x, y = positions[current_sha]
            parts.append(f'<text x="{x}" y="{y - 14}" text-anchor="middle" class="arch-star">&#9733;</text>')
        for node in graph_nodes:
            x, y = positions[node['sha']]
            cid = node['cycle_id'] or node['sha']
            kind = node.get('outcome', 'integrated')
            title = _node_title(node) or cid
            parts.append(f'<circle class="arch-node arch-{esc(kind)} lineage-node" data-cycle-id="{esc(cid)}" cx="{x}" cy="{y}" r="9"><title>{esc(title)}</title></circle>')
        parts.append('</svg></section>')
    parts.append('</div></div>')
    inline_scripts = ''
    if vendor_scripts is not None:
        inline_scripts = ''.join(f'<script>{vendor_scripts[name]}</script>' for name in ('lineage-renderer.js',))
    # #208 step 8: cycle_details (1.17 MB of the 2.04 MB live page) is no longer
    # inlined. render_pages() publishes it as LINEAGE_DETAILS_FILE beside this
    # page and the panel fetches it on the first click. Records are not
    # filtered: the 1,116 records without a node are the only place task
    # identity (title, serves, demand_id) is recorded. The day filter itself
    # lives in lineage-renderer.js (lineageDayFilter), not here.
    parts.append(f'''<section class="cycle-details-panel" id="cycle-details-panel" data-cycle-details-src="{LINEAGE_DETAILS_FILE}" hidden aria-label="Cycle details"><h2 class="cycle-details-title">Cycle details <button class="cycle-details-close" id="cycle-details-close" aria-label="Close cycle details">&times;</button></h2><div class="cycle-details-body"></div></section>{inline_scripts}<script>
(function () {{
  var panel = document.getElementById('cycle-details-panel'), src = panel.getAttribute('data-cycle-details-src'), data = null, loading = null;
  function esc(v) {{ var d = document.createElement('div'); d.textContent = v == null ? '' : String(v); return d.innerHTML; }}
  function line(label, value) {{ return value ? '<p><b>' + label + ':</b> ' + esc(value) + '</p>' : ''; }}
  function list(label, values) {{ if (!Array.isArray(values)) values = values ? [values] : []; return values.length ? '<h3>' + label + '</h3><ul>' + values.map(function (v) {{ return '<li>' + esc(v) + '</li>'; }}).join('') + '</ul>' : ''; }}
  // Default cache mode on purpose: the file is republished every cycle and 'force-cache' would pin the first copy for good.
  // A failed fetch clears `loading`, so the next click retries instead of replaying the rejection.
  function load() {{ if (data) return Promise.resolve(data); if (!loading) loading = fetch(src).then(function (r) {{ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }}).then(function (json) {{ data = json; return data; }}).catch(function (err) {{ loading = null; throw err; }}); return loading; }}
  function render(cid) {{
    var item = (data && data[cid]) || {{cycle_id: cid}};
    var html = '<h3>' + esc(item.title || cid) + '</h3>' + line('Cycle', item.cycle_id) + line('Outcome', item.outcome) + line('Reason', item.reason) + line('Timestamp', item.ts) + line('SHA', item.sha) + line('Parent SHA', item.parent_sha) + line('Target path', item.target_path) + line('Serves / demand', item.serves || item.demand_id) + list('Files changed', item.files_changed) + list('Gate violations', item.gate_violations);
    if (item.lesson_problem || item.lesson_solution) html += '<h3>Lesson</h3>' + line('Problem', item.lesson_problem) + line('Solution', item.lesson_solution);
    else if (item.lesson_insight) html += '<h3>Lesson insight</h3><p>' + esc(item.lesson_insight) + '</p>';
    if (item.reflection) html += '<h3>Reflector</h3>' + line('Summary', item.reflection.summary) + list('Findings', item.reflection.findings) + list('Recommendations', item.reflection.recommendations);
    html += '<p class="cycle-details-links"><a class="cycle-feed-link" href="cycles.html#cycle-' + encodeURIComponent(cid) + '">open in Cycle Feed</a> · <a href="lessons.html#q-' + encodeURIComponent(cid) + '">related lessons</a></p>';
    panel.querySelector('.cycle-details-body').innerHTML = html;
  }}
  // #213: selection state and focus return target.
  var selectedNode = null;
  var openedByNode = null;  // node to return focus to on close
  var openSeq = 0;          // incremented on every open(); stale async callbacks bail out
  function clearSelection() {{
    if (selectedNode) {{ selectedNode.classList.remove('cycle-node-selected'); selectedNode = null; }}
  }}
  function closePanel() {{
    panel.hidden = true;
    clearSelection();
    var returnTo = openedByNode;
    openedByNode = null;
    // Return focus to the node. SVG circles accept tabindex but Chromium
    // does not move activeElement to them via .focus(); blur the close button
    // explicitly so focus leaves the panel area regardless.
    var closeBtn = document.getElementById('cycle-details-close');
    if (closeBtn && document.activeElement === closeBtn) closeBtn.blur();
    if (returnTo && returnTo.focus) returnTo.focus({{ preventScroll: true }});
  }}
  // scrollPanelIntoView: single scroll owner, called after content is set.
  // Uses instant for the first 16ms so layout is stable; avoids competing
  // with the browser's native fragment-scroll on deep-link loads.
  function scrollPanelIntoView() {{
    panel.scrollIntoView({{ behavior: 'instant', block: 'nearest' }});
  }}
  function open(node, fromHash) {{
    clearSelection();
    selectedNode = node;
    openedByNode = fromHash ? null : node;  // no return-focus for hash-nav
    node.classList.add('cycle-node-selected');
    var cid = node.getAttribute('data-cycle-id');
    var seq = ++openSeq;
    // #213: update location.hash so the current card is bookmarkable.
    if (!fromHash && history.replaceState) {{
      history.replaceState(null, '', '#node-' + encodeURIComponent(cid));
    }}
    panel.hidden = false;
    panel.querySelector('.cycle-details-body').innerHTML = '<p>loading ' + esc(cid) + ' …</p>';
    // Move focus to the close button so Tab cycles through card content,
    // not back to the SVG tree. preventScroll: scrollPanelIntoView owns positioning.
    var closeBtn = document.getElementById('cycle-details-close');
    if (closeBtn) closeBtn.focus({{ preventScroll: true }});
    load()
      .then(function () {{
        if (seq !== openSeq) return;  // superseded by a newer click
        render(cid);
        scrollPanelIntoView();  // scroll AFTER content is rendered and height is final
      }})
      .catch(function (err) {{
        if (seq !== openSeq) return;
        panel.querySelector('.cycle-details-body').innerHTML =
          '<p>' + esc(cid) + ': details unavailable — ' + esc(src) +
          ' could not be loaded or rendered (' + esc(err && err.message || err) + ').</p>';
        scrollPanelIntoView();
      }});
  }}
  // #213: node click handler.
  document.addEventListener('click', function (event) {{
    var node = event.target.closest('.lineage-node');
    if (node) {{ event.preventDefault(); open(node, false); return; }}
    // Close button inside panel.
    if (event.target.closest('#cycle-details-close')) {{ closePanel(); }}
  }});
  // #213: Escape key closes the panel and clears selection.
  document.addEventListener('keydown', function (event) {{
    if (event.key === 'Escape' && !panel.hidden) {{ event.preventDefault(); closePanel(); }}
  }});
  // #213: deep-link — #node-<cycle_id> makes the target node and its context visible.
  // Model-neutral: locates by id; does not assume day-section structure.
  // Single scroll owner: node.scrollIntoView() positions the tree; panel
  // is scrolled inside open() AFTER content loads. No competing scrolls.
  function handleHash() {{
    var hash = window.location.hash;
    if (!hash || !hash.startsWith('#node-')) return;
    var cid = decodeURIComponent(hash.slice(6));
    var el = document.getElementById('node-' + cid);
    if (!el) return;
    // Unhide the containing day-group if the day filter hid it.
    var dayGroup = el.closest('.lineage-day-group');
    if (dayGroup && dayGroup.hidden) {{ dayGroup.hidden = false; }}
    // Scroll node into view first (instant: beat the browser's native fragment jump).
    el.scrollIntoView({{ behavior: 'instant', block: 'center' }});
    open(el, true);  // fromHash=true: panel scrolls after content loads
  }}
  // Run after renderer has had a chance to draw nodes.
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', handleHash);
  }} else {{
    // Renderer runs synchronously on DOMContentLoaded; by the time this inline
    // script body runs the DOM is ready but renderer may not yet have fired.
    // Use a microtask to let renderer finish first.
    Promise.resolve().then(handleHash);
  }}
  window.addEventListener('hashchange', handleHash);
}})();
</script>''')
    return '<div class="canvas-outer" id="panel-lineage">' + ''.join(parts) + '</div>'


def _lineage_coverage_text(coverage: dict[str, Any]) -> str:
    unique = int(coverage.get('unique_candidate_nodes') or 0)
    emitted = int(coverage.get('emitted_nodes') or 0)
    excluded = int(coverage.get('excluded_nodes') or max(0, unique - emitted))
    limit = coverage.get('retention_limit')
    span_from = str(coverage.get('from_ts') or '')
    span_to = str(coverage.get('to_ts') or '')
    parts = [f'Available graph: {emitted} of {unique} candidate nodes emitted']
    if limit:
        parts.append(f'retention limit {int(limit)}')
    if excluded:
        parts.append(f'{excluded} omitted by retention')
    if span_from or span_to:
        parts.append(f'emitted timestamp span {span_from or "?"} to {span_to or "?"}')
    parts.append('older or missing ancestry may be unavailable')
    return '; '.join(parts) + '.'


def _build_unified_lineage(
    ledger_rows: list[Any],
    fallback_tree: dict[str, Any] | None,
    task_titles: dict[str, str] | None,
    now: str | None,
    cycle_details: dict[str, dict[str, Any]] | None = None,
) -> str:
    """#218: one canonical lineage graph; date controls are projections."""
    vendor_scripts = _load_lineage_vendor_scripts()
    raw_read_rows = sum(1 for row in ledger_rows if isinstance(row, dict))
    commits: dict[str, dict[str, Any]] = {}
    leaves: dict[str, dict[str, Any]] = {}

    def ts_key(value: Any) -> tuple[int, str]:
        parsed = _parse_lineage_ts(value)
        return (1, parsed) if parsed is not None else (0, '')

    def keep_newer(existing: dict[str, Any] | None, row: dict[str, Any]) -> dict[str, Any]:
        if existing is None:
            return row
        return row if (ts_key(row.get('ts')), str(row.get('cycle_id') or ''), str(row.get('sha') or '')) >= (ts_key(existing.get('ts')), str(existing.get('cycle_id') or ''), str(existing.get('sha') or '')) else existing

    for row in ledger_rows:
        if not isinstance(row, dict):
            continue
        phase = row.get('phase')
        sha = str(row.get('sha') or '')
        cid = str(row.get('cycle_id') or '')
        if phase == 'evolution_tree' and sha:
            commits[sha] = keep_newer(commits.get(sha), row)
        elif cid and phase in {'outcome', 'gate', 'proposer_reject', 'dedup'} and not sha:
            outcome = str(row.get('outcome') or row.get('status') or '').lower()
            if outcome not in {'integrated', 'success', 'succeeded', 'ok', 'pass', 'passed'}:
                leaves[cid] = keep_newer(leaves.get(cid), row)
    if not commits and isinstance(fallback_tree, dict) and isinstance(fallback_tree.get('nodes'), dict):
        for sha, node in fallback_tree['nodes'].items():
            if isinstance(node, dict):
                row = dict(node)
                row['sha'] = str(sha)
                row.setdefault('cycle_id', str(node.get('cycle_id') or sha))
                commits[str(sha)] = row

    candidates: list[dict[str, Any]] = []
    for sha, row in commits.items():
        cid = str(row.get('cycle_id') or sha)
        detail = (cycle_details or {}).get(cid) if cycle_details else None
        title = str(detail.get('title') or '') if isinstance(detail, dict) else ''
        if not title or title == '(untitled cycle)':
            title = str((task_titles or {}).get(cid) or (task_titles or {}).get(cid.replace('cycle-', '', 1)) or '')
        parsed_ts = _parse_lineage_ts(row.get('ts'))
        candidates.append({'node_id': _node_id_for_commit(sha), 'sha': sha, 'cycle_id': cid, 'parent_sha': str(row.get('parent_sha') or ''), 'parent': None, 'parent_basis': None, 'parent_known': True, 'parent_status': 'root', 'ts': parsed_ts, 'ts_status': 'valid' if parsed_ts else 'invalid', 'outcome': str(row.get('outcome') or 'integrated'), 'kind': 'trunk', 'title': title})
    for cid, row in leaves.items():
        detail = (cycle_details or {}).get(cid) if cycle_details else None
        title = str(detail.get('title') or '') if isinstance(detail, dict) else ''
        if not title or title == '(untitled cycle)':
            title = str((task_titles or {}).get(cid) or (task_titles or {}).get(cid.replace('cycle-', '', 1)) or '')
        parsed_ts = _parse_lineage_ts(row.get('ts'))
        candidates.append({'node_id': 'a:' + cid, 'sha': None, 'cycle_id': cid, 'parent_sha': str(row.get('parent_sha') or ''), 'parent': None, 'parent_basis': None, 'parent_known': True, 'parent_status': 'root', 'ts': parsed_ts, 'ts_status': 'valid' if parsed_ts else 'invalid', 'outcome': _leaf_outcome(row), 'kind': 'leaf', 'title': title})

    commit_by_sha = {str(node['sha']): node for node in candidates if node.get('sha')}
    trunk_by_time = sorted((node for node in candidates if node['kind'] == 'trunk'), key=lambda node: (node.get('ts') or '', node['node_id']))
    for node in sorted(candidates, key=lambda item: (item.get('ts') or '', item['node_id'])):
        recorded = str(node.get('parent_sha') or '')
        if recorded and recorded != node.get('sha'):
            parent = commit_by_sha.get(recorded)
            if parent:
                node['parent'] = parent['node_id']
                node['parent_basis'] = 'recorded'
                node['parent_status'] = 'recorded'
            else:
                node['parent_known'] = False
                node['parent_status'] = 'recorded_unknown'
            continue
        if recorded and recorded == node.get('sha'):
            node['parent_known'] = False
            node['parent_status'] = 'cycle'
            continue
        if node['kind'] == 'leaf' and node.get('ts'):
            previous = max((item for item in trunk_by_time if item.get('ts') and item['ts'] <= node['ts']), key=lambda item: (item['ts'], item['node_id']), default=None)
            if previous:
                node['parent'] = previous['node_id']
                node['parent_basis'] = 'inferred'
                node['parent_status'] = 'inferred'

    unique_candidate_nodes = len(candidates)
    current_sha = str((fallback_tree or {}).get('current_sha') or '')
    current_node_id = _node_id_for_commit(current_sha) if current_sha else ''
    # A current SHA may be supplied by the fallback tree even when the ledger
    # window has no corresponding row. Materialise it as an explicit retained
    # boundary node so the anchor invariant is honest and testable.
    if current_node_id and current_node_id not in {node['node_id'] for node in candidates}:
        candidates.append({'node_id': current_node_id, 'sha': current_sha, 'cycle_id': current_sha, 'parent_sha': '', 'parent': None, 'parent_basis': None, 'parent_known': False, 'parent_status': 'current_unavailable', 'ts': None, 'ts_status': 'invalid', 'outcome': 'unknown', 'kind': 'current-boundary', 'title': current_sha, 'current': True, 'boundary': 'fallback_current_unavailable'})
        unique_candidate_nodes = len(candidates)
    retained = sorted(candidates, key=lambda node: (1 if node.get('ts') else 0, node.get('ts') or '', node['node_id']))[-LINEAGE_MAX_NODES:]
    retained_ids = {node['node_id'] for node in retained}
    if current_node_id and current_node_id not in retained_ids:
        current_node = next((node for node in candidates if node['node_id'] == current_node_id), None)
        if current_node is not None:
            if len(retained) >= LINEAGE_MAX_NODES and retained:
                retained.pop(0)
            retained.append(current_node)
    retained = sorted(retained, key=lambda node: (1 if node.get('ts') else 0, node.get('ts') or '', node['node_id']))
    retained_ids = {node['node_id'] for node in retained}
    truncated = unique_candidate_nodes > len(retained)
    truncated_before_ts = next((node.get('ts') for node in retained if node.get('ts')), None) if truncated else None

    payload_nodes: list[dict[str, Any]] = []
    payload_edges: list[dict[str, Any]] = []
    for node in retained:
        entry = dict(node)
        if node['node_id'] == current_node_id:
            entry['current'] = True
        parent = node.get('parent')
        if parent and parent in retained_ids:
            payload_edges.append({'source': parent, 'target': node['node_id'], 'basis': node.get('parent_basis') or 'recorded', 'source_available': True})
        elif parent:
            entry['parent_known'] = False
            entry['parent_status'] = 'truncated'
            entry['parent_boundary'] = 'truncated_history'
            payload_edges.append({'source': parent, 'target': node['node_id'], 'basis': node.get('parent_basis') or 'recorded', 'source_available': False, 'source_boundary': 'truncated_history'})
        payload_nodes.append(entry)

    by_cycle: dict[str, list[dict[str, Any]]] = {}
    for node in payload_nodes:
        by_cycle.setdefault(str(node.get('cycle_id') or node['node_id']), []).append(node)
    aliases: dict[str, str] = {}
    for cid, nodes in by_cycle.items():
        ordered = sorted(nodes, key=lambda node: (1 if node.get('ts') else 0, node.get('ts') or '', node['node_id']))
        aliases[cid] = ordered[-1]['node_id']
        for index, item in enumerate(ordered, start=1):
            item['cycle_node_index'] = index
            item['cycle_node_count'] = len(ordered)

    ts_values = [node['ts'] for node in payload_nodes if node.get('ts')]
    payload = {'version': 2, 'current_sha': current_sha, 'current_node_id': current_node_id, 'coverage': {'raw_read_rows': raw_read_rows, 'unique_candidate_nodes': unique_candidate_nodes, 'emitted_nodes': len(payload_nodes), 'excluded_nodes': max(0, unique_candidate_nodes - len(payload_nodes)), 'retention_limit': LINEAGE_MAX_NODES, 'truncated': truncated, 'truncated_before_ts': truncated_before_ts, 'from_ts': min(ts_values) if ts_values else None, 'to_ts': max(ts_values) if ts_values else None}, 'nodes': payload_nodes, 'edges': payload_edges, 'aliases': aliases}

    parent_lookup = {edge['target']: edge['source'] for edge in payload_edges if edge.get('source_available') and edge.get('source') in retained_ids}
    depth_cache: dict[str, int] = {}
    def node_depth(node_id: str, guard: set[str] | None = None) -> int:
        if node_id in depth_cache:
            return depth_cache[node_id]
        guard = guard or set()
        if node_id in guard:
            return 0
        parent = parent_lookup.get(node_id)
        depth_cache[node_id] = node_depth(parent, guard | {node_id}) + 1 if parent else 0
        return depth_cache[node_id]
    for node in payload_nodes:
        node_depth(node['node_id'])
    levels: dict[int, int] = {}
    positions: dict[str, tuple[int, int]] = {}
    for node in sorted(payload_nodes, key=lambda item: (node_depth(item['node_id']), item.get('ts') or '', item['node_id'])):
        d = node_depth(node['node_id'])
        slot = levels.get(d, 0)
        levels[d] = slot + 1
        positions[node['node_id']] = (40 + slot * 54, 32 + d * 48)
    width = max(220, max((x for x, _ in positions.values()), default=180) + 40)
    height = max(84, max((y for _, y in positions.values()), default=42) + 36)
    data_json = json.dumps(payload, ensure_ascii=True, separators=(',', ':')).replace('<', '\\u003c')

    parts = ['<div class="lineage-day-filter lineage-unified-graph" data-default-filter="all" data-lineage-default-mode="all" data-lineage-now="' + esc(now or '') + '"><div class="lineage-day-controls">', '<button type="button" data-lineage-filter="all" class="active">All</button>', '<button type="button" data-lineage-filter="today">Today</button>', '<button type="button" data-lineage-filter="24h">24h</button>', '<button type="button" data-lineage-filter="yesterday-today">Yesterday+Today (UTC calendar)</button>', '<label>from <input type="date" data-lineage-from></label><label>to <input type="date" data-lineage-to></label>', '<button type="button" data-lineage-filter="range">Apply</button>', '<span class="lineage-filter-note" hidden></span></div>', '<div class="lineage-coverage-note" role="status" aria-live="polite" data-default-text="' + esc(_lineage_coverage_text(payload['coverage'])) + '">' + esc(_lineage_coverage_text(payload['coverage'])) + '</div>', '<div class="lineage-legend" aria-label="Lineage Legend">', '  <div class="lineage-legend-group"><span class="lineage-legend-title">Edges:</span>', '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="28" height="12"><line x1="0" y1="6" x2="28" y2="6" class="lineage-legend-edge lineage-legend-edge-recorded"/></svg> recorded</span>', '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="28" height="12"><line x1="0" y1="6" x2="28" y2="6" class="lineage-legend-edge lineage-legend-edge-inferred"/></svg> inferred</span></div>', '  <div class="lineage-legend-group"><span class="lineage-legend-title">Nodes:</span>', '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="14" height="14"><circle cx="7" cy="7" r="5" class="arch-node arch-integrated lineage-legend-node"/></svg> integrated</span>', '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="14" height="14"><circle cx="7" cy="7" r="5" class="arch-node arch-skipped lineage-legend-node"/></svg> skipped</span>', '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="14" height="14"><circle cx="7" cy="7" r="5" class="arch-node arch-partial lineage-legend-node"/></svg> partial</span>', '    <span class="lineage-legend-item"><svg class="lineage-legend-swatch" width="14" height="14"><circle cx="7" cy="7" r="5" class="arch-node arch-failed lineage-legend-node"/></svg> failed</span></div>', '  <div class="lineage-legend-group"><span class="lineage-legend-title">Current:</span>', '    <span class="lineage-legend-item"><span class="arch-star" style="font-size:14px;line-height:1;">&#9733;</span> current sha</span></div>', '</div>', f'<script type="application/json" id="lineage-data" hidden aria-hidden="true">{data_json}</script>', f'<svg id="lineage-svg" class="lineage-day-svg lineage-unified-dag arch-tree" width="{width}" height="{height}" viewBox="0 0 {width} {height}" data-lineage-renderer="unified-dag" data-lineage-rendered="server">']
    for edge in payload_edges:
        if not (edge.get('source_available') and edge['source'] in positions and edge['target'] in positions):
            continue
        x1, y1 = positions[edge['source']]
        x2, y2 = positions[edge['target']]
        basis = edge.get('basis') or 'recorded'
        cls = 'lineage-edge arch-edge' if basis == 'recorded' else 'lineage-edge lineage-edge-chronological'
        dash = ' stroke-dasharray="6 5"' if basis != 'recorded' else ''
        path_value = edge['source'] + '->' + edge['target']
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="{cls}" data-edge-type="canonical" data-basis="{esc(basis)}" data-source="{esc(edge["source"])}" data-target="{esc(edge["target"])}" data-path="{esc(path_value)}" fill="none"{dash}/>')
    for node in payload_nodes:
        x, y = positions[node['node_id']]
        if node.get('current'):
            parts.append(f'<text x="{x}" y="{y - 14}" text-anchor="middle" class="arch-star">&#9733;</text>')
        if node.get('parent_known') is False and node.get('parent_status') and node.get('parent_status') not in {'root', 'inferred'}:
            label = {'truncated': 'history truncated', 'current_unavailable': 'current unavailable', 'cycle': 'cycle detected'}.get(node.get('parent_status'), 'recorded parent unavailable')
            parts.append(f'<text class="lineage-hidden-parent" data-parent-status="{esc(node["parent_status"])}" x="{x}" y="{y - (26 if node.get("current") else 14)}" text-anchor="middle">{esc(label)}</text>')
        cid = str(node.get('cycle_id') or node['node_id'])
        kind = str(node.get('outcome') or 'integrated')
        title = str(node.get('title') or cid)
        attrs = [f'class="arch-node arch-{esc(kind)} lineage-node"', f'data-cycle-id="{esc(cid)}"', f'data-node-id="{esc(node["node_id"])}"', f'data-cycle-node-index="{int(node.get("cycle_node_index") or 1)}"', f'data-cycle-node-count="{int(node.get("cycle_node_count") or 1)}"', f'cx="{x}"', f'cy="{y}"', 'r="9"', 'tabindex="0"', 'role="button"', f'aria-label="{esc(title)} — click for details"', f'id="{_safe_node_dom_id(node["node_id"])}"']
        if node.get('boundary'):
            attrs.append(f'data-boundary="{esc(node["boundary"])}"')
        if node.get('parent_status'):
            attrs.append(f'data-parent-status="{esc(node["parent_status"])}"')
        if node.get('sha'):
            attrs.append(f'data-sha="{esc(node["sha"])}"')
        if node.get('ts_status') == 'invalid':
            attrs.append('data-ts-status="invalid"')
        parts.append('<circle ' + ' '.join(attrs) + f'><title>{esc(title)}</title></circle>')
    parts.append('</svg></div>')
    inline_scripts = ''.join(f'<script>{vendor_scripts[name]}</script>' for name in ('lineage-renderer.js',)) if vendor_scripts is not None else ''
    parts.append(f"""<section class="cycle-details-panel" id="cycle-details-panel" data-cycle-details-src="{LINEAGE_DETAILS_FILE}" hidden aria-label="Cycle details"><h2 class="cycle-details-title">Cycle details <button class="cycle-details-close" id="cycle-details-close" aria-label="Close cycle details">&times;</button></h2><div class="cycle-details-body"></div></section>{inline_scripts}<script>
(function () {{
  var panel = document.getElementById('cycle-details-panel'), src = panel.getAttribute('data-cycle-details-src'), data = null, loading = null;
  function esc(v) {{ var d = document.createElement('div'); d.textContent = v == null ? '' : String(v); return d.innerHTML; }}
  function line(label, value) {{ return value ? '<p><b>' + label + ':</b> ' + esc(value) + '</p>' : ''; }}
  function list(label, values) {{ if (!Array.isArray(values)) values = values ? [values] : []; return values.length ? '<h3>' + label + '</h3><ul>' + values.map(function (v) {{ return '<li>' + esc(v) + '</li>'; }}).join('') + '</ul>' : ''; }}
  function load() {{ if (data) return Promise.resolve(data); if (!loading) loading = fetch(src).then(function (r) {{ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }}).then(function (json) {{ data = json; return data; }}).catch(function (err) {{ loading = null; throw err; }}); return loading; }}
  function render(node, cid) {{ var item = (data && data[cid]) || {{cycle_id: cid}}; var count = Number(node.getAttribute('data-cycle-node-count') || '1'); var index = Number(node.getAttribute('data-cycle-node-index') || '1'); var multi = count > 1 ? '<p><b>Node:</b> ' + index + ' of ' + count + ' for ' + esc(cid) + '</p>' : ''; var html = '<h3>' + esc(item.title || cid) + '</h3>' + multi + line('Cycle', item.cycle_id) + line('Outcome', item.outcome) + line('Reason', item.reason) + line('Timestamp', item.ts) + line('SHA', item.sha) + line('Parent SHA', item.parent_sha) + line('Target path', item.target_path) + line('Serves / demand', item.serves || item.demand_id) + list('Files changed', item.files_changed) + list('Gate violations', item.gate_violations); html += '<p class="cycle-details-links"><a class="cycle-feed-link" href="cycles.html#cycle-' + encodeURIComponent(cid) + '">open in Cycle Feed</a> · <a href="lessons.html#q-' + encodeURIComponent(cid) + '">related lessons</a></p>'; panel.querySelector('.cycle-details-body').innerHTML = html; }}
  var selectedNode = null, openedByNode = null, openSeq = 0;
  function clearSelection() {{ if (selectedNode) {{ selectedNode.classList.remove('cycle-node-selected'); selectedNode = null; }} }}
  function closePanel() {{ panel.hidden = true; clearSelection(); var returnTo = openedByNode; openedByNode = null; var closeBtn = document.getElementById('cycle-details-close'); if (closeBtn && document.activeElement === closeBtn) closeBtn.blur(); if (returnTo && returnTo.focus) returnTo.focus({{ preventScroll: true }}); }}
  function scrollPanelIntoView() {{ panel.scrollIntoView({{ behavior: 'instant', block: 'nearest' }}); }}
  function open(node, fromHash) {{ clearSelection(); selectedNode = node; openedByNode = fromHash ? null : node; node.classList.add('cycle-node-selected'); var cid = node.getAttribute('data-cycle-id'); var seq = ++openSeq; if (!fromHash && history.replaceState) history.replaceState(null, '', '#' + node.id); panel.hidden = false; panel.querySelector('.cycle-details-body').innerHTML = '<p>loading ' + esc(cid) + ' …</p>'; var closeBtn = document.getElementById('cycle-details-close'); if (closeBtn) closeBtn.focus({{ preventScroll: true }}); load().then(function () {{ if (seq !== openSeq) return; render(node, cid); scrollPanelIntoView(); }}).catch(function (err) {{ if (seq !== openSeq) return; panel.querySelector('.cycle-details-body').innerHTML = '<p>' + esc(cid) + ': details unavailable — ' + esc(src) + ' could not be loaded or rendered (' + esc(err && err.message || err) + ').</p>'; scrollPanelIntoView(); }}); }}
  document.addEventListener('click', function (event) {{ var node = event.target.closest('.lineage-node'); if (node) {{ event.preventDefault(); open(node, false); return; }} if (event.target.closest('#cycle-details-close')) {{ closePanel(); }} }});
  document.addEventListener('keydown', function (event) {{ if (event.key === 'Escape' && !panel.hidden) {{ event.preventDefault(); closePanel(); }} }});
  function handleHash() {{ var hash = window.location.hash; if (!hash || !hash.startsWith('#node-')) return; var el = window.lineageRenderer && window.lineageRenderer.selectNodeFromHash ? window.lineageRenderer.selectNodeFromHash(hash) : document.getElementById(hash.slice(1)); if (!el) return; el.scrollIntoView({{ behavior: 'instant', block: 'center' }}); open(el, true); }}
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', handleHash); else Promise.resolve().then(handleHash);
  window.addEventListener('hashchange', handleHash);
}})();
</script>""")
    return '<div class="canvas-outer" id="panel-lineage">' + ''.join(parts) + '</div>'


def _build_day_bucketed_lineage(
    ledger_rows: list[Any],
    fallback_tree: dict[str, Any] | None,
    task_titles: dict[str, str] | None,
    now: str | None,
    cycle_details: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Issue #107: render recent ledger evolution rows grouped by UTC day.

    #208: this is the ONLY lineage implementation. Its former ~140-line dead
    tail (a second day-bucketed renderer behind this return) and the DGM
    archive tree that used to follow :func:`build_archive_tree`'s early return
    were deleted so a layout fix can only land in one place.
    """
    return _build_unified_lineage(ledger_rows, fallback_tree, task_titles, now, cycle_details)


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
        # #215: retain gate_violations when populated; strip only when empty so an
        # empty list is not serialised but present violations are not silently dropped.
        if not out.get('gate_violations'):
            out.pop('gate_violations', None)
    return records


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
    # #208: one implementation. The ledger's evolution_tree rows are the
    # primary source (present on every production run since #107); when they
    # are absent the tree.json nodes go through the SAME day-bucketed renderer
    # as its fallback_tree, so a layout fix cannot land in a second code path.
    # The ~230-line DGM archive tree (#53/#71 legend, colorbar, best path,
    # day separators, _cycle_details_panel) that used to follow an early
    # return here was unreachable in production and is gone.
    # `ledger_history or ledger_tail`: a fail-soft read leaves ledger_history as
    # an EMPTY list, and the ledger-only leaves in ledger_tail must still render.
    rows = (ledger_history if isinstance(ledger_history, list) else []) or (ledger_tail if isinstance(ledger_tail, list) else [])
    has_rows = any(
        isinstance(row, dict) and row.get('phase') == 'evolution_tree' and row.get('sha') and _lineage_day(row.get('ts'))
        for row in rows
    )
    tree_nodes = evolution_tree.get('nodes') if isinstance(evolution_tree, dict) else None
    has_tree = isinstance(tree_nodes, dict) and any(isinstance(n, dict) and _lineage_day(n.get('ts')) for n in tree_nodes.values())
    if not has_rows and not has_tree:
        return unavailable_panel('Evolution Lineage', 'evolution tree unavailable (no node with a usable timestamp)')
    return _build_day_bucketed_lineage(rows, evolution_tree, task_titles, now or datetime.now(timezone.utc).isoformat(), cycle_details)


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
        dt_msk = dt.astimezone(MSK_TZ)
        return dt_msk.strftime('%b %d').replace(' 0', ' ')

    lo, hi = min(dts), max(dts)
    lo_msk, hi_msk = lo.astimezone(MSK_TZ), hi.astimezone(MSK_TZ)
    if lo_msk.year == hi_msk.year:
        label = f'{_short(lo_msk)} - {_short(hi_msk)} MSK'
    else:
        label = f'{_short(lo_msk)} {lo_msk.year} - {_short(hi_msk)} {hi_msk.year} MSK'
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

def _render_failed_bridge_exits(bridge_exits: list[dict[str, Any]] | None) -> str:
    """Issue #190: surface recent failed bridge exits (error + where) in a collapsible details section."""
    if bridge_exits is None or not isinstance(bridge_exits, list):
        return ''
    failures = [
        row for row in bridge_exits
        if isinstance(row, dict) and (
            row.get('outcome') != 'success' or (
                row.get('exit_status') not in (0, None, '')
            )
        )
    ]
    if not failures:
        return ''
    recent_fails = failures[-10:]
    recent_fails.reverse()
    rows = []
    for f in recent_fails:
        ts = esc(str(f.get('ts') or ''))
        err = esc(str(f.get('error') or ''))
        where = esc(str(f.get('where') or ''))
        status = esc(str(f.get('exit_status') if f.get('exit_status') is not None else ''))
        loc = f' at {where}' if where else ''
        rows.append(
            f'<li><span class="fail-ts">{ts}</span>'
            f' [status {status}]: <code>{err}</code>'
            f'<span class="now-sub">{loc}</span></li>'
        )
    return f"""
        <details class="failed-exits-details" open>
          <summary><strong class="health-alert-text">Recent Failed Bridge Exits ({len(failures)})</strong></summary>
          <ul class="failed-exits-list">
            {''.join(rows)}
          </ul>
        </details>
    """


def build_now_panel(
    portfolio: dict[str, Any] | None,
    evolution_tree: dict[str, Any] | None,
    demand_rotation: dict[str, Any] | None,
    demand_completed: dict[str, Any] | None,
    task_titles: dict[str, str] | None = None,
    ledger_tail: list[dict[str, Any]] | None = None,
    age_seconds: float | None = None,
    now: str | None = None,
    proposer_llm_unavailable: bool = False,
    health_last_integrated_ts: str | None = None,
    health_recent_outcomes: list[str] | None = None,
    bridge_exit_streak: dict[str, Any] | None = None,
    bridge_exits: list[dict[str, Any]] | None = None,
    scorecard: dict[str, Any] | None = None,
    strategist_decisions: list[dict[str, Any]] | None = None,
) -> str:
    now = now or datetime.now(timezone.utc).isoformat()
    outcomes: list[str] = []
    ledger_integrated_ts: str | None = None
    if isinstance(ledger_tail, list):
        # read_ledger_tail returns rows oldest-first over a 5000-row window, so
        # the newest integration is the LAST evolution_tree row, not the first.
        for entry in ledger_tail:
            if not isinstance(entry, dict):
                continue
            outcome = str(entry.get('outcome') or entry.get('status') or '')
            if entry.get('phase') == 'outcome' and outcome:
                outcomes.append('failed' if outcome == 'fail' else outcome)
            if entry.get('phase') == 'evolution_tree' and entry.get('ts'):
                ledger_integrated_ts = str(entry['ts'])
    # Precedence: caller override, then the current tree node (authoritative
    # last integration), then the newest evolution_tree row in the ledger tail.
    tree_nodes = evolution_tree.get('nodes') if isinstance(evolution_tree, dict) else None
    tree_current = evolution_tree.get('current_sha') if isinstance(evolution_tree, dict) else None
    tree_node = tree_nodes.get(tree_current) if isinstance(tree_nodes, dict) and tree_current else None
    tree_integrated_ts = (
        str(tree_node['ts']) if isinstance(tree_node, dict) and tree_node.get('ts') else None
    )
    last_integrated_ts = health_last_integrated_ts or tree_integrated_ts or ledger_integrated_ts
    verdict, verdict_reason = health_verdict(
        age_seconds, last_integrated_ts, health_recent_outcomes if health_recent_outcomes is not None else outcomes, proposer_llm_unavailable, now,
        bridge_exit_streak=bridge_exit_streak,
        scorecard=scorecard,
    )
    health_banner = (
        f'<section class="health-verdict health-{verdict}" aria-label="Health verdict">'
        f'<span class="now-label">Health:</span> <strong>{esc(verdict.upper())}</strong> '
        f'<span class="health-verdict-reason">{esc(verdict_reason)}</span></section>'
    )
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

    # 4. Bridge exit streak (issue #182)
    streak_html = ''
    if bridge_exit_streak is None:
        streak_html = (
            '<div class="now-item"><span class="now-label">Bridge Exit Streak:</span> '
            '<span class="unavailable-note">unavailable</span></div>'
        )
    elif isinstance(bridge_exit_streak, dict):
        consec = bridge_exit_streak.get('consecutive_failures')
        if not isinstance(consec, int):
            streak_html = (
                '<div class="now-item"><span class="now-label">Bridge Exit Streak:</span> '
                '<span class="unavailable-note">unavailable</span></div>'
            )
        elif consec == 0:
            streak_html = (
                '<div class="now-item"><span class="now-label">Bridge Exit Streak:</span> '
                '<span class="badge badge-available">0 failures (healthy)</span></div>'
            )
        else:
            err = bridge_exit_streak.get('last_error') or ''
            where = bridge_exit_streak.get('last_where') or ''
            detail_parts = []
            if err:
                detail_parts.append(f'<code>{esc(err)}</code>')
            if where:
                detail_parts.append(f'<span class="now-sub">at {esc(where)}</span>')
            detail_str = f' &mdash; {" ".join(detail_parts)}' if detail_parts else ''
            streak_html = (
                '<div class="now-item"><span class="now-label">Bridge Exit Streak:</span> '
                f'<strong class="health-alert-text">{consec} consecutive failure{"s" if consec != 1 else ""}</strong>'
                f'{detail_str}</div>'
            )

    # 5. Monitored feed ages
    feed_ages_html = _build_monitored_feed_ages_item(scorecard)

    # 6. Doc-only budget guard
    doc_budget_html = _build_doc_only_budget_item(ledger_tail)

    # 7. Strategist run provenance (#204)
    strategist_html = _build_strategist_run_item(strategist_decisions)

    return f'''
    <section class="panel panel-now" id="panel-now">
      <h2 class="panel-title">Now / Active Focus</h2>
      <div class="now-content">
        {health_banner}
        {dir_html}
        {cycle_html}
        {streak_html}
        {feed_ages_html}
        {doc_budget_html}
        {strategist_html}
        {_render_failed_bridge_exits(bridge_exits)}
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
    rendered_lesson_ids: set[str] | None = None,
    ledger_history: list[Any] | None = None,
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
        entity_links: list[str] = []
        for phase in phases:
            demand_id = phase.get('demand_id')
            if demand_id:
                entity_links.append(f'<span class="entity-chip">{esc(str(demand_id))}</span>')
            context = phase.get('lessons_context')
            if isinstance(context, list):
                entity_links.extend(
                    f'<a class="lesson-link" href="lessons.html#q-{esc(str(lesson_id).split(":", 1)[-1])}">{esc(str(lesson_id))}</a>'
                    for lesson_id in context if lesson_id and (rendered_lesson_ids is None or str(lesson_id).split(":", 1)[-1] in rendered_lesson_ids)
                )

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

        # Issue #72, #225: day grouping in MSK (newest-first) + outcome filter attribute.
        day_html = ''
        if history_mode and ts_val:
            dt = _parse_iso_ts(ts_val)
            day = dt.astimezone(MSK_TZ).strftime('%Y-%m-%d') if dt is not None else str(ts_val)[:10]
            if day != last_day:
                last_day = day
                day_html = f'<li class="feed-day-header">{esc(day)} MSK</li>\n        '

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
          {('<div class="entity-links">' + ' &middot; '.join(dict.fromkeys(entity_links)) + '</div>') if entity_links else ''}
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
        filter_empty = '<li class="filter-empty" data-filter-empty hidden>0 cycles with status <span class="filter-empty-value"></span></li>'
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
            'function apply(k){var rows=getRows(),shown=0;'
            'rows.forEach(function(r){var hidden=k!=="all"&&r.getAttribute("data-outcome")!==k;r.classList.toggle("filtered-out",hidden);if(!hidden)shown++;});'
            'var empty=document.querySelector("[data-filter-empty]");if(empty){empty.hidden=k==="all"||shown>0;empty.querySelector(".filter-empty-value").textContent=k;}'
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
        if ledger_history is None:
            title_line = f'Cycle History (Recent {len(rows)})'
        else:
            title_line = f'Cycle History ({len(rows)} cycles)'
    else:
        filter_html = ''
        title_line = f'Cycle Feed (Recent {len(rows)})'

    return f'''
    <section class="panel panel-feed" id="panel-feed">
      <h2 class="panel-title">{title_line}</h2>
      {filter_html}
      <ul class="feed-list">
        {filter_empty if history_mode else ''}
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
    demand_futility: dict[str, Any] | None = None,
    scorecard: dict[str, Any] | None = None,
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
        item_anchor = f' id="q-{esc(str(hid))}"' if hid else ''
        first_seen = info.get('first_seen') or ''
        last_touched = info.get('last_touched') or ''
        badge_cls = 'badge-stale' if is_stale_flag else 'badge-researching'
        badge_lbl = 'STALE' if (is_stale_flag and status != 'stale') else status.upper()
        return f'''
            <li class="hypo-row active"{item_anchor}>
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
        item_anchor = f' id="q-{esc(str(hid))}"' if hid else ''

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
            <li class="hypo-row answered"{item_anchor}>
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

    # Extract active gap metrics from scorecard if available
    active_gap_metrics = None
    if isinstance(scorecard, dict) and isinstance(scorecard.get('gaps'), list):
        active_gap_metrics = {
            g.get('metric') for g in scorecard.get('gaps', [])
            if isinstance(g, dict) and g.get('metric')
        }

    # Issue #95: render strategist durable HADI hypotheses from backlog.json
    # as a separate section -- clearly labelled and separated from live data.
    durable_html = _build_durable_hadi_section(hypotheses_durable)
    futility_html = _build_demand_futility_section(demand_futility, active_gap_metrics=active_gap_metrics)

    return f'''
    <section class="panel panel-hypotheses" id="panel-hypotheses">
      <h2 class="panel-title">Hypotheses Lifecycle</h2>
      {futility_html}
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


def _build_demand_futility_section(
    demand_futility: dict[str, Any] | None,
    active_gap_metrics: set[str] | None = None,
) -> str:
    """Issue #185: surface goal gap futility progress from state/demand/futility.json.
    
    3-state reporting:
    - Missing/unavailable: rendered as unavailable note (not 0/10).
    - Healthy / Low attempts: rendered as compact meter.
    - High attempts (>= 7/10): prominent warning banner with attempt unit & surface tokens.
    """
    if demand_futility is None:
        return '<div class="hypo-futility-section"><p class="unavailable-note">goal gap futility: unavailable</p></div>'
    if not isinstance(demand_futility, dict):
        return '<div class="hypo-futility-section"><p class="unavailable-note">goal gap futility: invalid format</p></div>'

    # Accept both root-keyed gap dict and nested {'gaps': {...}} structure
    gaps = demand_futility.get('gaps') if isinstance(demand_futility.get('gaps'), dict) else demand_futility
    if not isinstance(gaps, dict) or not gaps:
        return '<div class="hypo-futility-section"><p class="unavailable-note">goal gap futility: no active gap tracking</p></div>'

    rows: list[str] = []
    has_alarm = False

    for gap_id, gap_data in sorted(gaps.items()):
        if not isinstance(gap_data, dict):
            continue
        attempts = gap_data.get('attempt_count', 0)
        threshold = gap_data.get('threshold', 10)
        unit = gap_data.get('attempt_unit') or 'demand_id'
        surface = gap_data.get('surface') or []
        surface_str = f' (surface: {", ".join(map(str, surface))})' if surface and unit == 'lever_surface' else ''
        metric = gap_data.get('metric')

        # Check whether this gap corresponds to an active gap in the current scorecard
        is_active = active_gap_metrics is None or (metric is not None and metric in active_gap_metrics)

        is_warning = is_active and (attempts >= 7)
        if is_warning:
            has_alarm = True

        if not is_active:
            status_cls = 'badge-dim'
            active_label = ' <span class="gap-status-note">(resolved)</span>'
        elif is_warning:
            status_cls = 'badge-stale'
            active_label = ''
        else:
            status_cls = 'badge-researching'
            active_label = ''

        attempts_label = f'{attempts}/{threshold} attempts [{esc(unit)}]{esc(surface_str)}{active_label}'

        rows.append(
            f'<li class="futility-item{" futility-resolved" if not is_active else ""}">'
            f'<span class="badge {status_cls}">{esc(gap_id)}</span> '
            f'<strong class="futility-attempts">{attempts_label}</strong>'
            f'</li>'
        )

    if not rows:
        return '<div class="hypo-futility-section"><p class="unavailable-note">goal gap futility: no valid records</p></div>'

    alarm_class = ' futility-alarm' if has_alarm else ''
    return (
        f'<div class="hypo-futility-section{alarm_class}">'
        f'<h3 class="hypo-futility-title">Goal Gap Futility Tracking</h3>'
        f'<ul class="futility-list">{"".join(rows)}</ul>'
        f'</div>'
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
    id_counts: dict[str, int] = {}
    for entry in entries:
        lesson_id = str(entry.get('id') or '')
        if lesson_id:
            id_counts[lesson_id] = id_counts.get(lesson_id, 0) + 1
    # Issue #130: duplicates on disk are all rendered, so the anchor has to be
    # unique per rendered row or the page carries duplicate DOM ids. The FIRST
    # occurrence keeps the bare `q-<id>` anchor, because the cycle-page links
    # built in #129 point at it; later occurrences get an ordinal suffix.
    id_seen: dict[str, int] = {}

    def _lesson_anchor(lesson: dict[str, Any]) -> str:
        lesson_id = str(lesson.get('id') or '')
        if not lesson_id:
            return ''
        nth = id_seen.get(lesson_id, 0) + 1
        id_seen[lesson_id] = nth
        suffix = '' if nth == 1 else f'-{nth}'
        return f' id="q-{esc(lesson_id)}{suffix}"'

    rendered_lesson_ids = {str(l.get('id')) for l in entries if l.get('id')}
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
        duplicate_html = '<span class="lesson-duplicate-warning">duplicate id on disk</span>' if id_counts.get(str(l.get('id') or ''), 0) > 1 else ''
        item_anchor = _lesson_anchor(l)

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
        meta_chips = severity_html + tags_html + seen_html + duplicate_html
        meta_chips_html = f'<div class="lesson-chips">{meta_chips}</div>' if meta_chips else ''

        problem_html = f'<div class="lesson-problem"><span class="lesson-label">Problem:</span> {esc(problem[:400])}{"..." if len(problem) > 400 else ""}</div>' if problem else ''
        solution_html = f'<div class="lesson-solution"><span class="lesson-label">Solution:</span> {esc(solution[:400])}{"..." if len(solution) > 400 else ""}</div>' if solution else ''

        search_text = esc((' '.join([
            l.get('id') or '', str(l.get('task_id') or ''), problem, solution,
            severity, ' '.join(str(t) for t in tags_list), cid,
        ])).lower())
        v2_rows.append(
            f'<li class="lesson-row lesson-row-v2" data-text="{search_text}"{item_anchor}>'
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
        duplicate_html = '<span class="lesson-duplicate-warning">duplicate id on disk</span>' if id_counts.get(str(l.get('id') or ''), 0) > 1 else ''
        item_anchor = _lesson_anchor(l)
        insight_html = f'<div class="lesson-insight">{esc(insight[:300])}</div>' if insight else ''
        search_text = esc((' '.join([
            l.get('id') or '', str(l.get('task_id') or ''), str(l.get('hypothesis') or ''),
            result, insight, cid,
        ])).lower())
        legacy_rows.append(
            f'<li class="lesson-row" data-text="{search_text}"{item_anchor}>'
            f'<div class="lesson-meta"><span class="lesson-id" translate="no">{esc(l.get("id") or "")}</span>{duplicate_html}'
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
        <li class="filter-empty" data-filter-empty hidden>0 results for <span class="filter-empty-value"></span></li>
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
           var shown = 0;
           rowsL.forEach(function (r) {{
             var visible = !t || r.getAttribute('data-text').indexOf(t) !== -1;
             r.style.display = visible ? '' : 'none';
             if (visible) shown += 1;
           }});
           var empty = document.querySelector('#panel-lessons [data-filter-empty]');
           if (empty) {{ empty.hidden = !t || shown > 0; empty.querySelector('.filter-empty-value').textContent = t ? '"' + t + '"' : ''; }}
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
    agent_context: dict[str, Any] | None = None,
) -> str:
    context_html = build_two_tier_context_html(agent_context)
    # 1. AGENTS.md
    if agents_md is not None:
        md_text = agents_md.strip()
        md_body = esc(md_text[:2000]) + ('...' if len(md_text) > 2000 else '')
        # Issue #44: capped scroll boxes are scroll-traps; native <details>
        # keeps the page one scrolling document, closed by default.
        agents_html = (
            f'<details class="charter-details agents-md-box">'
            f'<summary>AGENTS.md charter ({len(md_text.splitlines())} lines)</summary>'
            f'<div class="agent-wide-content"><pre><code>{md_body}</code></pre></div></details>'
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
            f'<div class="agent-wide-content"><pre><code>{g_body}</code></pre></div></details>'
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
                    <div class="agent-wide-content"><table class="skills-table">
                  <thead>
                    <tr><th>Skill</th><th>Reads (skill_fitness/reads.json)</th><th>Confirmed Usage (skill_fitness/reads.json; skill_fitness/evals.jsonl when present)</th></tr>
                  </thead>
                  <tbody>
                    {''.join(rows)}
                  </tbody>
                </table></div>
                '''
            else:
                skills_html = '<p class="unavailable-note">no skill reads recorded</p>'

    return f'''
    <section class="panel panel-agent" id="panel-agent">
      <h2 class="panel-title">Agent Configuration & Fitness</h2>
      {context_html}
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


def _fmt_feed_age_token(age_sec):
    """Render one feed age or threshold.

    Ages and thresholds are printed side by side as ``age/threshold``, so the
    two must land in the same unit to be comparable at a glance. Every
    ``_FEEDS`` threshold is 12h or 24h, so days only start at 48h; below that a
    24h threshold reads ``24.0h`` rather than ``1.0d`` and a 15h age can be
    compared to it without arithmetic.
    """
    if age_sec is None or not isinstance(age_sec, (int, float)):
        return 'unknown'
    if age_sec >= 172800:
        return f'{age_sec / 86400:.1f}d'
    if age_sec >= 3600:
        return f'{age_sec / 3600:.1f}h'
    if age_sec >= 60:
        return f'{int(age_sec // 60)}m'
    return f'{int(age_sec)}s'


def _build_monitored_feed_ages_item(scorecard):
    if not isinstance(scorecard, dict):
        return '<div class="now-item"><span class="now-label">Feed Freshness:</span> <span class="unavailable-note">unavailable</span></div>'
    feeds_sec = scorecard.get('feeds')
    if not isinstance(feeds_sec, dict):
        return '<div class="now-item"><span class="now-label">Feed Freshness:</span> <span class="unavailable-note">unavailable</span></div>'
    feeds = feeds_sec.get('feeds')
    if not isinstance(feeds, dict) or not feeds:
        return '<div class="now-item"><span class="now-label">Feed Freshness:</span> <span class="unavailable-note">unavailable</span></div>'
    badges = []
    for name in sorted(feeds.keys()):
        info = feeds[name]
        if not isinstance(info, dict):
            continue
        st = info.get('status') or 'unknown'
        age_sec = info.get('age_seconds')
        max_sec = info.get('max_age_seconds')
        age_str = _fmt_feed_age_token(age_sec)
        max_str = _fmt_feed_age_token(max_sec)
        # scorecard._feed_details emits fresh|stale|corrupt|unreadable|missing.
        badge_cls = 'feed-badge-ok' if st in ('fresh', 'ok') else f'feed-badge-{esc(st)}'
        badges.append(
            f'<span class="feed-age-badge {badge_cls}" title="{esc(name)}: {esc(st)}">'
            f'{esc(name)}: {esc(age_str)}/{esc(max_str)}</span>'
        )
    if not badges:
        return ''
    return (
        '<div class="now-item"><span class="now-label">Feed Freshness:</span> '
        + ' '.join(badges)
        + '</div>'
    )



def _build_strategist_run_item(decisions: list[dict[str, Any]] | None) -> str:
    """Issue #204: the strategist's newest run, from state/strategist/decisions.jsonl.

    The strategist was paused for 36 hours because three of its five inputs were
    structurally dead and it kept advising from them; nothing on this dashboard
    showed that, which is why it took an audit rather than an alert. Each row
    carries `inputs_status` per input (#1182) precisely so the condition is
    machine-readable, so the states that must stay apart are:

      unavailable  no file, unreadable, or no row -- not the same as a zero
      refused      the guard declined the LLM call on a mostly empty view
      error        the run failed
      ok           it ran; the input ratio says on how much of a view

    A refusal must never render like a healthy run: that conflation is the
    original defect, moved from the runtime into the panel.
    """
    unavailable = ('<div class="now-item"><span class="now-label">Strategist:</span> '
                   '<span class="unavailable-note">unavailable</span></div>')
    if not isinstance(decisions, list) or not decisions:
        return unavailable
    row = None
    for candidate in reversed(decisions):
        if isinstance(candidate, dict):
            row = candidate
            break
    if row is None:
        return unavailable

    status = row.get('inputs_status')
    names = ('goals', 'scorecard', 'funnel', 'insights', 'evolution_tree')
    complete, degraded = 0, []
    if isinstance(status, dict):
        for name in names:
            entry = status.get(name)
            state = entry.get('status') if isinstance(entry, dict) else None
            if state == 'complete':
                complete += 1
            else:
                degraded.append(f'{name}:{state or "?"}')
    ratio = f'inputs {complete}/{len(names)}'

    counts = row.get('counts') if isinstance(row.get('counts'), dict) else {}
    produced = (f"{int(counts.get('hypotheses_appended') or 0)} hypotheses, "
                f"{int(counts.get('advisories_written') or 0)} advisories")

    reason = str(row.get('reason') or '')
    if row.get('success') is True:
        badge, label = 'badge badge-available', 'ran'
    elif 'refus' in reason.lower():
        badge, label = 'badge badge-rejected', 'refused'
    else:
        badge, label = 'health-alert-text', 'error'

    when = str(row.get('timestamp') or row.get('ts') or '')[:19] or 'unknown time'
    detail = f'{label} {when} — {ratio}, {produced}'
    if degraded:
        detail += ' — degraded: ' + ', '.join(degraded[:3])
    if label != 'ran' and reason:
        detail += f' — {reason[:80]}'
    return (f'<div class="now-item"><span class="now-label">Strategist:</span> '
            f'<span class="{badge}">{esc(detail)}</span></div>')

def _build_doc_only_budget_item(ledger_tail: list[dict[str, Any]] | None) -> str:
    """Issue #200: doc-only budget guard item in the Now panel.
    Extracts the latest doc_only_budget row from ledger_tail.
    Renders 4 distinct states:
    1. unavailable: ledger_tail missing/empty or no doc_only_budget row.
    2. blind: ledger_blind=True (alert: ledger unreadable / fail-open).
    3. deferring: doc_only_deferred > 0 (alert/warning badge: deferred N, X/Y cap).
    4. cap reached vs within budget:
       - exceeded=True (cap reached: X/Y 24h, 0 pending doc proposals of N)
       - exceeded=False (within budget: X/Y 24h)
    """
    if not isinstance(ledger_tail, list) or not ledger_tail:
        return '<div class="now-item"><span class="now-label">Doc Budget Guard:</span> <span class="unavailable-note">unavailable</span></div>'

    latest_doc_row: dict[str, Any] | None = None
    for row in reversed(ledger_tail):
        if isinstance(row, dict) and row.get('phase') == 'doc_only_budget':
            latest_doc_row = row
            break

    if not latest_doc_row:
        return '<div class="now-item"><span class="now-label">Doc Budget Guard:</span> <span class="unavailable-note">unavailable</span></div>'

    blind = bool(latest_doc_row.get('ledger_blind'))
    deferred = int(latest_doc_row.get('doc_only_deferred') or 0)
    int_24h = int(latest_doc_row.get('doc_only_integrations_24h') or 0)
    budget_24h = int(latest_doc_row.get('doc_only_budget_24h') or 0)
    exceeded = bool(latest_doc_row.get('doc_budget_exceeded'))
    items = int(latest_doc_row.get('items_considered') or 0)

    if blind:
        badge_cls = 'health-alert-text'
        status_text = 'ledger unreadable (fail-open)'
    elif deferred > 0:
        badge_cls = 'badge badge-rejected'
        status_text = f'deferring doc-only ({deferred} deferred, {int_24h}/{budget_24h} cap)'
    elif exceeded:
        badge_cls = 'badge badge-available'
        status_text = f'cap reached ({int_24h}/{budget_24h} 24h, 0 pending doc proposals of {items})'
    else:
        badge_cls = 'badge badge-available'
        status_text = f'within budget ({int_24h}/{budget_24h} 24h)'

    return (
        f'<div class="now-item"><span class="now-label">Doc Budget Guard:</span> '
        f'<span class="{badge_cls}">{esc(status_text)}</span></div>'
    )


def _build_provenance_badge(scorecard: dict[str, Any] | None) -> str:
    """Issue #188: 10-status data provenance indicator (4 readers + 5 feeds + gaps_status).
    - Unavailable if scorecard or reader_status is missing.
    - Compact 'Window: 7d (complete)' if all 10 inputs healthy.
    - Specific anomaly alert naming degraded feed/reader otherwise.
    """
    if not isinstance(scorecard, dict):
        return '<span class="provenance-badge provenance-unavailable">data inputs: unavailable</span>'
    reader_status = scorecard.get('reader_status')
    if not isinstance(reader_status, dict):
        return '<span class="provenance-badge provenance-unavailable">data inputs: unavailable</span>'

    healthy_statuses = {'complete', 'present', 'fresh'}
    anomalies: list[str] = []

    # 4 flat readers
    for key in ('ledger', 'completed', 'heldout', 'history'):
        val = reader_status.get(key)
        if isinstance(val, dict):
            status = val.get('status')
            if status not in healthy_statuses:
                anomalies.append(f'{key}: {status or "unknown"}')
        else:
            anomalies.append(f'{key}: missing')

    # 5 nested feeds
    feeds = reader_status.get('feeds')
    if isinstance(feeds, dict):
        for feed_name, feed_info in feeds.items():
            if isinstance(feed_info, dict):
                st = feed_info.get('status')
                if st not in healthy_statuses:
                    anomalies.append(f'{feed_name}: {st or "unknown"}')
            else:
                anomalies.append(f'{feed_name}: missing')
    else:
        anomalies.append('feeds: missing')

    # gaps_status
    gaps_status = scorecard.get('gaps_status')
    if gaps_status and gaps_status not in healthy_statuses:
        anomalies.append(f'gaps: {gaps_status}')

    window_days = scorecard.get('window_days', 7)

    if not anomalies:
        return f'<span class="provenance-badge provenance-complete">Window: {window_days}d (complete)</span>'

    anomaly_str = esc(', '.join(anomalies))
    return f'<span class="provenance-badge provenance-anomaly" title="{anomaly_str}">&#x26a0;&#xfe0f; data inputs: {anomaly_str}</span>'


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

    prov_html = _build_provenance_badge(scorecard)

    return f'''
    <header class="empire-strip">
      <h1 class="empire-title"># eeebot / tech-tree</h1>
      <div class="empire-stats">{stat_html}</div>
      {prov_html}
      <div class="empire-fresh">{freshness_html}</div>
    </header>
    '''


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------



def build_tokens_panel(data: dict[str, Any], host: str = '', generated_at: str | None = None) -> str:
    """Issue #223: Standalone interactive token heatmap panel for tokens.html."""
    token_heatmap = data.get('token_heatmap')
    if not token_heatmap or not token_heatmap.get('dates'):
        return unavailable_panel('Token Heatmap', 'no LLM calls data recorded')

    summary = token_heatmap.get('summary') or {}
    total_tokens = summary.get('total_tokens', 0)
    vendor_tokens = summary.get('vendor_tokens', summary.get('gateway_tokens', 0))
    self_hosted_tokens = summary.get('self_hosted_tokens', summary.get('local_tokens', 0))
    other_tokens = summary.get('other_tokens', 0)
    total_calls = summary.get('total_calls', 0)
    days_span = summary.get('days_span', 0)
    days_present = summary.get('days_present', 0)
    days_missing = summary.get('days_missing', 0)

    gw_pct = (vendor_tokens / total_tokens * 100) if total_tokens else 0.0
    loc_pct = (self_hosted_tokens / total_tokens * 100) if total_tokens else 0.0
    oth_pct = (other_tokens / total_tokens * 100) if total_tokens else 0.0

    other_kpi_card = f'''    <div class="tkn-kpi-card">
      <div class="tkn-kpi-val">{fmt_tokens(other_tokens)} <span class="tkn-kpi-pct">({oth_pct:.1f}%)</span></div>
      <div class="tkn-kpi-lbl">Unclassified Models</div>
    </div>''' if other_tokens > 0 else ''

    raw_json = json.dumps(token_heatmap)

    return f"""
<section class="panel" id="panel-tokens">
  <div class="panel-header">
    <h2>Token Heatmap</h2>
    <p class="panel-subtitle">Hourly and 5-minute LLM token consumption across all recorded autonomous cycles (displayed in MSK, UTC+3; source stored in UTC).</p>
  </div>

  <div class="tkn-kpi-strip">
    <div class="tkn-kpi-card">
      <div class="tkn-kpi-val">{fmt_tokens(total_tokens)}</div>
      <div class="tkn-kpi-lbl">Total Tokens</div>
    </div>
    <div class="tkn-kpi-card">
      <div class="tkn-kpi-val">{fmt_tokens(vendor_tokens)} <span class="tkn-kpi-pct">({gw_pct:.1f}%)</span></div>
      <div class="tkn-kpi-lbl">Vendor API (cl/*, an/*)</div>
    </div>
    <div class="tkn-kpi-card">
      <div class="tkn-kpi-val">{fmt_tokens(self_hosted_tokens)} <span class="tkn-kpi-pct">({loc_pct:.1f}%)</span></div>
      <div class="tkn-kpi-lbl">Self-hosted GPU (un/qwen)</div>
    </div>
{other_kpi_card}
    <div class="tkn-kpi-card">
      <div class="tkn-kpi-val">{total_calls:,}</div>
      <div class="tkn-kpi-lbl">LLM Calls</div>
    </div>
    <div class="tkn-kpi-card">
      <div class="tkn-kpi-val">{days_span}d <span class="tkn-kpi-pct">({days_present} active, {days_missing} unobserved)</span></div>
      <div class="tkn-kpi-lbl">Calendar Window</div>
    </div>
  </div>

  <div class="tkn-controls">
    <span class="tkn-ctrl-label">View:</span>
    <button type="button" class="tkn-btn active" data-view="split">Split View (Self-hosted vs Vendor)</button>
    <button type="button" class="tkn-btn" data-view="gateway">Vendor API (Gemini / Claude)</button>
    <button type="button" class="tkn-btn" data-view="local">Self-hosted GPU (Qwen)</button>
    <button type="button" class="tkn-btn" data-view="total">Combined Total</button>
  </div>

  <div class="tkn-grid-container" id="tkn-grid-container">
    <div class="tkn-card" id="tkn-card-gw">
      <div class="tkn-card-header">
        <h3>Vendor API (cl/*, an/* — Cloud Providers)</h3>
        <span class="tkn-card-sub">Proposer, Reflector, Strategist, Curator &bull; Quantile scale</span>
      </div>
      <div class="tkn-matrix-wrap" id="matrix-gw"></div>
      <div class="tkn-legend" id="legend-gw"></div>
    </div>

    <div class="tkn-card" id="tkn-card-loc">
      <div class="tkn-card-header">
        <h3>Self-hosted GPU (un/* — Qwen на 3090Ti)</h3>
        <span class="tkn-card-sub">Bridge Executor (без потокенной оплаты) &bull; Quantile scale</span>
      </div>
      <div class="tkn-matrix-wrap" id="matrix-loc"></div>
      <div class="tkn-legend" id="legend-loc"></div>
    </div>

    <div class="tkn-card" id="tkn-card-tot" style="display: none;">
      <div class="tkn-card-header">
        <h3>Combined Total LLM Calls</h3>
        <span class="tkn-card-sub">All models aggregated &bull; Quantile scale</span>
      </div>
      <div class="tkn-matrix-wrap" id="matrix-tot"></div>
      <div class="tkn-legend" id="legend-tot"></div>
    </div>
  </div>

  <div class="tkn-infra-notice">
    <div class="tkn-notice-icon">&#9432;</div>
    <div class="tkn-notice-body">
      <strong>Инфраструктурный маршрут:</strong> обе категории моделей маршрутизируются через общий локальный шлюз LiteLLM на <code>192.168.1.35:4001</code>.
      EeePC выступает исключительно локальным оркестратором (CPU Atom N270, 2 ГБ RAM) — вызовы <code>un/qwen</code> обслуживаются на выделенной GPU в LAN (RTX 3090Ti, без потокенной оплаты), а вызовы <code>cl/*</code> и <code>an/*</code> уходят во внешние вендорские API.
      При отказе LAN-шлюза вызовы обеих категорий останавливаются одновременно (резервирования между ними нет).
      <br><br>
      <strong>Часовой пояс:</strong> журнал и исходные данные хранятся в <strong>UTC</strong>. Все графики, ячейки часов, строки дней и 5-минутные интервалы пересчитаны и отображаются в <strong>MSK (UTC+3)</strong>. Строка даты MSK строго содержит 24 часа с 00:00 до 23:59 MSK (события после 21:00 UTC попадают в следующие сутки MSK).
    </div>
  </div>

  <div class="tkn-card tkn-detail-card" id="tkn-day-detail">
    <div class="tkn-card-header">
      <h3>Day Detail: <span id="tkn-active-date-label"></span></h3>
      <span class="tkn-card-sub">288 five-minute intervals (24h MSK) &bull; Источник: UTC &bull; Отображение: MSK (UTC+3) &bull; Hover for breakdown, click an hour above to inspect</span>
    </div>
    <div class="tkn-strip-wrap" id="tkn-strip-wrap"></div>
    <div class="tkn-strip-hours">
      <span>00h</span><span>03h</span><span>06h</span><span>09h</span><span>12h</span><span>15h</span><span>18h</span><span>21h</span><span>24h</span>
    </div>
    <div class="tkn-detail-info" id="tkn-detail-info">Hover over any 5-minute block to inspect tokens and top component.</div>
  </div>

  <div id="tkn-tooltip" class="tkn-tooltip" style="display: none;"></div>

  <script id="token-heatmap-data" type="application/json">{raw_json}</script>

  <style>
    .tkn-kpi-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin: 16px 0 24px 0;
    }}
    .tkn-kpi-card {{
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 6px;
      padding: 12px 16px;
      flex: 1 1 170px;
    }}
    .tkn-kpi-val {{
      font-size: 1.45rem;
      font-weight: 700;
      color: #f0f6fc;
    }}
    .tkn-kpi-pct {{
      font-size: 0.85rem;
      font-weight: normal;
      color: #8b949e;
    }}
    .tkn-kpi-lbl {{
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #8b949e;
      margin-top: 4px;
    }}
    .tkn-controls {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }}
    .tkn-ctrl-label {{
      font-size: 0.85rem;
      color: #8b949e;
      font-weight: 600;
    }}
    .tkn-btn {{
      background: #21262d;
      color: #c9d1d9;
      border: 1px solid #30363d;
      border-radius: 6px;
      padding: 5px 12px;
      font-size: 0.8rem;
      cursor: pointer;
      transition: all 0.15s ease;
    }}
    .tkn-btn:hover {{
      background: #30363d;
      color: #fff;
    }}
    .tkn-btn.active {{
      background: #1f6feb;
      color: #fff;
      border-color: #388bfd;
    }}
    .tkn-grid-container {{
      display: flex;
      flex-wrap: wrap;
      gap: 20px;
      margin-bottom: 24px;
    }}
    .tkn-card {{
      flex: 1 1 480px;
      min-width: 320px;
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 6px;
      padding: 16px;
      overflow-x: auto;
    }}
    .tkn-infra-notice {{
      background: #161b22;
      border: 1px solid #30363d;
      border-left: 4px solid #f0883e;
      border-radius: 6px;
      padding: 12px 16px;
      margin: 0 0 24px 0;
      display: flex;
      gap: 12px;
      align-items: flex-start;
      font-size: 0.84rem;
      line-height: 1.45;
      color: #c9d1d9;
    }}
    .tkn-notice-icon {{
      font-size: 1.15rem;
      color: #f0883e;
      line-height: 1.2;
    }}
    .tkn-notice-body code {{
      background: #0d1117;
      padding: 2px 6px;
      border-radius: 4px;
      border: 1px solid #30363d;
      color: #58a6ff;
      font-size: 0.8rem;
    }}
    .tkn-card-header {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-bottom: 12px;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .tkn-card-header h3 {{
      margin: 0;
      font-size: 1.05rem;
      font-weight: 600;
      color: #f0f6fc;
    }}
    .tkn-card-sub {{
      font-size: 0.76rem;
      color: #8b949e;
    }}
    .tkn-matrix-wrap {{
      display: flex;
      gap: 2px;
      user-select: none;
      padding-bottom: 4px;
    }}
    .tkn-hour-col {{
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding-right: 6px;
    }}
    .tkn-hour-lbl {{
      height: 11px;
      line-height: 11px;
      font-size: 9px;
      color: #6e7681;
      text-align: right;
      width: 16px;
    }}
    .tkn-day-col {{
      display: flex;
      flex-direction: column;
      gap: 2px;
      cursor: pointer;
    }}
    .tkn-day-col:hover .tkn-cell {{
      outline: 1px solid #58a6ff;
    }}
    .tkn-day-col.active-col .tkn-cell {{
      outline: 1px solid #f0883e;
    }}
    .tkn-cell {{
      width: 11px;
      height: 11px;
      border-radius: 2px;
      box-sizing: border-box;
      transition: transform 0.05s ease;
    }}
    .tkn-cell:hover {{
      transform: scale(1.25);
      z-index: 2;
    }}
    /* Distinct NO DATA vs ZERO */
    .cell-nodata {{
      background: repeating-linear-gradient(
        45deg,
        #13171f,
        #13171f 2px,
        #21262d 2px,
        #21262d 4px
      ) !important;
      border: 1px solid #21262d;
    }}
    .cell-zero {{
      background: #161b22;
    }}
    /* Gateway scale (Blue) */
    .gw-lvl-1 {{ background: #0e2d54; }}
    .gw-lvl-2 {{ background: #114b8b; }}
    .gw-lvl-3 {{ background: #1f6feb; }}
    .gw-lvl-4 {{ background: #58a6ff; }}
    .gw-lvl-5 {{ background: #a5d6ff; }}

    /* Local scale (Green) */
    .loc-lvl-1 {{ background: #0e4429; }}
    .loc-lvl-2 {{ background: #006d32; }}
    .loc-lvl-3 {{ background: #26a641; }}
    .loc-lvl-4 {{ background: #39d353; }}
    .loc-lvl-5 {{ background: #7ee787; }}

    /* Total scale (Purple) */
    .tot-lvl-1 {{ background: #2b1d4c; }}
    .tot-lvl-2 {{ background: #49288f; }}
    .tot-lvl-3 {{ background: #6e40c9; }}
    .tot-lvl-4 {{ background: #8957e5; }}
    .tot-lvl-5 {{ background: #d2a8ff; }}

    .tkn-legend {{
      display: flex;
      align-items: center;
      gap: 6px;
      margin-top: 14px;
      font-size: 10px;
      color: #8b949e;
      flex-wrap: wrap;
    }}
    .tkn-swatch {{
      width: 11px;
      height: 11px;
      border-radius: 2px;
      display: inline-block;
      vertical-align: middle;
    }}
    .tkn-legend-item {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      margin-right: 4px;
    }}
    .tkn-detail-card {{
      margin-top: 20px;
    }}
    .tkn-strip-wrap {{
      display: flex;
      gap: 1px;
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 4px;
      padding: 4px;
      height: 28px;
      align-items: flex-end;
      overflow-x: auto;
    }}
    .tkn-strip-cell {{
      flex: 1 1 2px;
      min-width: 2px;
      height: 100%;
      border-radius: 1px;
      cursor: pointer;
    }}
    .tkn-strip-cell:hover {{
      transform: scaleY(1.15);
      outline: 1px solid #58a6ff;
    }}
    .tkn-strip-hours {{
      display: flex;
      justify-content: space-between;
      font-size: 9px;
      color: #6e7681;
      margin-top: 4px;
      padding: 0 4px;
    }}
    .tkn-detail-info {{
      margin-top: 8px;
      font-size: 0.82rem;
      color: #8b949e;
      min-height: 1.4em;
    }}
    .tkn-tooltip {{
      position: fixed;
      z-index: 9999;
      pointer-events: none;
      background: rgba(22, 27, 34, 0.95);
      border: 1px solid #30363d;
      box-shadow: 0 4px 12px rgba(0,0,0,0.5);
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 0.78rem;
      color: #c9d1d9;
      line-height: 1.35;
      max-width: 280px;
    }}
    .tkn-tt-title {{
      font-weight: 600;
      color: #f0f6fc;
      margin-bottom: 4px;
    }}
    .tkn-tt-row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
    }}
  </style>

  <script>
  (function() {{
    const raw = document.getElementById('token-heatmap-data');
    if (!raw) return;
    let data;
    try {{ data = JSON.parse(raw.textContent); }} catch (e) {{ return; }}
    if (!data || !data.dates) return;

    const dates = data.dates;
    const hourly = data.hourly || {{}};
    const fiveMin = data.five_min || {{}};
    const summary = data.summary || {{}};
    const qHourly = summary.quantiles_hourly || {{}};
    const q5m = summary.quantiles_5min || {{}};

    let activeDate = dates[dates.length - 1];
    for (let i = dates.length - 1; i >= 0; i--) {{
      if (hourly[dates[i]] !== null) {{
        activeDate = dates[i];
        break;
      }}
    }}

    function fmtTok(n) {{
      if (n === null || n === undefined) return 'n/a';
      if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
      if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
      if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
      return String(n);
    }}

    function getLevel(tok, q) {{
      if (tok === 0) return 0;
      if (!q || !q.length) return 1;
      if (tok <= q[0]) return 1;
      if (tok <= q[1]) return 2;
      if (tok <= q[2]) return 3;
      if (tok <= q[3]) return 4;
      return 5;
    }}

    const tooltip = document.getElementById('tkn-tooltip');
    function showTooltip(e, html) {{
      if (!tooltip) return;
      tooltip.innerHTML = html;
      tooltip.style.display = 'block';
      moveTooltip(e);
    }}
    function moveTooltip(e) {{
      if (!tooltip || tooltip.style.display === 'none') return;
      let x = e.clientX + 14;
      let y = e.clientY + 14;
      if (x + 290 > window.innerWidth) x = e.clientX - 290;
      if (y + 160 > window.innerHeight) y = e.clientY - 160;
      tooltip.style.left = x + 'px';
      tooltip.style.top = y + 'px';
    }}
    function hideTooltip() {{
      if (tooltip) tooltip.style.display = 'none';
    }}

    function renderMatrix(containerId, legendId, fieldIdx, qKey, lvlPrefix) {{
      const container = document.getElementById(containerId);
      if (!container) return;
      container.innerHTML = '';

      const hourCol = document.createElement('div');
      hourCol.className = 'tkn-hour-col';
      for (let h = 0; h < 24; h++) {{
        const lbl = document.createElement('div');
        lbl.className = 'tkn-hour-lbl';
        lbl.textContent = (h % 3 === 0) ? String(h).padStart(2, '0') : '';
        hourCol.appendChild(lbl);
      }}
      container.appendChild(hourCol);

      const q = qHourly[qKey] || [1000, 5000, 20000, 100000];

      dates.forEach(d => {{
        const col = document.createElement('div');
        col.className = 'tkn-day-col' + (d === activeDate ? ' active-col' : '');
        col.setAttribute('data-date', d);
        col.addEventListener('click', () => {{
          selectDate(d);
        }});

        const dayData = hourly[d];
        for (let h = 0; h < 24; h++) {{
          const cell = document.createElement('div');
          cell.className = 'tkn-cell';
          cell.setAttribute('data-date', d);
          cell.setAttribute('data-hour', h);

          if (dayData === null || dayData === undefined) {{
            cell.classList.add('cell-nodata');
            cell.addEventListener('mouseenter', (e) => {{
              showTooltip(e, '<div class="tkn-tt-title">' + d + ' (Unobserved)</div><div>No tracking record for this calendar day.</div>');
            }});
          }} else {{
            const row = dayData[h] || [0, 0, 0, 0, ''];
            const tok = row[fieldIdx] || 0;
            const lvl = getLevel(tok, q);
            if (lvl === 0) {{
              cell.classList.add('cell-zero');
            }} else {{
              cell.classList.add(lvlPrefix + '-' + lvl);
            }}
            cell.addEventListener('mouseenter', (e) => {{
              const hStr = String(h).padStart(2, '0') + ':00 MSK';
          const utcH = String((h - 3 + 24) % 24).padStart(2, '0');
              let html = '<div class="tkn-tt-title">' + d + ' ' + hStr + ' <span style="font-size:11px;color:#8b949e">(' + utcH + ':00 UTC)</span></div>';
              if (row[2] === 0) {{
                html += '<div>0 tokens (Quiet hour, 0 calls)</div>';
              }} else {{
                html += '<div class="tkn-tt-row"><span>Total:</span><span>' + fmtTok(row[2]) + '</span></div>';
                html += '<div class="tkn-tt-row"><span>Gateway:</span><span>' + fmtTok(row[1]) + '</span></div>';
                html += '<div class="tkn-tt-row"><span>Local (Qwen):</span><span>' + fmtTok(row[0]) + '</span></div>';
                html += '<div class="tkn-tt-row"><span>Calls:</span><span>' + row[3] + '</span></div>';
                if (row[4]) html += '<div class="tkn-tt-row"><span>Top:</span><span>' + row[4] + '</span></div>';
              }}
              showTooltip(e, html);
            }});
          }}
          cell.addEventListener('mousemove', moveTooltip);
          cell.addEventListener('mouseleave', hideTooltip);
          col.appendChild(cell);
        }}
        container.appendChild(col);
      }});

      const leg = document.getElementById(legendId);
      if (leg) {{
        leg.innerHTML = '';
        const items = [
          {{ label: 'No Data', cls: 'cell-nodata' }},
          {{ label: '0 tokens', cls: 'cell-zero' }},
          {{ label: '< ' + fmtTok(q[0]), cls: lvlPrefix + '-1' }},
          {{ label: fmtTok(q[0]) + '..' + fmtTok(q[1]), cls: lvlPrefix + '-2' }},
          {{ label: fmtTok(q[1]) + '..' + fmtTok(q[2]), cls: lvlPrefix + '-3' }},
          {{ label: fmtTok(q[2]) + '..' + fmtTok(q[3]), cls: lvlPrefix + '-4' }},
          {{ label: '> ' + fmtTok(q[3]), cls: lvlPrefix + '-5' }},
        ];
        items.forEach(it => {{
          const sp = document.createElement('span');
          sp.className = 'tkn-legend-item';
          sp.innerHTML = '<span class="tkn-swatch ' + it.cls + '"></span> ' + it.label;
          leg.appendChild(sp);
        }});
      }}
    }}

    function renderDayDetail(dateStr) {{
      const lbl = document.getElementById('tkn-active-date-label');
      if (lbl) lbl.textContent = dateStr;
      const wrap = document.getElementById('tkn-strip-wrap');
      const info = document.getElementById('tkn-detail-info');
      if (!wrap) return;
      wrap.innerHTML = '';

      const buckets = fiveMin[dateStr];
      const isMissing = (buckets === null || buckets === undefined);
      const q = q5m.gateway || [500, 2000, 8000, 50000];

      let dayTotal = 0, dayGw = 0, dayLoc = 0, dayCalls = 0;

      for (let b = 0; b < 288; b++) {{
        const cell = document.createElement('div');
        cell.className = 'tkn-strip-cell';
        const h = Math.floor(b / 12);
        const m = (b % 12) * 5;
        const timeStr = String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ' MSK';
        const utcH = String((h - 3 + 24) % 24).padStart(2, '0');

        if (isMissing) {{
          cell.classList.add('cell-nodata');
          cell.addEventListener('mouseenter', (e) => {{
            showTooltip(e, '<div class="tkn-tt-title">' + dateStr + ' ' + timeStr + '</div><div>No tracking data recorded for this day.</div>');
          }});
        }} else {{
          const bRow = buckets[String(b)];
          if (!bRow) {{
            cell.classList.add('cell-zero');
            cell.addEventListener('mouseenter', (e) => {{
              showTooltip(e, '<div class="tkn-tt-title">' + dateStr + ' ' + timeStr + '</div><div>0 tokens (quiet 5m)</div>');
            }});
          }} else {{
            const loc = bRow[0], gw = bRow[1], tot = bRow[2], calls = bRow[3], top = bRow[4];
            dayTotal += tot; dayGw += gw; dayLoc += loc; dayCalls += calls;
            const lvl = getLevel(gw > 0 ? gw : loc, q);
            cell.classList.add(gw > 0 ? ('gw-lvl-' + lvl) : ('loc-lvl-' + lvl));
            cell.addEventListener('mouseenter', (e) => {{
              let html = '<div class="tkn-tt-title">' + dateStr + ' ' + timeStr + ' <span style="font-size:11px;color:#8b949e">(' + utcH + ':' + String(m).padStart(2, '0') + ' UTC)</span></div>';
              html += '<div class="tkn-tt-row"><span>Total:</span><span>' + fmtTok(tot) + '</span></div>';
              html += '<div class="tkn-tt-row"><span>Gateway:</span><span>' + fmtTok(gw) + '</span></div>';
              html += '<div class="tkn-tt-row"><span>Local (Qwen):</span><span>' + fmtTok(loc) + '</span></div>';
              html += '<div class="tkn-tt-row"><span>Calls:</span><span>' + calls + '</span></div>';
              if (top) html += '<div class="tkn-tt-row"><span>Top:</span><span>' + top + '</span></div>';
              showTooltip(e, html);
            }});
          }}
        }}
        cell.addEventListener('mousemove', moveTooltip);
        cell.addEventListener('mouseleave', hideTooltip);
        wrap.appendChild(cell);
      }}

      if (info) {{
        if (isMissing) {{
          info.textContent = 'Date ' + dateStr + ' has no recorded LLM calls data (unobserved gap).';
        }} else {{
          info.textContent = dateStr + ' Summary: ' + fmtTok(dayTotal) + ' total tokens (' + fmtTok(dayGw) + ' gateway, ' + fmtTok(dayLoc) + ' local) across ' + dayCalls + ' calls.';
        }}
      }}
    }}

    function selectDate(d) {{
      activeDate = d;
      document.querySelectorAll('.tkn-day-col').forEach(col => {{
        if (col.getAttribute('data-date') === d) {{
          col.classList.add('active-col');
        }} else {{
          col.classList.remove('active-col');
        }}
      }});
      renderDayDetail(d);
    }}

    renderMatrix('matrix-gw', 'legend-gw', 1, 'gateway', 'gw-lvl');
    renderMatrix('matrix-loc', 'legend-loc', 0, 'local', 'loc-lvl');
    renderMatrix('matrix-tot', 'legend-tot', 2, 'total', 'tot-lvl');
    renderDayDetail(activeDate);

    document.querySelectorAll('.tkn-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.tkn-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const view = btn.getAttribute('data-view');
        const cardGw = document.getElementById('tkn-card-gw');
        const cardLoc = document.getElementById('tkn-card-loc');
        const cardTot = document.getElementById('tkn-card-tot');

        if (view === 'split') {{
          if (cardGw) cardGw.style.display = 'block';
          if (cardLoc) cardLoc.style.display = 'block';
          if (cardTot) cardTot.style.display = 'none';
        }} else if (view === 'gateway') {{
          if (cardGw) cardGw.style.display = 'block';
          if (cardLoc) cardLoc.style.display = 'none';
          if (cardTot) cardTot.style.display = 'none';
        }} else if (view === 'local') {{
          if (cardGw) cardGw.style.display = 'none';
          if (cardLoc) cardLoc.style.display = 'block';
          if (cardTot) cardTot.style.display = 'none';
        }} else if (view === 'total') {{
          if (cardGw) cardGw.style.display = 'none';
          if (cardLoc) cardLoc.style.display = 'none';
          if (cardTot) cardTot.style.display = 'block';
        }}
      }});
    }});
  }})();
  </script>
</section>
"""


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
    .feed-age-badge {
      display: inline-block;
      padding: 1px 6px;
      border-radius: 3px;
      font-size: 0.85em;
      border: 1px solid #234234;
      background: #0d1a15;
    }
    .feed-badge-stale, .feed-badge-corrupt, .feed-badge-missing, .feed-badge-unreadable {
      border-color: #8c2a2a;
      color: #ff9999;
      background: #2b1111;
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
    .health-verdict {
      display: flex;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 8px;
      padding: 8px 12px;
      margin-bottom: 10px;
      border-left: 4px solid #7f8c8d;
      border-radius: 3px;
      background: rgba(127, 140, 141, 0.12);
      font-size: 0.9em;
    }
    .health-verdict .now-label {
      min-width: 0;
    }
    .health-verdict strong {
      letter-spacing: 1px;
    }
    .health-verdict-reason {
      color: #b8d0c2;
      font-size: 0.85em;
    }
    .health-healthy {
      border-left-color: #4caf7d;
      background: rgba(76, 175, 125, 0.12);
    }
    .health-healthy strong {
      color: #6fd39b;
    }
    .health-degraded {
      border-left-color: #d9a441;
      background: rgba(217, 164, 65, 0.12);
    }
    .health-degraded strong {
      color: #e8bf6a;
    }
    .health-investigate {
      border-left-color: #d1544f;
      background: rgba(209, 84, 79, 0.14);
    }
    .health-investigate strong {
      color: #ea7f7a;
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
    .agent-subcol { min-width: 0; max-width: 100%; width: 100%; overflow: hidden; }
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
    .lesson-duplicate-warning { color: #e06c75; border: 1px solid #b23a3a; padding: 1px 5px; margin-left: 6px; font-size: .72em; text-transform: uppercase; }
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
    .agent-wide-content { max-width: 100%; overflow-x: auto; }
    .agent-wide-content pre, .agent-wide-content .skills-table { min-width: max-content; }
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
    .lineage-day-group { padding: 4px 12px 10px; overflow-x: auto; }
    .lineage-filter-note { color: #d19a66; font-size: .8rem; }
    .lineage-coverage-note { margin: 2px 12px 8px; color: #8aa695; font-size: .72rem; line-height: 1.35; overflow-wrap: anywhere; }
    .lineage-filter-note[hidden] { display: none; }
    .lineage-day-group[hidden] { display: none; }
    .lineage-day-group h3 { color: #56d364; font-size: .8rem; margin: 4px 0; }
    /* #208: no max-width — a wide day scrolls inside .lineage-day-group instead of
       being scaled down until r=9 circles are 4 px and the labels unreadable. */
    .lineage-day-svg { display: block; height: auto; overflow: visible; }
    .lineage-node { fill: #2fd3c4; stroke: #dcebe1; stroke-width: 2; }
    .arch-node.arch-integrated { fill: #2fd3c4; stroke: #dcebe1; }
    .arch-node.arch-skipped { fill: #1a3328; stroke: #5a7a68; stroke-dasharray: 3 2; }
    .arch-node.arch-partial { fill: #46381e; stroke: #d19a66; stroke-dasharray: 4 2; }
    .arch-node.arch-failed { fill: #4a1d24; stroke: #e06c75; stroke-dasharray: 2 2; }
    .lineage-edge { fill: none; stroke: #2f5c46; stroke-width: 2; }
    .lineage-legend { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; margin: 4px 12px 10px; padding: 6px 10px; background: rgba(8, 17, 12, 0.6); border: 1px solid #1e3b2b; border-radius: 4px; font-size: 0.76rem; color: #8aa695; font-family: 'Consolas', monospace; }
    .lineage-legend-group { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; max-width: 100%; }
    .lineage-legend-title { color: #5a7a68; text-transform: uppercase; font-size: 0.68rem; letter-spacing: 0.5px; }
    .lineage-legend-item { display: inline-flex; align-items: center; gap: 5px; color: #b8d0c2; }
    .lineage-legend-swatch { display: inline-block; vertical-align: middle; }
    .lineage-legend-edge { fill: none; stroke: #2f5c46; stroke-width: 2; }
    .lineage-legend-edge-inferred { stroke-dasharray: 6 5; }
    .lineage-legend-node { stroke-width: 2; }
    .lineage-legend .arch-node { cursor: default; pointer-events: none; }
    .lineage-hidden-parent { fill: #d19a66; font-size: 10px; }
    .lineage-context-node { fill: #263c31 !important; stroke: #d19a66 !important; stroke-width: 3; stroke-dasharray: 2 2; opacity: .9; }
    .lineage-context-edge { stroke: #d19a66 !important; stroke-dasharray: 4 3; opacity: .9; }
    .lineage-collapsed-label { fill: #d19a66; font-size: 10px; pointer-events: none; }
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
    .arch-star { fill: #56d364; color: #56d364; font-size: 14px; }
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
    ledger_history = data.get('ledger_history')
    demand_rotation = data.get('demand_rotation')
    demand_completed = data.get('demand_completed')
    skill_reads = data.get('skill_reads')
    skill_evals = data.get('skill_evals')
    goal_text = data.get('goal_text')
    agents_md = data.get('agents_md')
    cycle_titles = data.get('cycle_titles')
    # Issue #172: build cycle_details from ledger_history (full history) rather than ledger_tail
    cycle_details = build_cycle_details(
        ledger_history if ledger_history is not None else ledger_tail,
        evolution_tree,
        data.get('lessons'),
        data.get('reflections'),
        cycle_titles,
        data.get('cycle_files'),
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
        age_seconds=age_seconds if isinstance(age_seconds, (int, float)) else None,
        now=generated_at,
        proposer_llm_unavailable=bool(isinstance(data.get('proposer_stats'), dict) and data['proposer_stats'].get('llm_unavailable')),
        health_last_integrated_ts=data.get('health_last_integrated_ts'),
        health_recent_outcomes=data.get('health_recent_outcomes'),
        bridge_exit_streak=data.get('bridge_exit_streak'),
        bridge_exits=data.get('bridge_exits'),
        scorecard=scorecard,
        strategist_decisions=data.get('strategist_decisions'),
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

    rendered_lesson_ids = {
        str(lesson.get('id')) for lesson in (data.get('lessons') or [])
        if isinstance(lesson, dict) and lesson.get('id')
    }
    lessons_panel = build_lessons_panel(data.get('lessons'))
    cycle_feed = build_cycle_feed(
        ledger_tail=ledger_tail,
        demand_completed=demand_completed,
        task_titles=cycle_titles,
        evolution_tree=evolution_tree,
        cycle_files=data.get('cycle_files'),
        llm_stats=data.get('llm_stats'),
    )
    hypotheses_panel = build_hypotheses_panel(
        hypotheses,
        feed_cycles=feed_cycles,
        hypotheses_durable=hypotheses_durable,
        demand_futility=data.get('demand_futility'),
        scorecard=data.get('scorecard'),
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
        agent_context=data.get('agent_context'),
    )

    return PAGE_TEMPLATE.format(
        css=CSS + '\n' + AGENT_CONTEXT_CSS,
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
    ('tokens.html', 'tokens'),
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
        css=CSS + '\n' + AGENT_CONTEXT_CSS,
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
    # Issue #175: 3-state reporting (value N, value 0, or unavailable).
    if ledger_tail is None:
        cycles_teaser = 'unavailable'
    else:
        cycle_ids = {
            str(r.get('cycle_id')) for r in ledger_tail
            if isinstance(r, dict) and r.get('cycle_id')
        }
        cycles_teaser = f'{len(cycle_ids)} cycles tracked in the recent ledger window'

    if evolution_tree is None or not isinstance(evolution_tree, dict):
        lineage_teaser = 'unavailable'
    else:
        nodes = evolution_tree.get('nodes')
        node_count = len(nodes) if isinstance(nodes, dict) else 0
        lineage_teaser = f'{node_count} evolution nodes'

    if hypotheses is None or not isinstance(hypotheses, dict):
        hypotheses_teaser = 'unavailable'
    else:
        active = answered = 0
        if isinstance(hypotheses.get('entries'), dict):
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
        hypotheses_teaser = f'{active} active / {answered} answered + {durable_count} strategist durable'

    token_heatmap = data.get('token_heatmap')
    if token_heatmap and isinstance(token_heatmap, dict) and token_heatmap.get('summary'):
        tot = token_heatmap['summary'].get('total_tokens', 0)
        days = token_heatmap['summary'].get('days_present', 0)
        tokens_teaser = f'{fmt_tokens(tot)} across {days} recorded days'
    else:
        tokens_teaser = 'unavailable'

    return f'''
    <section class="panel panel-teasers">
      <h2 class="panel-title">Explore</h2>
      <ul class="teaser-list">
        <li><a href="cycles.html">cycles</a> &mdash; {cycles_teaser}</li>
        <li><a href="lineage.html">lineage</a> &mdash; {lineage_teaser}</li>
        <li><a href="tokens.html">tokens</a> &mdash; {tokens_teaser}</li>
        <li><a href="hypotheses.html">hypotheses</a> &mdash; {hypotheses_teaser}</li>
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
    ledger_history = data.get('ledger_history')
    demand_rotation = data.get('demand_rotation')
    demand_completed = data.get('demand_completed')
    skill_reads = data.get('skill_reads')
    skill_evals = data.get('skill_evals')
    goal_text = data.get('goal_text')
    agents_md = data.get('agents_md')
    cycle_titles = data.get('cycle_titles')
    # Issue #172: build cycle_details from ledger_history (full history) rather than ledger_tail
    cycle_details = build_cycle_details(
        ledger_history if ledger_history is not None else ledger_tail,
        evolution_tree,
        data.get('lessons'),
        data.get('reflections'),
        cycle_titles,
        data.get('cycle_files'),
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
        age_seconds=age_seconds,
        now=generated_at,
        proposer_llm_unavailable=bool(isinstance(data.get('proposer_stats'), dict) and data['proposer_stats'].get('llm_unavailable')),
        health_last_integrated_ts=data.get('health_last_integrated_ts'),
        health_recent_outcomes=data.get('health_recent_outcomes'),
        bridge_exit_streak=data.get('bridge_exit_streak'),
        bridge_exits=data.get('bridge_exits'),
        scorecard=scorecard,
        strategist_decisions=data.get('strategist_decisions'),
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
    rendered_lesson_ids = {
        str(lesson.get('id')) for lesson in (data.get('lessons') or [])
        if isinstance(lesson, dict) and lesson.get('id')
    }
    cycle_feed = build_cycle_feed(
        ledger_tail=history_source,
        demand_completed=demand_completed,
        task_titles=cycle_titles,
        evolution_tree=evolution_tree,
        cycle_files=data.get('cycle_files'),
        llm_stats=data.get('llm_stats'),
        history_mode=True,
        rendered_lesson_ids=rendered_lesson_ids,
        ledger_history=history_rows if isinstance(history_rows, list) and history_rows else None,
    )
    hypotheses_panel = build_hypotheses_panel(
        hypotheses,
        feed_cycles=feed_cycles,
        hypotheses_durable=hypotheses_durable,
        demand_futility=data.get('demand_futility'),
        scorecard=data.get('scorecard'),
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
        agent_context=data.get('agent_context'),
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
        # #208 step 8: the cycle-details records travel as a sibling static JSON
        # (published and written by the same loops as the pages), fetched by
        # lineage.html on the first node click. Nothing is filtered out.
        LINEAGE_DETAILS_FILE: json.dumps(cycle_details or {}, ensure_ascii=True, separators=(',', ':')),
        'cycles.html': _page('eeebot / cycles', 'cycles.html', cycle_feed),
        'tokens.html': _page('eeebot / tokens', 'tokens.html', build_tokens_panel(data, host, generated_at)),
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
    # (#208: the former "copy vendor files when a page references assets/vendor/"
    # block was dead — the renderer is inlined and no page ever carried that path.)

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
        for name in ('lineage-renderer.js',):
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
