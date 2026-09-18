"""Issue #227: Two-tier agent context model reader and renderer.
Visualizes:
- Tier 1: In active context (system prompt sections in assembly order, history, user)
- Tier 2: Reachable on disk (skills, lessons, memory)
Zero import of `nanobot` runtime; uses recorded data and filesystem.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

MSK_TZ = timezone(timedelta(hours=3))

# ozand/eeebot#1755: window-pressure threshold and lookback window. A p99
# above this fraction of the model's own context window is flagged; the
# healthy value is stated inline wherever this constant is rendered.
WINDOW_PRESSURE_HOURS = 24
WINDOW_PRESSURE_THRESHOLD_PCT = 80.0

SEPARATOR = "\n\n---\n\n"
SEPARATOR_LEN = len(SEPARATOR)  # 7 characters

# #301 (ADR-022, ozand/eeebot#1720): static owner map for prompt sections.
# Source of truth: nanobot/agent/context.py `_RELEASE_BLOCK_CAPS` +
# `MUTATION_POLICY.read_paths` (eeebot repo) — release-owned ontology files
# (IDENTITY.md/SOUL.md/goals.md/USER.md/OPERATING.md), instance-owned
# AGENTS.md, and the generated skills/memory/runtime blocks. Pre-migration
# ledger rows still carry the old "bootstrap"/"active_skills" names, kept
# here as aliases so old rows render with real owner/cap info too. Any
# section name absent from this map falls through to "unmapped" — never
# dropped.
SECTION_OWNER_MAP: dict[str, dict[str, Any]] = {
    "identity": {"file": "IDENTITY.md", "owner": "release", "cap": 1500, "label": "Identity & Role"},
    "soul": {"file": "SOUL.md", "owner": "release", "cap": 1800, "label": "Soul"},
    "goals": {"file": "goals.md", "owner": "release", "cap": 3200, "label": "Operator Charter / Goals"},
    "user": {"file": "USER.md", "owner": "release", "cap": 4000, "label": "User"},
    "operating": {"file": "OPERATING.md", "owner": "release", "cap": 5000, "label": "Operating"},
    "agents": {"file": "AGENTS.md", "owner": "instance", "cap": 4000, "label": "Bootstrap (AGENTS.md)"},
    "skills_catalogue": {"file": "skills index", "owner": "generated", "cap": None, "label": "Skills Catalogue (Index)"},
    "memory": {"file": "memory index", "owner": "generated", "cap": None, "label": "Working Memory"},
    "runtime": {"file": "runtime facts", "owner": "generated", "cap": None, "label": "Runtime"},
    # Pre-migration aliases (old ledger rows, before ADR-022):
    "bootstrap": {"file": "AGENTS.md", "owner": "instance", "cap": 4000, "label": "Bootstrap (AGENTS.md)"},
    "active_skills": {"file": "active skills (loop-owned)", "owner": "generated", "cap": None, "label": "Active Skills (Always Loaded)"},
}

UNMAPPED_SECTION_META: dict[str, Any] = {"file": "unmapped", "owner": "unmapped", "cap": None, "label": None}


def section_owner_meta(name: str) -> dict[str, Any]:
    """Look up the static owner/file/cap for a recorded section name.

    Unknown names (future sections the map hasn't caught up with yet) fall
    through to "unmapped" with their chars still rendered — never dropped.
    """
    return SECTION_OWNER_MAP.get(name, UNMAPPED_SECTION_META)


# #301: rule fingerprints the harness test tracks (see the issue's code map)
# and which prompt block owns each rule today. Static until the harness
# publishes a `rule_owners` telemetry field alongside the fingerprint test
# (ozand/eeebot#1725) — the page prefers that field when present and labels
# this table "static" otherwise so the distinction stays visible.
RULE_OWNERS_STATIC: dict[str, str] = {
    "skip": "agents",
    "surface": "agents",
    "branch": "agents",
    "runner": "agents",
    "identity": "identity",
    "budget": "operating",
    "final_json": "operating",
}


def esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def estimate_tokens(chars: int) -> int:
    """Heuristic estimator: ~4 characters per token for mixed code/English prompts."""
    return max(1, chars // 4) if chars > 0 else 0


def _flagged_names(row: dict[str, Any], key: str) -> set[str]:
    """Block/file names a ledger row lists under ``truncated`` or ``dropped``.

    Both keys have carried either a plain list of names or a list of dicts
    (``{"name": ..., "chars": ...}``) across schema versions -- accept both,
    never dropping a real entry just because its shape changed.
    """
    names: set[str] = set()
    for item in row.get(key) or []:
        if isinstance(item, dict):
            name = item.get("name") or item.get("file")
        else:
            name = item
        if name:
            names.add(str(name))
    return names


def compute_truncation_streak(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    """ozand/eeebot#1755 (4a): consecutive-cycle streak for truncated/dropped blocks.

    ``rows`` must be ALL recorded ``phase: system_prompt`` ledger rows, oldest
    first -- a single latest row cannot answer "how many cycles in a row".
    For each block name present in the truncated/dropped list of the MOST
    RECENT row, walk backward counting consecutive rows (starting at the
    latest) that also list that name under the same key. The walk stops at
    the first row that does not list the name: if a block was truncated, then
    healthy for a cycle, then truncated again, the streak counts only the
    run since it most recently started being truncated again -- it does NOT
    add the two runs together. This is a deliberate judgment call: an
    interrupted streak is evidence the degradation is intermittent, not a
    50-cycle continuous failure, and inflating it would misstate severity.

    Returns a dict:
      - ``status``: "no_data" (no ledger rows read at all -- must render
        distinctly from "0", never as a fabricated healthy zero),
        "healthy" (rows read, latest row's truncated/dropped both empty),
        or "alarm" (latest row has at least one truncated/dropped name).
      - ``total_rows``: how many system_prompt rows the streak was computed
        over (evidence for how far back "consecutive" reaches).
      - ``entries``: list of {"kind": "truncated"|"dropped", "name", "streak"}
        for every name present in the latest row, streak counted as above.
    """
    if not rows:
        return {"status": "no_data", "total_rows": 0, "entries": []}
    valid_rows = [row for row in rows if isinstance(row, dict)]
    if not valid_rows:
        return {"status": "no_data", "total_rows": 0, "entries": []}
    total_rows = len(valid_rows)
    latest = valid_rows[-1]
    entries: list[dict[str, Any]] = []
    for kind in ("truncated", "dropped"):
        for name in sorted(_flagged_names(latest, kind)):
            streak = 0
            for row in reversed(valid_rows):
                if name in _flagged_names(row, kind):
                    streak += 1
                else:
                    break
            entries.append({"kind": kind, "name": name, "streak": streak})
    status = "alarm" if entries else "healthy"
    return {"status": status, "total_rows": total_rows, "entries": entries}


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile over an already-ascending-sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (pct / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    if lo == hi:
        return sorted_values[lo]
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def compute_window_pressure(
    rows: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
    window_hours: int = WINDOW_PRESSURE_HOURS,
) -> dict[str, Any]:
    """ozand/eeebot#1755 (4b): p99 of prompt_tokens / context_window, last 24h.

    ``rows`` are raw ``llm_calls`` rows (any component, any day file the
    caller chose to read); this function does the time-window filtering
    itself using each row's own ``ts`` (UTC ISO8601).

    A row with ``context_window`` absent or ``null`` is NEVER treated as 0
    (which would silently zero the ratio and hide real pressure) and NEVER
    assumed to equal any fallback default -- it is excluded from the p99
    input and counted in ``unknown_rows`` instead. Rows missing ``prompt_tokens``
    or an unparseable ``ts`` are excluded from consideration entirely (neither
    known nor unknown -- they are not evidence about window pressure at all).

    Returns a dict:
      - ``status``: "no_data" (no llm_calls rows fell in the window at all),
        "unknown" (rows exist but none have a usable context_window --
        e.g. before the harness PR lands, or for unmapped models),
        or "measured" (at least one row has both prompt_tokens and
        context_window).
      - ``rows_in_window``: rows in the last `window_hours` with usable
        prompt_tokens (known + unknown).
      - ``known_rows`` / ``unknown_rows``: split of the above by whether
        context_window was resolvable.
      - ``p99_pct``: p99 of (prompt_tokens / context_window * 100) over the
        known rows only, or None if there are no known rows.
      - ``threshold_pct``: the healthy-vs-alarm cutoff (80.0), carried on the
        result so renderers never hard-code it separately.
    """
    result = {
        "status": "no_data",
        "rows_in_window": 0,
        "known_rows": 0,
        "unknown_rows": 0,
        "p99_pct": None,
        "threshold_pct": WINDOW_PRESSURE_THRESHOLD_PCT,
    }
    if not rows:
        return result
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    ratios: list[float] = []
    unknown = 0
    considered = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts_raw = row.get("ts")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff or ts > now:
            continue
        prompt_tokens = row.get("prompt_tokens")
        if not isinstance(prompt_tokens, (int, float)) or isinstance(prompt_tokens, bool):
            continue
        considered += 1
        context_window = row.get("context_window")
        if not isinstance(context_window, (int, float)) or isinstance(context_window, bool) or context_window <= 0:
            # Absent/null/non-positive context_window: excluded from the
            # ratio, counted separately -- never coerced to 0 or a guess.
            unknown += 1
            continue
        ratios.append((prompt_tokens / context_window) * 100.0)
    result["rows_in_window"] = considered
    result["unknown_rows"] = unknown
    result["known_rows"] = len(ratios)
    if considered == 0:
        result["status"] = "no_data"
        return result
    if not ratios:
        result["status"] = "unknown"
        return result
    result["status"] = "measured"
    result["p99_pct"] = _percentile(sorted(ratios), 99)
    return result


def parse_prompt_sections(
    prompt_text: str | None,
    recorded_sections: dict[str, Any] | None,
) -> dict[str, Any]:
    """Split the recorded prompt structurally, never by section headings.

    ``ContextBuilder`` joins non-empty capped sections in canonical order and
    the loop appends operator-owned ``system_context`` afterwards.  The ledger
    records the former, so the first non-empty recorded sections map
    positionally; remaining chunks are outside the cap (charter, identity,
    etc.).  Length mismatches remain evidence instead of being repaired.
    """
    if not prompt_text or not isinstance(recorded_sections, dict):
        return {"status": "unavailable", "sections": {}, "outside_cap": [], "mismatches": []}

    # #301: chunk positions follow the RECORDED order of `sections` (however
    # the harness named/ordered them that day), not a hard-coded canonical
    # list — ``_join_sections`` in the harness joins non-empty sections in
    # the order it built them, whatever that order is.
    capped_names = [
        name for name in recorded_sections
        if (recorded_sections.get(name) or 0) > 0
    ]
    chunks = prompt_text.split(SEPARATOR)
    parsed: dict[str, dict[str, Any]] = {}
    mismatches: list[dict[str, Any]] = []
    for index, name in enumerate(capped_names):
        recorded = int(recorded_sections[name])
        text = chunks[index] if index < len(chunks) else None
        actual = len(text) if text is not None else None
        parsed[name] = {"text": text, "recorded_chars": recorded, "actual_chars": actual}
        if actual != recorded:
            mismatches.append({"name": name, "recorded_chars": recorded, "actual_chars": actual})

    outside_chunks = chunks[len(capped_names):]
    outside_cap: list[dict[str, Any]] = []
    for index, text in enumerate(outside_chunks):
        if index == 0 and "\n\n# Loop agent identity\n\n" in text:
            goals_text, identity_text = text.split("\n\n# Loop agent identity\n\n", 1)
            outside_cap.extend([
                {"name": "goals", "text": goals_text, "actual_chars": len(goals_text)},
                {"name": "loop_identity", "text": "\n\n# Loop agent identity\n\n" + identity_text, "actual_chars": len(identity_text) + len("\n\n# Loop agent identity\n\n")},
            ])
        else:
            outside_cap.append({
                "name": "goals" if index == 0 else "loop_identity" if index == 1 else f"outside_cap_{index + 1}",
                "text": text,
                "actual_chars": len(text),
            })
    expected_total = sum(int(recorded_sections[name]) for name in capped_names)
    expected_total += max(0, len(capped_names) - 1) * SEPARATOR_LEN
    status = "exact" if not mismatches else "mismatch"
    if len(chunks) < len(capped_names):
        status = "mismatch"
    return {
        "status": status,
        "sections": parsed,
        "outside_cap": outside_cap,
        "mismatches": mismatches,
        "recorded_capped_chars": expected_total,
        "actual_prompt_chars": len(prompt_text),
    }


def build_task_sections(task_text: str | None) -> list[dict[str, Any]]:
    """#301: the user message's own section list, built the same way
    ``build_task`` in the harness builds it — one entry per top-level
    ``## `` heading, with the chars of the body that follows it up to the
    next ``## `` heading (or end of text). A heading seen more than once is
    flagged ``duplicate`` so repeated-section defects are visible on the
    page rather than only in a ledger dump.
    """
    if not task_text:
        return []
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", task_text, re.MULTILINE))
    if not matches:
        return []
    seen: dict[str, int] = {}
    sections: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(task_text)
        body_chars = end - start
        seen[heading] = seen.get(heading, 0) + 1
        sections.append({"heading": heading, "chars": body_chars, "occurrence": seen[heading]})
    for section in sections:
        section["duplicate"] = seen[section["heading"]] > 1
    return sections


def corpus_status(path: Path, *, read_error: bool = False) -> str:
    if read_error:
        return "unavailable"
    if not path.exists():
        return "missing"
    return "present"


def read_dir_files(root: Path, matcher: Any) -> tuple[list[Path], str]:
    if not root.exists():
        return [], "missing"
    if not root.is_dir():
        return [], "unavailable"
    try:
        files = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and matcher(path):
                files.append(path)
        return files, "present"
    except (OSError, UnicodeError):
        return [], "unavailable"


def read_lesson_corpus(inst_path: Path | None) -> dict[str, Any]:
    """#274: single source of truth for the live lesson-file count.

    ``lessons.html`` and ``agent.html`` reported four disagreeing counts for
    one corpus because each page counted it a different way. Both now call
    this function so they cannot disagree again.
    """
    lessons_dir = inst_path / "lessons" if inst_path else Path("__missing_instance_repo__/lessons")
    lesson_files, status = read_dir_files(
        lessons_dir, lambda path: path.suffix == ".md" and path.name != "index.md",
    )
    index_status = corpus_status(lessons_dir / "index.md")
    files: list[dict[str, Any]] = []
    total_size = 0
    for path in lesson_files:
        try:
            size = path.stat().st_size
            total_size += size
            rel = path.relative_to(inst_path).as_posix() if inst_path else path.name
            files.append({"name": rel, "size_bytes": size})
        except (OSError, ValueError):
            status = "unavailable"
    return {
        "index_status": index_status,
        "corpus_status": status,
        "corpus_count": len(files),
        "total_size_bytes": total_size,
        "files": files,
    }


def read_agent_context_dict(state_root: Path, instance_repo: Path | str | None = None) -> dict[str, Any]:
    """Read context telemetry and Tier 2 corpus from the instance workspace.

    ``state_root`` contains runtime telemetry; the corpus writer is the
    instance repository.  Never fall back to the runtime/release tree: a
    missing or failed corpus read must stay visible as a state, not become a
    fabricated empty count.
    """
    state_root = Path(state_root)
    inst_path = Path(instance_repo) if instance_repo else None

    # 1. Scan ledger/cycles.jsonl for every system_prompt row (oldest first).
    # ozand/eeebot#1755 (4a): the streak needs the whole recorded history in
    # this file, not just the latest cycle -- see compute_truncation_streak.
    lpath = state_root / "ledger" / "cycles.jsonl"
    sys_prompt_rows: list[dict[str, Any]] = []
    ledger_read_failed = False
    if lpath.is_file():
        try:
            with lpath.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and obj.get("phase") == "system_prompt":
                        sys_prompt_rows.append(obj)
        except Exception:
            ledger_read_failed = True
    sys_prompt_row: dict[str, Any] | None = sys_prompt_rows[-1] if sys_prompt_rows else None
    truncation_streak = (
        {"status": "no_data", "total_rows": 0, "entries": []}
        if ledger_read_failed
        else compute_truncation_streak(sys_prompt_rows)
    )

    prompt_text: str | None = None
    task_text: str | None = None
    cid = sys_prompt_row.get("cycle_id") if sys_prompt_row else None

    prompts_dir = state_root / "prompts"
    if prompts_dir.is_dir():
        if cid:
            p_file = prompts_dir / f"{cid}.system.txt"
            t_file = prompts_dir / f"{cid}.task.txt"
            if p_file.is_file():
                try:
                    prompt_text = p_file.read_text(encoding="utf-8", errors="replace")[:150000]
                except Exception:
                    pass
            if t_file.is_file():
                try:
                    task_text = t_file.read_text(encoding="utf-8", errors="replace")[:50000]
                except Exception:
                    pass

        if prompt_text is None:
            try:
                p_files = [f for f in prompts_dir.iterdir() if f.name.endswith(".system.txt")]
                if p_files:
                    p_files.sort(key=lambda f: f.stat().st_mtime)
                    latest_p = p_files[-1]
                    prompt_text = latest_p.read_text(encoding="utf-8", errors="replace")[:150000]
                    t_cand = prompts_dir / f"{latest_p.name[:-11]}.task.txt"
                    if t_cand.is_file():
                        task_text = t_cand.read_text(encoding="utf-8", errors="replace")[:50000]
            except Exception:
                pass

    # 2. Tier 2: Skills
    # Tier 2 is written by the instance workspace, not the runtime release tree.
    skills_list: list[dict[str, Any]] = []
    skills_files, skills_status = read_dir_files(
        inst_path / "skills" if inst_path else Path("__missing_instance_repo__/skills"),
        lambda path: path.name == "SKILL.md",
    )
    for s_file in skills_files:
        try:
            content = s_file.read_text(encoding="utf-8", errors="replace")[:15000]
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            desc = next((line for line in lines if not line.startswith("#") and len(line) > 10), "")
            rel = s_file.relative_to(inst_path).as_posix()
            skills_list.append({
                "name": s_file.parent.name,
                "size_bytes": s_file.stat().st_size,
                "desc": desc[:200],
                "content": content,
                "path": rel,
            })
        except (OSError, UnicodeError, ValueError):
            skills_status = "unavailable"

    lesson_corpus = read_lesson_corpus(inst_path)

    memory_dir = inst_path / "memory" if inst_path else Path("__missing_instance_repo__/memory")
    memory_paths, memory_status = read_dir_files(memory_dir, lambda path: True)
    memory_index_path = memory_dir / "index.md"
    memory_index_status = corpus_status(memory_index_path)
    memory_files: list[dict[str, Any]] = []
    total_memory_size = 0
    for path in memory_paths:
        try:
            rel = path.relative_to(inst_path).as_posix()
            size = path.stat().st_size
            total_memory_size += size
            memory_files.append({"name": rel, "size_bytes": size})
        except (OSError, ValueError):
            memory_status = "unavailable"

    return {
        "system_prompt": sys_prompt_row,
        "truncation_streak": truncation_streak,
        "prompt_text": prompt_text,
        "task_text": task_text,
        "tier2_skills": skills_list,
        "tier2_skills_status": skills_status,
        "tier2_lessons": {
            **lesson_corpus,
            "files": lesson_corpus["files"][:50],
        },
        "tier2_memory": {
            "index_status": memory_index_status,
            "corpus_status": memory_status,
            "total_files": len(memory_files),
            "total_size_bytes": total_memory_size,
            "files": memory_files[:50],
        },
    }


def _render_truncation_streak_html(streak: dict[str, Any]) -> str:
    """ozand/eeebot#1755 (4a): render the truncation/drop streak indicator.

    Renders three distinguishable states -- "no data" must never look like a
    healthy zero, and a live alarm must be unmissable (danger badge with the
    block name(s) and consecutive-cycle count spelled out inline).
    """
    status = streak.get("status", "no_data")
    total_rows = streak.get("total_rows", 0) or 0
    entries = streak.get("entries") or []
    if status == "no_data":
        return (
            '<div class="context-indicator-box indicator-no-data">'
            '<span class="context-badge badge-secondary">TRUNCATION/DROP STREAK: NO DATA</span>'
            ' <span class="indicator-note">ledger unreadable or no <code>phase: system_prompt</code> rows recorded — '
            'not the same as a healthy 0, healthy: 0 consecutive cycles.</span></div>'
        )
    if status != "alarm" or not entries:
        return (
            '<div class="context-indicator-box indicator-healthy">'
            '<span class="context-badge badge-success">TRUNCATION/DROP STREAK: HEALTHY (0)</span>'
            f' <span class="indicator-note">0 consecutive cycles truncated or dropped, healthy: 0 · {total_rows:,} recorded cycle(s) checked.</span></div>'
        )
    kind_label = {"truncated": "TRUNCATED", "dropped": "DROPPED"}
    pills = []
    for entry in entries:
        kind = entry.get("kind", "truncated")
        name = entry.get("name", "unknown")
        entry_streak = entry.get("streak", 0)
        label = kind_label.get(kind, kind.upper())
        pills.append(
            f'<span class="dropped-pill">{esc(label)} {entry_streak:,}/{total_rows:,} consecutive cycles: {esc(name)}</span>'
        )
    return (
        '<div class="context-indicator-box indicator-alarm">'
        '<span class="context-badge badge-danger">TRUNCATION/DROP STREAK: ALARM</span>'
        f' <span class="indicator-note">healthy: 0 consecutive cycles &bull; {total_rows:,} recorded cycle(s) checked.</span>'
        f'<div class="indicator-pills">{" ".join(pills)}</div></div>'
    )


def _render_window_pressure_html(pressure: dict[str, Any]) -> str:
    """ozand/eeebot#1755 (4b): render the 24h window-pressure indicator.

    A row with no ``context_window`` is never folded into the p99 as 0 (that
    would fake a healthy reading) and never assumed to equal any hard-coded
    default window size -- it is counted and shown separately as
    "window unknown: N rows".
    """
    status = pressure.get("status", "no_data")
    threshold = pressure.get("threshold_pct", WINDOW_PRESSURE_THRESHOLD_PCT)
    rows_in_window = pressure.get("rows_in_window", 0) or 0
    unknown_rows = pressure.get("unknown_rows", 0) or 0
    known_rows = pressure.get("known_rows", 0) or 0
    if status == "no_data":
        return (
            '<div class="context-indicator-box indicator-no-data">'
            '<span class="context-badge badge-secondary">WINDOW PRESSURE (24H): NO DATA</span>'
            f' <span class="indicator-note">no llm_calls rows in the last {WINDOW_PRESSURE_HOURS}h — '
            f'not the same as a healthy 0%, healthy: &le; {threshold:.0f}%.</span></div>'
        )
    if status == "unknown":
        return (
            '<div class="context-indicator-box indicator-no-data">'
            '<span class="context-badge badge-secondary">WINDOW PRESSURE (24H): UNKNOWN</span>'
            f' <span class="indicator-note">window pressure: unknown ({unknown_rows:,}/{rows_in_window:,} rows have no <code>context_window</code>) — '
            f'excluded from the ratio rather than assumed 0%, healthy: &le; {threshold:.0f}%.</span></div>'
        )
    p99 = pressure.get("p99_pct")
    p99_display = f"{p99:.1f}%" if isinstance(p99, (int, float)) else "unavailable"
    is_alarm = isinstance(p99, (int, float)) and p99 > threshold
    badge_class = "badge-danger" if is_alarm else "badge-success"
    verdict = "OVER THRESHOLD" if is_alarm else "WITHIN BUDGET"
    return (
        '<div class="context-indicator-box indicator-window-pressure">'
        f'<span class="context-badge {badge_class}">WINDOW PRESSURE (24H): {esc(verdict)} (p99 {p99_display})</span>'
        f' <span class="indicator-note">healthy: &le; {threshold:.0f}% &bull; p99 over {known_rows:,} row(s) with a known context_window &bull; '
        f'window unknown: {unknown_rows:,} rows (excluded, not counted as 0%) of {rows_in_window:,} total in the last {WINDOW_PRESSURE_HOURS}h.</span></div>'
    )


def build_two_tier_context_html(agent_context: dict[str, Any] | None) -> str:
    """Issue #227: render the Two-Tier Agent Context Model."""
    if not agent_context:
        return """
        <section class="panel context-panel">
          <div class="panel-header">
            <h2>Agent Context &amp; Working Memory</h2>
            <span class="context-badge badge-unavailable">context unavailable</span>
          </div>
          <p class="unavailable-note">No active system prompt or prompt recording found in runtime state.</p>
        </section>
        """

    sys_prompt = agent_context.get("system_prompt") or {}
    prompt_text = agent_context.get("prompt_text")
    task_text = agent_context.get("task_text")
    skills = agent_context.get("tier2_skills") or []
    skills_status = agent_context.get("tier2_skills_status", "present" if "tier2_skills" in agent_context else "missing")
    lessons = agent_context.get("tier2_lessons") or {}
    memory = agent_context.get("tier2_memory") or {}
    skill_reads = agent_context.get("skill_reads")
    skill_evals = agent_context.get("skill_evals") or []
    executor_llm_stats = agent_context.get("executor_llm_stats")
    compaction = agent_context.get("compaction")

    chars = sys_prompt.get("chars")
    cap = sys_prompt.get("cap")
    overflow = sys_prompt.get("overflow", False)
    over_by = sys_prompt.get("over_by", 0)
    sections = sys_prompt.get("sections")
    missing_files = sys_prompt.get("missing") or []
    truncated_files = sys_prompt.get("truncated") or []
    is_post_migration_row = isinstance(sections, dict) and any(
        name in sections for name in ("soul", "user", "operating", "agents", "runtime")
    )
    nonzero_section_names = [
        name for name in (sections or {}) if (sections.get(name) or 0) > 0
    ] if sections else []
    separator_count = max(0, len(nonzero_section_names) - 1)
    separator_total_chars = separator_count * SEPARATOR_LEN
    dropped = sys_prompt.get("dropped") or []
    rung = sys_prompt.get("rung")
    cid = sys_prompt.get("cycle_id", "")
    ts_str = str(sys_prompt.get("ts") or "")

    # ozand/eeebot#1755 (4a/4b): truncation/drop streak + window pressure.
    # Both are genuinely new indicators (see the issue) -- rendered as their
    # own alert-style rows next to the existing rung/dropped alerts, using
    # the same badge vocabulary as the rest of this panel.
    truncation_streak = agent_context.get("truncation_streak") or {"status": "no_data", "total_rows": 0, "entries": []}
    truncation_streak_html = _render_truncation_streak_html(truncation_streak)
    window_pressure = agent_context.get("window_pressure") or {"status": "no_data", "rows_in_window": 0, "known_rows": 0, "unknown_rows": 0, "p99_pct": None, "threshold_pct": WINDOW_PRESSURE_THRESHOLD_PCT}
    window_pressure_html = _render_window_pressure_html(window_pressure)
    prompt_fit = agent_context.get("prompt_fit") or {}
    prompt_fit_status = prompt_fit.get("source_status", "unavailable")
    prompt_fit_reader_status = prompt_fit.get("reader_status", "unavailable")
    prompt_fit_latest = prompt_fit.get("latest") if isinstance(prompt_fit.get("latest"), dict) else None
    prompt_fit_window_kind = prompt_fit.get("window_kind", "unavailable")
    prompt_fit_window_days = prompt_fit.get("window_days")
    prompt_fit_window_rows = prompt_fit.get("window_rows")
    prompt_fit_rows_considered = prompt_fit.get("rows_considered")
    prompt_fit_rows_with_drops = prompt_fit.get("rows_with_drops")
    prompt_fit_rows_with_trims = prompt_fit.get("rows_with_trims")
    prompt_fit_covered_from = prompt_fit.get("prompt_covered_from")
    prompt_fit_covered_to = prompt_fit.get("prompt_covered_to")
    prompt_fit_latest_ts = prompt_fit_latest.get("ts") if prompt_fit_latest else None

    def fit_metric(metric_name: str, key: str) -> str:
        metric = prompt_fit_latest.get(metric_name) if prompt_fit_latest else None
        if not isinstance(metric, dict):
            return "unavailable"
        status = metric.get("status")
        if status == "empty":
            return "0" if key != "sections" else "none"
        if status != "measured":
            return str(status or "unavailable")
        if key == "sections":
            sections = metric.get("sections")
            return "; ".join(str(section) for section in sections) if sections else "none"
        value = metric.get(key)
        return str(value) if value is not None else "unavailable"

    def fit_window_text() -> str:
        if not isinstance(prompt_fit_rows_considered, int) or not isinstance(prompt_fit_window_rows, int):
            return "window unavailable"
        period = (
            f"{prompt_fit_covered_from} → {prompt_fit_covered_to}"
            if prompt_fit_covered_from and prompt_fit_covered_to
            else "timestamps unavailable"
        )
        return f"{prompt_fit_rows_considered}/{prompt_fit_window_rows} rows · {prompt_fit_window_kind} · {prompt_fit_window_days}d · {period}"

    def fit_status_text() -> str:
        if prompt_fit_status == "valid" and prompt_fit_reader_status == "complete":
            return "published"
        return f"{prompt_fit_status} (reader: {prompt_fit_reader_status})"

    ts_display = ts_str.replace("T", " ").replace("Z", " MSK") if ts_str else "active cycle"

    parsed_prompt = parse_prompt_sections(prompt_text, sections)
    raw_sections_text = {
        name: details["text"]
        for name, details in parsed_prompt["sections"].items()
        if details.get("text") is not None
    }
    outside_cap = parsed_prompt["outside_cap"]
    actual_system_chars = len(prompt_text) if prompt_text is not None else None

    if chars is None and sections:
        # For overflow rows, chars key is absent; total is cap + over_by or sum of non-empty sections + separators
        if overflow and cap is not None and over_by is not None:
            chars = cap + over_by
        else:
            chars = sum(sections.values()) + separator_total_chars

    total_chars = chars or (len(prompt_text) if prompt_text else 0)
    total_tokens = estimate_tokens(total_chars)

    if overflow and cap is not None:
        ov_amount = over_by if over_by else (total_chars - cap)
        headroom_badge = f'<span class="context-badge badge-danger context-badge-overflow">OVERFLOW (+{ov_amount:,} chars over cap)</span>'
        headroom_text = f'<span class="stat-warn">-{ov_amount:,} chars (OVERFLOW)</span>'
        bar_pct = min(100, int((total_chars / cap) * 100)) if cap > 0 else None
        bar_color = "var(--color-danger, #f85149)"
    elif cap is not None and cap > 0 and total_chars:
        spare = cap - total_chars
        pct = (total_chars / cap) * 100
        headroom_badge = f'<span class="context-badge badge-success context-badge-safe">WITHIN BUDGET (+{spare:,} chars spare)</span>'
        headroom_text = f'<span class="stat-good">+{spare:,} chars ({spare/cap*100:.1f}%)</span>'
        bar_pct = min(100, int(pct))
        bar_color = "var(--color-warning, #d29922)" if pct > 85 else "var(--color-success, #2ea043)"
    else:
        headroom_badge = '<span class="context-badge badge-secondary">CAPACITY UNKNOWN</span>'
        headroom_text = '<span>n/a</span>'
        bar_pct = None
        bar_color = "var(--color-accent, #58a6ff)"

    rung_html = ""
    if rung and rung != "full":
        rung_html = f'<div class="context-dropped-alert">&#9888; <strong>Prompt fit degradation:</strong> {esc(rung)}</div>'

    dropped_html = ""
    if dropped:
        pills = []
        for d in dropped:
            d_name = d.get("name", "section") if isinstance(d, dict) else str(d)
            d_sz = f'{d.get("chars", 0):,}c' if isinstance(d, dict) else "dropped"
            pills.append(f'<span class="dropped-pill"><s>{esc(d_name)}</s> ({d_sz})</span>')
        dropped_html = f'<div class="context-dropped-alert">&#9888; <strong>Trimming applied:</strong> {" ".join(pills)}</div>'

    skills_kb = sum(s.get("size_bytes", 0) for s in skills) / 1024
    lessons_st = lessons.get("index_status", "unknown")
    lessons_corpus_status = lessons.get("corpus_status", "present" if "corpus_count" in lessons else "missing")
    lessons_cnt = lessons.get("corpus_count", 0)
    lessons_kb = lessons.get("total_size_bytes", 0) / 1024
    mem_st = memory.get("index_status", "unknown")
    mem_corpus_status = memory.get("corpus_status", "present" if "total_files" in memory else "missing")
    mem_cnt = memory.get("total_files", 0)
    mem_kb = memory.get("total_size_bytes", 0) / 1024
    t2_kb = skills_kb + lessons_kb + mem_kb

    def corpus_count(status: str, count: int) -> str:
        return str(count) if status == "present" else status

    corpus_counts = (
        (skills_status, len(skills)),
        (lessons_corpus_status, lessons_cnt),
        (mem_corpus_status, mem_cnt),
    )
    t2_files = (
        str(sum(count for status, count in corpus_counts))
        if all(status == "present" for status, _ in corpus_counts)
        else "unavailable"
    )
    cat_sz = sections.get("skills_catalogue", 0) if sections else len(raw_sections_text.get("skills_catalogue", ""))
    mem_sz = sections.get("memory", 0) if sections else len(raw_sections_text.get("memory", ""))

    out = []
    out.append('<section class="panel context-panel">')
    out.append('  <div class="panel-header">')
    out.append('    <div>')
    out.append('      <h2>Agent Context &amp; Working Memory</h2>')
    out.append('      <p class="panel-subtitle">Two-tier architecture: assembled messages inside the model active context window vs. reachable resources on disk. Source in UTC, displayed in MSK.</p>')
    out.append('    </div>')
    out.append('    <div class="context-header-badges">')
    out.append(f'      {headroom_badge}')
    if rung:
        rung_badge_class = "badge-success" if rung == "full" else "badge-danger"
        out.append(f'      <span class="context-badge {rung_badge_class}">Prompt fit: {esc(rung)}</span>')
    out.append(f'      <span class="cycle-pill">{esc(cid or "latest")}</span>')
    out.append('    </div>')
    out.append('  </div>')

    out.append('  <div class="context-kpis">')
    out.append('    <div class="context-kpi-card">')
    out.append('      <span class="kpi-label">Capped Prompt Load</span>')
    out.append(f'      <span class="kpi-value">{total_chars:,} <span class="kpi-unit">capped chars</span></span>')
    out.append(f'      <span class="kpi-sub">~{total_tokens:,} est. tokens under builder cap</span>')
    actual_system_display = f'<strong>Actual System Message:</strong> {actual_system_chars:,} chars received by model' if actual_system_chars is not None else '<strong>Actual System Message:</strong> unavailable'
    out.append(f'      <span class="kpi-sub">{actual_system_display}</span>')
    out.append('    </div>')
    out.append('    <div class="context-kpi-card">')
    out.append('      <span class="kpi-label">Context Budget Cap</span>')
    cap_display = f'{cap:,} <span class="kpi-unit">chars</span>' if cap is not None else 'unavailable'
    cap_tokens_display = f'~{estimate_tokens(cap):,} est. tokens limit' if cap is not None else 'recorded cap unavailable'
    out.append(f'      <span class="kpi-value">{cap_display}</span>')
    out.append(f'      <span class="kpi-sub">{cap_tokens_display}</span>')
    out.append('    </div>')
    out.append('    <div class="context-kpi-card">')
    out.append('      <span class="kpi-label">Remaining Headroom</span>')
    out.append(f'      <span class="kpi-value">{headroom_text}</span>')
    out.append('      <span class="kpi-sub">margin before overflow trim</span>')
    out.append('    </div>')
    out.append('    <div class="context-kpi-card">')
    out.append('      <span class="kpi-label">Tier 2 Reachable Knowledge</span>')
    out.append(f'      <span class="kpi-value">~{t2_kb:.1f} <span class="kpi-unit">KB</span></span>')
    out.append(f'      <span class="kpi-sub">{t2_files} files reachable via read_file</span>')
    out.append('    </div>')
    out.append('  </div>')

    out.append('  <div class="context-subject-group context-overview-group">')
    out.append('    <h3 class="context-subject-heading">Prompt Budget &amp; Fit</h3>')
    out.append('  <div class="context-meter-box">')
    meter_value = f'<strong>{bar_pct}%</strong> ({total_chars:,} / {cap:,} chars)' if bar_pct is not None and cap is not None else '<strong>unavailable</strong> (recorded cap unavailable)'
    out.append(f'    <div class="meter-labels"><span>Prompt Budget Utilization: {meter_value}</span><span>{ts_display}</span></div>')
    out.append('    <div class="context-progress-bar">')
    meter_fill = f'<div class="context-progress-fill" style="width:{bar_pct}%;background:{bar_color};"></div>' if bar_pct is not None else '<div class="context-progress-fill meter-unavailable" style="width:0%;"></div>'
    out.append(f'      {meter_fill}')
    out.append('    </div>')
    out.append('  </div>')

    if rung_html:
        out.append(f'  {rung_html}')
    if dropped_html:
        out.append(f'  {dropped_html}')

    out.append(f'  {truncation_streak_html}')
    out.append(f'  {window_pressure_html}')

    out.append('  <div class="prompt-fit-events" id="prompt-fit-events">')
    out.append('    <h3>Prompt Fit Event Telemetry</h3>')
    out.append(f'    <p class="section-sub">Published runtime summary · latest prompt row: {esc(prompt_fit_latest_ts or ts_str or "unavailable")} · source: {esc(fit_status_text())}</p>')
    out.append('    <div class="prompt-fit-event-grid">')
    prompt_fit_rung = prompt_fit_latest.get("rung") if prompt_fit_latest else None
    out.append(f'      <div class="prompt-fit-event-item"><strong>Rung</strong><span>{esc(prompt_fit_rung or rung or "unavailable")}</span></div>')
    out.append(f'      <div class="prompt-fit-event-item"><strong>Dropped</strong><span>{esc(fit_metric("dropped", "count"))} sections / {esc(fit_metric("dropped", "chars"))} chars</span></div>')
    out.append(f'      <div class="prompt-fit-event-item"><strong>Trimmed</strong><span>{esc(fit_metric("trimmed", "count"))} sections / {esc(fit_metric("trimmed", "chars"))} chars</span></div>')
    out.append(f'      <div class="prompt-fit-event-item"><strong>Recent rows with drops</strong><span>{esc(str(prompt_fit_rows_with_drops) if prompt_fit_rows_with_drops is not None else "unavailable")} / {esc(str(prompt_fit_rows_considered) if prompt_fit_rows_considered is not None else "unavailable")}</span></div>')
    out.append(f'      <div class="prompt-fit-event-item"><strong>Recent rows with trims</strong><span>{esc(str(prompt_fit_rows_with_trims) if prompt_fit_rows_with_trims is not None else "unavailable")} / {esc(str(prompt_fit_rows_considered) if prompt_fit_rows_considered is not None else "unavailable")}</span></div>')
    out.append(f'      <div class="prompt-fit-event-item prompt-fit-event-wide"><strong>Event window</strong><span>{esc(fit_window_text())}</span></div>')
    out.append(f'      <div class="prompt-fit-event-item prompt-fit-event-wide"><strong>Dropped section names</strong><span>{esc(fit_metric("dropped", "sections"))}</span></div>')
    out.append(f'      <div class="prompt-fit-event-item prompt-fit-event-wide"><strong>Trimmed section names</strong><span>{esc(fit_metric("trimmed", "sections"))}</span></div>')
    history_prompt_tokens = executor_llm_stats.get("prompt_tokens") if isinstance(executor_llm_stats, dict) else None
    history_text = f"{history_prompt_tokens:,} prompt tokens (executor)" if isinstance(history_prompt_tokens, int) else "unavailable"
    out.append('    </div>')
    window_tokens = 98000
    reserve_tokens = 8000
    window_budget = window_tokens - reserve_tokens
    compaction_status = "compaction did not fire" if isinstance(compaction, dict) and compaction.get("status") in {"missing", "empty"} else esc(compaction.get("status", "unavailable") if isinstance(compaction, dict) else "unavailable")
    out.append(f'<div class="context-telemetry-box dialogue-window-summary"><strong>Dialogue Window:</strong> {window_budget:,} tokens ({window_tokens:,} − {reserve_tokens:,}) · occupancy: {history_text} · {compaction_status}</div>')
    out.append('  </div>')

    out.append('  <div class="context-subject-group context-assembly-group">')
    out.append('    <h3 class="context-subject-heading">Prompt Assembly &amp; Context Architecture</h3>')
    out.append('  <div class="two-tier-canvas">')
    out.append('    <div class="tier-col tier1-col">')
    out.append('      <div class="tier-col-header"><span class="tier-tag tag-t1">TIER 1</span><div><h3>In Active Context (Attention Window)</h3><p>Assembled into system and user messages.</p></div></div>')
    out.append('      <div class="tier1-blocks-list">')
    # #301: no hard-coded section names here -- one row per entry in the
    # ledger row's `sections`, in the RECORDED order (whatever the harness
    # emitted that cycle). Owner/file/cap come from the static ADR-022 map;
    # unknown names fall through to "unmapped" and still render their chars.
    for t1_seq, sec_name in enumerate(sections or {}, start=1):
        sec_sz = sections.get(sec_name) or 0
        meta = section_owner_meta(sec_name)
        is_empty = sec_sz == 0
        empty_class = "t1-empty" if is_empty else ""
        sz_text = f"{sec_sz:,}c (empty under loop profile)" if is_empty else f"{sec_sz:,}c"
        owner_tag = f'<span class="t1-owner t1-owner-{esc(meta["owner"])}">{esc(meta["owner"])}</span>'
        if sec_name == "skills_catalogue":
            out.append(f'        <div class="t1-block-item t1-linked"><div class="t1-row"><span class="t1-seq">{t1_seq}</span><span class="t1-name">{esc(sec_name)}</span>{owner_tag}<span class="t1-sz">{sz_text}</span></div><a href="#tier2-skills-section" class="tier-link-badge tier-link-origin">&#10140; Indexes {corpus_count(skills_status, len(skills))} Skills in Tier 2 ({skills_kb:.1f} KB)</a></div>')
        elif sec_name == "memory":
            out.append(f'        <div class="t1-block-item t1-linked"><div class="t1-row"><span class="t1-seq">{t1_seq}</span><span class="t1-name">{esc(sec_name)}</span>{owner_tag}<span class="t1-sz">{sz_text}</span></div><a href="#tier2-memory-section" class="tier-link-badge">&#10140; Indexes {corpus_count(mem_corpus_status, mem_cnt)} files in Tier 2</a></div>')
        else:
            out.append(f'        <div class="t1-block-item {empty_class}"><span class="t1-seq">{t1_seq}</span><span class="t1-name">{esc(sec_name)}</span>{owner_tag}<span class="t1-sz">{sz_text}</span></div>')
    out.append(f'        <div class="t1-block-item t1-sep-row"><span class="t1-name">&#8230; {separator_count} &times; "\n\n---\n\n" Separators</span><span class="t1-sz">{separator_total_chars}c</span></div>')
    window_budget = 98000 - 8000
    window_text = f"{window_budget:,} tokens available (98,000 − 8,000) · occupancy {history_text}"
    out.append(f'        <div class="t1-block-item t1-msg"><span class="t1-seq">msg</span><span class="t1-name">history + tool results</span><span class="t1-desc">{window_text}</span></div>')
    out.append(f'        <div class="t1-block-item t1-msg"><span class="t1-seq">user</span><span class="t1-name">runtime_context + task</span><span class="t1-sz">{len(task_text) if task_text else 0:,}c</span></div>')
    out.append('      </div>')
    out.append('    </div>')

    out.append('    <div class="tier-bridge">')
    out.append('      <div class="bridge-card">')
    out.append('        <span class="bridge-arrow">&#10132;</span>')
    out.append('        <strong>On-Demand Access</strong>')
    out.append('        <p>read_file · search_memory · exec</p>')
    out.append(f'        <span class="bridge-cost">Tier 1 index costs: catalogue {cat_sz:,}c · memory {mem_sz:,}c · exec path unavailable</span>')
    out.append('      </div>')
    out.append('    </div>')

    out.append('    <div class="tier-col tier2-col">')
    out.append('      <div class="tier-col-header"><span class="tier-tag tag-t2">TIER 2</span><div><h3>Reachable on Disk (Zero Base Context)</h3><p>Full instructions, lessons, and memory records.</p></div></div>')
    out.append('      <div class="tier2-targets-list">')
    out.append(f'        <div class="t2-target-card"><div class="t2-card-top"><strong>Skills Store: {corpus_count(skills_status, len(skills))} skills</strong><span class="t2-size">{skills_kb:.1f} KB</span></div><p>Corpus status: <span class="status-badge status-{skills_status}">{skills_status}</span></p><a href="#tier2-skills-section" class="t2-explore-btn">Inspect Skills &darr;</a></div>')
    out.append(f'        <div class="t2-target-card"><div class="t2-card-top"><strong>Lessons Corpus: {corpus_count(lessons_corpus_status, lessons_cnt)} lessons</strong><span class="t2-size">{lessons_kb:.1f} KB</span></div><p>Corpus status: <span class="status-badge status-{lessons_corpus_status}">{lessons_corpus_status}</span>; index: <span class="status-badge status-{lessons_st}">{lessons_st}</span></p><a href="#tier2-lessons-section" class="t2-explore-btn">Inspect Lessons &darr;</a></div>')
    out.append(f'        <div class="t2-target-card"><div class="t2-card-top"><strong>Memory Store: {corpus_count(mem_corpus_status, mem_cnt)} files</strong><span class="t2-size">{mem_kb:.1f} KB</span></div><p>Corpus status: <span class="status-badge status-{mem_corpus_status}">{mem_corpus_status}</span>; index: <span class="status-badge status-{mem_st}">{mem_st}</span></p><a href="#tier2-memory-section" class="t2-explore-btn">Inspect Memory &darr;</a></div>')
    out.append('      </div>')
    out.append('    </div>')
    out.append('  </div>')

    out.append('  <div class="context-detail-section">')
    format_badge = (
        '<span class="status-badge status-present">format: post-ADR-022 ontology</span>' if is_post_migration_row
        else '<span class="status-badge status-missing">format: pre-ADR-022 legacy</span>' if sections
        else '<span class="status-badge status-missing">format: unavailable</span>'
    )
    out.append(f'    <h3>Tier 1: Assembled Context Blocks (Recorded Order) {format_badge}</h3>')
    out.append('    <p class="section-sub">Blocks follow the ledger row\'s own recorded `sections` order for this cycle -- never a hard-coded assembly order. Click to view exact text.</p>')

    block_seq = 1
    reconciliation_rows = []
    total_sections_chars = 0

    if sections:
        for sec_name in sections:
            sec_sz = sections.get(sec_name) or 0
            meta = section_owner_meta(sec_name)
            owner = meta["owner"]
            file_name = meta["file"]
            sec_cap = meta.get("cap")
            cap_text = f"{sec_cap:,}c" if isinstance(sec_cap, int) else "dynamic"
            is_missing = file_name in missing_files
            is_truncated = file_name in truncated_files
            flags = []
            if is_missing:
                flags.append('<span class="badge-flag badge-flag-missing">MISSING</span>')
            if is_truncated:
                flags.append('<span class="badge-flag badge-flag-truncated">TRUNCATED</span>')
            flags_html = " ".join(flags) if flags else "&mdash;"
            sec_tokens = estimate_tokens(sec_sz)
            total_sections_chars += sec_sz
            sec_text = raw_sections_text.get(sec_name, "")
            owner_cell = f'<span class="t1-owner t1-owner-{esc(owner)}">{esc(owner)}</span> <code>{esc(file_name)}</code>'
            reconciliation_rows.append(
                f'<tr class="{"muted-row" if sec_sz == 0 else ""}"><td><code>{esc(sec_name)}</code></td>'
                f'<td>{owner_cell}</td><td class="num">{sec_sz:,}</td><td class="num">{cap_text}</td>'
                f'<td>{flags_html}</td><td>~{sec_tokens:,} tokens</td></tr>'
            )
            preview = ""
            if sec_text:
                heading_match = re.search(r"^#{1,3}\s+(.+)$", sec_text, re.MULTILINE)
                if heading_match:
                    preview = f'<span class="block-preview">{esc(heading_match.group(1).strip())}</span>'
            if sec_sz == 0:
                out.append(f'<div class="context-block-empty"><span class="block-seq">#{block_seq}</span><strong>{esc(sec_name)}</strong> {owner_cell} &mdash; 0 chars (empty under loop profile) {flags_html}</div>')
            else:
                out.append(
                    f'<details class="context-block-details"><summary class="block-summary"><span class="block-seq">#{block_seq}</span>'
                    f'<strong class="block-title">{esc(sec_name)}</strong><span class="block-label">{owner_cell} &bull; cap {cap_text}</span>'
                    f'{preview}<span class="block-meta">{sec_sz:,} chars &bull; ~{sec_tokens:,} tokens {flags_html}</span></summary>'
                    f'<div class="block-body"><pre><code>{esc(sec_text if sec_text else "(section text not captured in prompt file)")}</code></pre></div></details>'
                )
            block_seq += 1

        sep_count = separator_count
        sep_total_chars = separator_total_chars
        reconciled_total = total_sections_chars + sep_total_chars

        # #301: compare the reconciled sum against the REAL message length --
        # the actual system message the model received (`prompt_text`) when
        # we have it, never the recorded `chars` field alone, which on
        # pre-migration rows deliberately excludes a tail the bridge appends
        # after the prompt fit. Falsely calling that an exact match hid the
        # tail's existence. Only fall back to the recorded/derived total
        # when the actual message text was not captured.
        if actual_system_chars is not None:
            message_length = actual_system_chars
            message_length_label = "actual system message received by model"
        else:
            message_length = total_chars
            message_length_label = "recorded chars field (actual message text unavailable)"

        reconciliation_rows.append(f'<tr class="subtotal-row"><td colspan="2"><strong>Sum of Sections</strong></td><td class="num"><strong>{total_sections_chars:,}</strong></td><td colspan="2"></td><td>&sum; section chars</td></tr>')
        reconciliation_rows.append(f'<tr class="sep-row"><td colspan="2"><strong>Separators (\\n\\n---\\n\\n)</strong></td><td class="num"><strong>{sep_total_chars:,}</strong></td><td colspan="2"></td><td>{sep_count} &times; {SEPARATOR_LEN} chars</td></tr>')
        diff = reconciled_total - message_length
        is_match = diff == 0
        match_badge = (
            '<span class="status-badge status-present">&#10003; Exact Match &bull; Reconciliation verified</span>' if is_match
            else f'<span class="status-badge status-missing">Diff: {diff:+,}c vs {esc(message_length_label)}</span>'
        )

        out.append('    <div class="reconciliation-box">')
        out.append(f'      <div class="rec-header"><h4>Arithmetic Character Reconciliation</h4>{match_badge}</div>')
        out.append('      <table class="reconciliation-table">')
        out.append('        <thead><tr><th>Key</th><th>Owner / Source</th><th class="num">Chars</th><th class="num">Cap</th><th>Flags</th><th>Notes</th></tr></thead>')
        out.append(f'        <tbody>{"".join(reconciliation_rows)}<tr class="total-row"><td colspan="2"><strong>Total (sections + separators)</strong></td><td class="num"><strong>{reconciled_total:,}</strong></td><td colspan="2"></td><td><strong>Recorded chars: {message_length:,}</strong> ({esc(message_length_label)})</td></tr></tbody>')
        out.append('      </table>')
        out.append(f'      <p class="rec-note">Formula: &sum;(recorded sections: {total_sections_chars:,}c) + {sep_count} separators &times; {SEPARATOR_LEN}c ({sep_total_chars:,}c) = {reconciled_total:,} chars, compared against {esc(message_length_label)} ({message_length:,}c).</p>')
        if outside_cap:
            tail_total = sum(item["actual_chars"] for item in outside_cap)
            out.append(f'      <p class="rec-note"><strong>Legacy tail beyond recorded sections:</strong> {tail_total:,}c across {len(outside_cap)} block(s) -- see below. Not part of the ledger `sections` map.</p>')
        if parsed_prompt["status"] == "exact":
            evidence = " ".join(
                f'<code>{esc(name)}</code>: recorded {details["recorded_chars"]:,} / parsed {details["actual_chars"]:,} chars (exact)'
                for name, details in parsed_prompt["sections"].items()
            )
            out.append(f'      <p class="rec-note">Structural prompt parse: {evidence}</p>')
        elif parsed_prompt["status"] == "mismatch":
            evidence = " ".join(
                f'<code>{esc(item["name"])}</code>: recorded {item["recorded_chars"]} / parsed {item["actual_chars"] if item["actual_chars"] is not None else "unavailable"}'
                for item in parsed_prompt["mismatches"]
            ) or "recorded section boundaries unavailable"
            out.append(f'      <p class="rec-note"><strong>Structural prompt parse mismatch:</strong> {evidence}</p>')
        out.append('    </div>')
    else:
        out.append('    <div class="reconciliation-box rec-unavailable">')
        out.append('      <div class="rec-header"><h4>Arithmetic Character Reconciliation</h4><span class="status-badge status-missing">sections: unavailable</span></div>')
        out.append(f'      <p class="unavailable-note"><strong>sections breakdown: unavailable</strong> &mdash; This cycle row was recorded prior to structured section logging (#1379). Recorded total chars: <strong>{total_chars:,}</strong>. Per honesty rules, section sizes are not reconstructed.</p>')
        out.append('    </div>')
        if prompt_text:
            out.append(f'<details class="context-block-details"><summary class="block-summary"><span class="block-seq">#1</span><strong class="block-title">system_prompt (full text)</strong><span class="block-meta">{total_chars:,} chars &bull; ~{total_tokens:,} tokens</span></summary><div class="block-body"><pre><code>{esc(prompt_text)}</code></pre></div></details>')

    # #301: legacy tail (pre-ADR-022 rows only) -- rendered as its own
    # labelled block(s), never folded silently into "outside capped prompt";
    # for post-migration rows `outside_cap` is empty because the harness no
    # longer appends anything after the prompt fit.
    for outside in outside_cap:
        out.append(
            f'<details class="context-block-details outside-cap-block"><summary class="block-summary">'
            f'<strong class="block-title">{esc(outside["name"])}</strong>'
            f'<span class="block-label">unmapped &bull; legacy tail, beyond recorded sections</span>'
            f'<span class="block-meta">{outside["actual_chars"]:,} chars</span></summary>'
            f'<div class="block-body"><pre><code>{esc(outside["text"])}</code></pre></div></details>'
        )

    if task_text:
        t_sz = len(task_text)
        out.append(f'<details class="context-block-details user-block-details"><summary class="block-summary"><span class="block-seq">#{block_seq}</span><strong class="block-title">user (runtime_context + task)</strong><span class="block-meta">{t_sz:,} chars &bull; ~{estimate_tokens(t_sz):,} tokens</span></summary><div class="block-body"><pre><code>{esc(task_text)}</code></pre></div></details>')

    # #301: the user message's own section list (`build_task` `## ` headings)
    # for the latest recorded prompt -- makes duplicate-section defects
    # visible on the page instead of only in a ledger dump.
    task_sections = build_task_sections(task_text)
    out.append('    <div class="user-message-sections">')
    out.append('      <h4>User Message Sections (latest recorded task text)</h4>')
    if not task_text:
        out.append('      <p class="unavailable-note">task text unavailable for this cycle.</p>')
    elif not task_sections:
        out.append('      <p class="unavailable-note">no `## ` headings found in the recorded task text.</p>')
    else:
        rows = []
        for section in task_sections:
            dup_badge = ' <span class="badge-flag badge-flag-truncated">DUPLICATE</span>' if section["duplicate"] else ""
            rows.append(f'<tr><td>{esc(section["heading"])}{dup_badge}</td><td class="num">{section["chars"]:,}</td></tr>')
        out.append('      <table class="reconciliation-table">')
        out.append('        <thead><tr><th>## Heading</th><th class="num">Chars</th></tr></thead>')
        out.append(f'        <tbody>{"".join(rows)}</tbody>')
        out.append('      </table>')
    out.append('    </div>')

    # #301: rule-owners panel -- which prompt block owns each rule the
    # harness's fingerprint test tracks. Prefers a `rule_owners` telemetry
    # field once the harness publishes one (ozand/eeebot#1725); until then
    # this is the static ADR-022 map, clearly labelled as such.
    rule_owners = sys_prompt.get("rule_owners")
    rule_owners_is_static = not isinstance(rule_owners, dict) or not rule_owners
    rule_owners_source = rule_owners if isinstance(rule_owners, dict) and rule_owners else RULE_OWNERS_STATIC
    out.append('    <div class="rule-owners-panel">')
    static_note = ' <span class="status-badge status-missing">static (until harness publishes rule_owners)</span>' if rule_owners_is_static else ' <span class="status-badge status-present">published by harness</span>'
    out.append(f'      <h4>Rule Owners{static_note}</h4>')
    rule_rows = []
    for rule_name, owner_section in rule_owners_source.items():
        owner_meta = section_owner_meta(owner_section)
        rule_rows.append(
            f'<tr><td><code>{esc(rule_name)}</code></td><td><code>{esc(owner_section)}</code></td>'
            f'<td><span class="t1-owner t1-owner-{esc(owner_meta["owner"])}">{esc(owner_meta["owner"])}</span></td></tr>'
        )
    out.append('      <table class="reconciliation-table">')
    out.append('        <thead><tr><th>Rule (fingerprint test)</th><th>Owning block</th><th>Owner class</th></tr></thead>')
    out.append(f'        <tbody>{"".join(rule_rows)}</tbody>')
    out.append('      </table>')
    out.append('    </div>')

    out.append('  </div>')
    out.append('  </div>')

    out.append('  <div class="context-subject-group context-knowledge-group">')
    out.append('    <h3 class="context-subject-heading">Reachable Knowledge &amp; Access Paths</h3>')
    out.append('  <div class="tier2-deep-section">')
    out.append('    <h3>Tier 2: Reachable On-Demand Knowledge Base</h3>')
    out.append(f'    <p class="section-sub">Assets residing on disk, accessible by tool calls during cycle loop. Total: <strong>~{t2_kb:.1f} KB</strong> across <strong>{t2_files}</strong> files.</p>')

    catalogue = sys_prompt.get("skills_catalogue")
    omitted_names = [str(name) for name in catalogue.get("omitted_names", [])] if isinstance(catalogue, dict) else []
    catalogue_state = "truncated" if isinstance(catalogue, dict) and catalogue.get("truncated") else "complete" if isinstance(catalogue, dict) else "unavailable"
    out.append(f'<div class="context-telemetry-box"><strong>Skills catalogue:</strong> budget {catalogue.get("budget", "unavailable") if isinstance(catalogue, dict) else "unavailable"}c · retained {catalogue.get("retained_count", "unavailable") if isinstance(catalogue, dict) else "unavailable"}/{catalogue.get("total_count", "unavailable") if isinstance(catalogue, dict) else "unavailable"} · {catalogue_state} · omitted: {esc(", ".join(omitted_names) if omitted_names else "none")}</div>')
    memory_index = sys_prompt.get("memory_index")
    missing = [str(name) for name in memory_index.get("resident_missing", [])] if isinstance(memory_index, dict) and isinstance(memory_index.get("resident_missing"), list) else None
    missing_text = ", ".join(missing) if missing else "none" if isinstance(memory_index, dict) else "unavailable"
    out.append(f'<div class="context-telemetry-box"><strong>Memory index:</strong> status {esc(memory_index.get("status", "unavailable") if isinstance(memory_index, dict) else "unavailable")} · resident matched {memory_index.get("resident_matched", "unavailable") if isinstance(memory_index, dict) else "unavailable"} · resident missing: {esc(missing_text)}</div>')
    catalogue_text = raw_sections_text.get("skills_catalogue", "")
    catalogue_names = {re.sub(r"<[^>]+>", "", name).strip() for name in re.findall(r"<name>(.*?)</name>", catalogue_text, re.DOTALL)}
    catalogue_names.discard("")
    skill_rows = {}
    if isinstance(skill_reads, dict) and isinstance(skill_reads.get("reads"), list):
        for row in skill_reads["reads"]:
            if isinstance(row, dict) and row.get("skill"):
                name = str(row["skill"])
                record = skill_rows.setdefault(name, {"reads": 0, "confirmed": 0})
                record["reads"] += 1
                record["confirmed"] += int(row.get("confirmed") is True)
    for name in catalogue_names:
        skill_rows.setdefault(name, {})["catalogue"] = True
    for skill in skills:
        skill_rows.setdefault(str(skill.get("name", "")), {})["on_disk"] = True
    for name in omitted_names:
        skill_rows.setdefault(name, {})["omitted"] = True
    skill_table_rows = []
    for name in sorted(name for name in skill_rows if name):
        record = skill_rows[name]
        state = "unreachable" if record.get("omitted") else "working" if record.get("on_disk") else "not on disk"
        catalogue_value = "yes" if record.get("catalogue") else "no" if record.get("omitted") else "unavailable"
        skill_table_rows.append(f'<tr><td>{esc(name)}</td><td>{catalogue_value}</td><td>{"yes" if record.get("on_disk") else "no"}</td><td>{record.get("reads", 0)}</td><td>{record.get("confirmed", 0)}</td><td>{state}</td></tr>')
    out.append(f'<div class="context-telemetry-box"><strong>Skill source join:</strong> catalogue · Tier 2 disk · skill fitness reads</div><table class="skills-table"><thead><tr><th>Skill</th><th>In catalogue</th><th>On disk</th><th>Reads</th><th>Confirmed</th><th>State</th></tr></thead><tbody>{"".join(skill_table_rows)}</tbody></table>')
    out.append('    <div class="t2-group" id="tier2-skills-section">')
    out.append(f'      <div class="t2-group-header"><h4>Skills Store ({corpus_count(skills_status, len(skills))} skills &bull; ~{skills_kb:.1f} KB)</h4><span class="t2-group-note">Corpus: {skills_status}; indexed in Tier 1 via <code>skills_catalogue</code> ({cat_sz:,} chars)</span></div>')
    out.append('      <div class="skills-card-grid">')
    for s in skills:
        s_name = s.get("name", "")
        s_bytes = s.get("size_bytes", 0)
        s_desc = s.get("desc", "")
        s_content = s.get("content", "")
        out.append(f'        <div class="skill-asset-card"><div class="skill-card-head"><span class="skill-card-name">{esc(s_name)}</span><span class="skill-card-size">{s_bytes:,} B</span></div><p class="skill-card-desc">{esc(s_desc if s_desc else "No description line found.")}</p><details class="skill-card-details"><summary>View SKILL.md ({s_bytes:,} bytes)</summary><pre><code>{esc(s_content)}</code></pre></details></div>')
    out.append('      </div>')
    out.append('    </div>')

    out.append('    <div class="t2-group" id="tier2-lessons-section">')
    out.append(f'      <div class="t2-group-header"><h4>Lessons Corpus</h4><span class="status-badge status-{lessons_corpus_status}">corpus: {lessons_corpus_status.upper()}</span> <span class="status-badge status-{lessons_st}">lessons/index.md: {lessons_st.upper()}</span></div>')
    if lessons_st == "missing":
        lesson_count_text = corpus_count(lessons_corpus_status, lessons_cnt)
        out.append(f'      <div class="missing-artifact-callout"><span class="callout-icon">&#8505;</span><div><strong>lessons/index.md is MISSING</strong><p>The lessons index is generated once daily and cleared between cycles by <code>git clean -fd</code>. The underlying corpus of <strong>{lesson_count_text} lesson files</strong> (~{lessons_kb:.1f} KB) remains intact on disk.</p></div></div>')
    out.append(f'      <div class="lessons-compact-list"><p><strong>Corpus Files:</strong> {corpus_count(lessons_corpus_status, lessons_cnt)} lesson records on disk (~{lessons_kb:.1f} KB total):</p><div class="lessons-pills">')
    for lf in lessons.get("files", [])[:30]:
        out.append(f'<span class="lesson-pill">{esc(lf.get("name", ""))} ({lf.get("size_bytes", 0):,} B)</span> ')
    if lessons_corpus_status == "present" and lessons_cnt > 30:
        out.append(f'<span class="lesson-pill pill-more">+{lessons_cnt - 30} more lessons...</span>')
    out.append('      </div></div>')
    out.append('    </div>')

    out.append('    <div class="t2-group" id="tier2-memory-section">')
    out.append(f'      <div class="t2-group-header"><h4>Working Memory Store</h4><span class="status-badge status-{mem_corpus_status}">corpus: {mem_corpus_status.upper()}</span> <span class="status-badge status-{mem_st}">memory/index.md: {mem_st.upper()}</span></div>')
    out.append(f'      <div class="memory-compact-list"><p>Indexed via Tier 1 <code>memory</code> block. <strong>{corpus_count(mem_corpus_status, mem_cnt)} files</strong> on disk (~{mem_kb:.1f} KB total):</p><div class="memory-pills">')
    for mf in memory.get("files", [])[:30]:
        out.append(f'<span class="memory-pill">{esc(mf.get("name", ""))} ({mf.get("size_bytes", 0):,} B)</span> ')
    if mem_corpus_status == "present" and mem_cnt > 30:
        out.append(f'<span class="memory-pill pill-more">+{mem_cnt - 30} more files...</span>')
    out.append('      </div></div>')
    out.append('    </div>')

    out.append('  </div>')
    out.append('  </div>')
    out.append('</section>')

    return "\n".join(out)

AGENT_CONTEXT_CSS = """
/* Issue #227: Two-tier agent context visualization styling */
.context-subject-group {
  margin-top: 24px;
  padding-top: 18px;
  border-top: 1px solid #30363d;
}
.context-subject-group:first-of-type { margin-top: 16px; }
.context-subject-heading {
  margin: 0 0 12px;
  font-size: 16px;
  color: #c9d1d9;
  letter-spacing: .01em;
}
.context-knowledge-group .tier2-deep-section,
.context-assembly-group .context-detail-section { margin-top: 0; }
.context-telemetry-box { margin: 10px 0; padding: 9px 12px; border: 1px solid #30363d; border-radius: 6px; background: #0d1117; color: #c9d1d9; font-size: 12px; overflow-wrap: anywhere; }

.context-panel {
  background: var(--color-bg-subtle, #161b22);
  border: 1px solid var(--color-border-default, #30363d);
  border-radius: 6px;
  padding: 20px;
  margin-bottom: 24px;
}
.context-header-badges {
  display: flex;
  gap: 8px;
  align-items: center;
}
.context-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 4px 8px;
  border-radius: 12px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.badge-success { background: rgba(46, 160, 67, 0.2); color: #3fb950; border: 1px solid rgba(46, 160, 67, 0.4); }
.badge-danger { background: rgba(248, 81, 73, 0.2); color: #f85149; border: 1px solid rgba(248, 81, 73, 0.4); }
.badge-secondary { background: #21262d; color: #8b949e; border: 1px solid #30363d; }
.cycle-pill { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; padding: 4px 10px; border-radius: 12px; font-size: 11px; font-family: monospace; }
.context-kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
  margin: 16px 0;
}
.context-kpi-card {
  background: var(--color-canvas-default, #0d1117);
  border: 1px solid var(--color-border-muted, #21262d);
  border-radius: 6px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
}
.kpi-label { font-size: 12px; color: var(--color-fg-muted, #8b949e); margin-bottom: 4px; }
.kpi-value { font-size: 20px; font-weight: 600; color: var(--color-fg-default, #c9d1d9); }
.kpi-unit { font-size: 13px; font-weight: normal; color: #8b949e; }
.kpi-sub { font-size: 11px; color: var(--color-fg-muted, #8b949e); margin-top: 2px; }
.stat-good { color: #3fb950; }
.stat-warn { color: #f85149; }
.context-meter-box {
  margin: 16px 0;
  background: #0d1117;
  padding: 12px 14px;
  border-radius: 6px;
  border: 1px solid #21262d;
}
.meter-labels { display: flex; justify-content: space-between; font-size: 12px; color: #8b949e; margin-bottom: 6px; }
.context-progress-bar {
  height: 10px;
  background: #21262d;
  border-radius: 5px;
  overflow: hidden;
}
.context-progress-fill { height: 100%; transition: width 0.3s ease; }
.prompt-fit-events {
  margin: 16px 0 20px;
  padding: 12px 14px;
  border: 1px solid rgba(88, 166, 255, 0.28);
  border-radius: 8px;
  background: rgba(13, 17, 23, 0.45);
}
.prompt-fit-events h3 { margin: 0 0 4px; }
.prompt-fit-event-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px;
}
.prompt-fit-event-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px;
  border-radius: 6px;
  background: rgba(110, 118, 129, 0.10);
  font-size: 13px;
}
.prompt-fit-event-item strong { color: var(--text-muted, #8b949e); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
.prompt-fit-event-item span { overflow-wrap: anywhere; }
.prompt-fit-event-wide { grid-column: 1 / -1; }

.context-dropped-alert {
  background: rgba(248, 81, 73, 0.1);
  border: 1px solid rgba(248, 81, 73, 0.4);
  color: #f85149;
  border-radius: 6px;
  padding: 10px 14px;
  margin: 12px 0;
  font-size: 13px;
}
.dropped-pill { background: rgba(248, 81, 73, 0.2); padding: 2px 6px; border-radius: 4px; margin: 0 4px; }
.two-tier-canvas {
  display: flex;
  gap: 16px;
  margin: 24px 0;
  flex-wrap: wrap;
}
.tier-col {
  flex: 1;
  min-width: 320px;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 16px;
}
.tier-col-header { display: flex; gap: 10px; align-items: flex-start; margin-bottom: 14px; }
.tier-tag {
  font-size: 11px;
  font-weight: bold;
  padding: 2px 8px;
  border-radius: 4px;
  letter-spacing: 0.5px;
}
.tag-t1 { background: #1f6feb; color: #fff; }
.tag-t2 { background: #8957e5; color: #fff; }
.tier-col-header h3 { font-size: 15px; margin: 0; color: #c9d1d9; }
.tier-col-header p { font-size: 12px; color: #8b949e; margin: 2px 0 0; }
.tier1-blocks-list { display: flex; flex-direction: column; gap: 6px; }
.t1-block-item {
  background: #161b22;
  border: 1px solid #21262d;
  border-radius: 4px;
  padding: 8px 10px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.t1-seq { background: #30363d; color: #c9d1d9; border-radius: 3px; font-size: 10px; font-weight: bold; padding: 1px 5px; }
.t1-name { font-weight: 600; color: #58a6ff; }
.t1-sz { margin-left: auto; font-family: monospace; color: #8b949e; font-size: 12px; }
.t1-desc { font-size: 11px; color: #8b949e; margin-left: 6px; }
.t1-empty { opacity: 0.6; }
.t1-linked { flex-direction: column; align-items: stretch; gap: 4px; }
.t1-row { display: flex; align-items: center; gap: 8px; width: 100%; }
.tier-link-badge {
  display: inline-block;
  background: rgba(137, 87, 229, 0.15);
  color: #a371f7;
  border: 1px solid rgba(137, 87, 229, 0.4);
  font-size: 11px;
  font-weight: 500;
  padding: 3px 8px;
  border-radius: 4px;
  text-decoration: none;
  margin-top: 2px;
}
.tier-link-badge:hover { background: rgba(137, 87, 229, 0.3); text-decoration: none; }
.t1-sep-row { background: transparent; border: 1px dashed #30363d; color: #8b949e; font-size: 11px; font-style: italic; justify-content: space-between; }
.t1-msg { background: rgba(88, 166, 255, 0.05); border-color: rgba(88, 166, 255, 0.2); }
.tier-bridge {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 8px;
}
.bridge-card {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 14px;
  text-align: center;
  max-width: 180px;
}
.bridge-arrow { font-size: 24px; color: #a371f7; display: block; margin-bottom: 4px; }
.bridge-card strong { display: block; font-size: 13px; color: #c9d1d9; }
.bridge-card p { font-size: 11px; color: #8b949e; margin: 2px 0 6px; }
.bridge-cost { font-size: 10px; color: #a371f7; font-weight: 500; }
.tier2-targets-list { display: flex; flex-direction: column; gap: 10px; }
.t2-target-card {
  background: #161b22;
  border: 1px solid #21262d;
  border-radius: 4px;
  padding: 10px 12px;
}
.t2-card-top { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px; }
.t2-size { font-family: monospace; color: #a371f7; font-size: 12px; }
.t2-target-card p { font-size: 12px; color: #8b949e; margin: 0 0 8px; }
.t2-explore-btn { font-size: 11px; color: #58a6ff; text-decoration: none; font-weight: 500; }
.t2-explore-btn:hover { text-decoration: underline; }
.context-detail-section { margin-top: 32px; }
.context-detail-section h3 { font-size: 17px; margin: 0 0 4px; color: #c9d1d9; }
.section-sub { font-size: 13px; color: #8b949e; margin: 0 0 16px; }
.context-block-details {
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 4px;
  margin-bottom: 8px;
  overflow: hidden;
}
.block-summary {
  padding: 10px 12px;
  background: #161b22;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  user-select: none;
}
.block-summary:hover { background: #1f242c; }
.block-seq { background: #30363d; color: #c9d1d9; border-radius: 3px; font-size: 10px; font-weight: bold; padding: 1px 5px; }
.block-title { color: #58a6ff; }
.block-label { color: #8b949e; font-size: 12px; }
.block-meta { margin-left: auto; font-family: monospace; color: #8b949e; font-size: 12px; }
.block-body { padding: 12px; max-height: 400px; overflow-y: auto; background: #0d1117; }
.block-body pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; font-family: monospace; color: #c9d1d9; }
.context-block-empty {
  background: #0d1117;
  border: 1px dashed #30363d;
  border-radius: 4px;
  padding: 8px 12px;
  font-size: 12px;
  color: #8b949e;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.reconciliation-box {
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 14px 16px;
  margin: 18px 0;
}
.rec-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.rec-header h4 { margin: 0; font-size: 14px; color: #c9d1d9; }
.reconciliation-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.reconciliation-table th, .reconciliation-table td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #21262d; }
.reconciliation-table th { color: #8b949e; font-weight: 500; }
.reconciliation-table .num { text-align: right; font-family: monospace; }
.reconciliation-table .sep-row { background: rgba(137, 87, 229, 0.05); color: #a371f7; }
.reconciliation-table .muted-row { opacity: 0.6; }
.reconciliation-table .total-row { border-top: 2px solid #30363d; font-weight: bold; background: #161b22; }
.rec-note { font-size: 11px; color: #8b949e; margin: 8px 0 0; }
.rec-unavailable { border-color: rgba(210, 153, 34, 0.4); background: rgba(210, 153, 34, 0.05); }
.tier2-deep-section { margin-top: 32px; border-top: 1px solid #30363d; padding-top: 24px; }
.t2-group { margin-bottom: 24px; }
.t2-group-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }
.t2-group-header h4 { margin: 0; font-size: 15px; color: #c9d1d9; }
.t2-group-note { font-size: 12px; color: #8b949e; }
.skills-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.skill-asset-card {
  background: #0d1117;
  border: 1px solid #21262d;
  border-radius: 6px;
  padding: 12px;
  display: flex;
  flex-direction: column;
}
.skill-card-head { display: flex; justify-content: space-between; font-size: 13px; font-weight: 600; margin-bottom: 4px; }
.skill-card-name { color: #58a6ff; font-family: monospace; }
.skill-card-size { color: #8b949e; font-size: 11px; }
.skill-card-desc { font-size: 12px; color: #8b949e; margin: 0 0 10px; line-height: 1.4; flex-grow: 1; }
.skill-card-details summary { font-size: 11px; color: #a371f7; cursor: pointer; user-select: none; }
.skill-card-details pre { margin: 8px 0 0; max-height: 280px; overflow-y: auto; background: #161b22; padding: 8px; border-radius: 4px; font-size: 11px; }
.missing-artifact-callout {
  background: rgba(248, 81, 73, 0.1);
  border: 1px solid rgba(248, 81, 73, 0.4);
  border-radius: 6px;
  padding: 12px 14px;
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
  font-size: 13px;
  color: #f85149;
}
.callout-icon { font-size: 18px; }
.missing-artifact-callout p { margin: 2px 0 0; font-size: 12px; color: #c9d1d9; }
.lessons-pills, .memory-pills { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.lesson-pill, .memory-pill {
  background: #161b22;
  border: 1px solid #21262d;
  border-radius: 4px;
  padding: 3px 8px;
  font-size: 11px;
  color: #8b949e;
  font-family: monospace;
}
.pill-more { background: transparent; border-style: dashed; color: #58a6ff; font-family: sans-serif; }

/* #301: recorded-order Tier 1 blocks, owner badges, flags, rule owners & user-message sections */
.t1-owner {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: .03em;
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid #30363d;
  color: #8b949e;
}
.t1-owner-release { color: #58a6ff; border-color: rgba(88, 166, 255, 0.4); }
.t1-owner-instance { color: #d29922; border-color: rgba(210, 153, 34, 0.4); }
.t1-owner-generated { color: #a371f7; border-color: rgba(163, 113, 247, 0.4); }
.t1-owner-unmapped { color: #f85149; border-color: rgba(248, 81, 73, 0.4); }
.badge-flag {
  font-size: 10px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 3px;
  margin-left: 4px;
}
.badge-flag-missing { background: rgba(248, 81, 73, 0.2); color: #f85149; }
.badge-flag-truncated { background: rgba(210, 153, 34, 0.2); color: #d29922; }
.block-preview { font-size: 11px; color: #6e7681; font-style: italic; margin-left: 8px; }
.user-message-sections, .rule-owners-panel { margin-top: 18px; }
.user-message-sections h4, .rule-owners-panel h4 { margin: 0 0 8px; font-size: 14px; color: #c9d1d9; }

/* ozand/eeebot#1755: truncation/drop streak + window-pressure indicators */
.context-indicator-box {
  border-radius: 6px;
  padding: 10px 14px;
  margin: 10px 0;
  font-size: 13px;
}
.context-indicator-box .indicator-note { color: #8b949e; font-size: 12px; margin-left: 6px; }
.context-indicator-box .indicator-note code { color: inherit; }
.context-indicator-box .indicator-pills { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }
.indicator-no-data { background: rgba(139, 148, 158, 0.08); border: 1px solid rgba(139, 148, 158, 0.3); }
.indicator-healthy { background: rgba(46, 160, 67, 0.08); border: 1px solid rgba(46, 160, 67, 0.3); }
.indicator-alarm { background: rgba(248, 81, 73, 0.08); border: 1px solid rgba(248, 81, 73, 0.4); }
.indicator-window-pressure { background: rgba(88, 166, 255, 0.06); border: 1px solid rgba(88, 166, 255, 0.25); }
"""
