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
from datetime import timezone, timedelta
from pathlib import Path
from typing import Any

MSK_TZ = timezone(timedelta(hours=3))

SEPARATOR = "\n\n---\n\n"
SEPARATOR_LEN = len(SEPARATOR)  # 7 characters

CANONICAL_ASSEMBLY_ORDER = [
    ("identity", "Identity & Role"),
    ("bootstrap", "Bootstrap (AGENTS.md)"),
    ("active_skills", "Active Skills (Always Loaded)"),
    ("skills_catalogue", "Skills Catalogue (Index)"),
    ("memory", "Working Memory"),
    ("goals", "Operator Charter / Goals"),
]


def esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def estimate_tokens(chars: int) -> int:
    """Heuristic estimator: ~4 characters per token for mixed code/English prompts."""
    return max(1, chars // 4) if chars > 0 else 0


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

    capped_names = [
        name for name, _label in CANONICAL_ASSEMBLY_ORDER
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


def read_agent_context_dict(state_root: Path, instance_repo: Path | str | None = None) -> dict[str, Any]:
    """Read context telemetry and Tier 2 corpus from the instance workspace.

    ``state_root`` contains runtime telemetry; the corpus writer is the
    instance repository.  Never fall back to the runtime/release tree: a
    missing or failed corpus read must stay visible as a state, not become a
    fabricated empty count.
    """
    state_root = Path(state_root)
    inst_path = Path(instance_repo) if instance_repo else None

    def corpus_status(path: Path, *, read_error: bool = False) -> str:
        if read_error:
            return "unavailable"
        if not path.exists():
            return "missing"
        return "present"

    def read_dir_files(root: Path, matcher: Any) -> tuple[list[dict[str, Any]], str]:
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

    # 1. Scan ledger/cycles.jsonl for latest system_prompt row
    lpath = state_root / "ledger" / "cycles.jsonl"
    sys_prompt_row: dict[str, Any] | None = None
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
                        sys_prompt_row = obj
        except Exception:
            pass

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

    lessons_dir = inst_path / "lessons" if inst_path else Path("__missing_instance_repo__/lessons")
    lesson_files, lessons_status = read_dir_files(
        lessons_dir,
        lambda path: path.suffix == ".md" and path.name != "index.md",
    )
    lessons_index_path = lessons_dir / "index.md"
    lessons_index_status = corpus_status(lessons_index_path)
    lessons_files: list[dict[str, Any]] = []
    total_lessons_size = 0
    for path in lesson_files:
        try:
            size = path.stat().st_size
            total_lessons_size += size
            lessons_files.append({"name": path.relative_to(inst_path).as_posix(), "size_bytes": size})
        except (OSError, ValueError):
            lessons_status = "unavailable"

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
        "prompt_text": prompt_text,
        "task_text": task_text,
        "tier2_skills": skills_list,
        "tier2_skills_status": skills_status,
        "tier2_lessons": {
            "index_status": lessons_index_status,
            "corpus_status": lessons_status,
            "corpus_count": len(lessons_files),
            "total_size_bytes": total_lessons_size,
            "files": lessons_files[:50],
        },
        "tier2_memory": {
            "index_status": memory_index_status,
            "corpus_status": memory_status,
            "total_files": len(memory_files),
            "total_size_bytes": total_memory_size,
            "files": memory_files[:50],
        },
    }

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
    separator_count = max(
        0,
        sum(
            1 for key, _label in CANONICAL_ASSEMBLY_ORDER
            if sections and key in sections and (sections.get(key) or 0) > 0
        ) - 1,
    )
    separator_total_chars = separator_count * SEPARATOR_LEN
    dropped = sys_prompt.get("dropped") or []
    rung = sys_prompt.get("rung")
    cid = sys_prompt.get("cycle_id", "")
    ts_str = str(sys_prompt.get("ts") or "")
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
    outside_cap_by_name = {item["name"]: item for item in outside_cap}
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
    active_skills_sz = sections.get("active_skills") if sections and "active_skills" in sections else None
    active_skills_text = (
        f"{active_skills_sz:,}c (empty under loop profile)"
        if active_skills_sz == 0
        else f"{active_skills_sz:,}c"
        if active_skills_sz is not None
        else "unavailable"
    )
    cat_sz = sections.get("skills_catalogue", 0) if sections else len(raw_sections_text.get("skills_catalogue", ""))
    mem_sz = sections.get("memory", 0) if sections else len(raw_sections_text.get("memory", ""))
    id_sz = sections.get("identity", 0) if sections else len(raw_sections_text.get("identity", ""))
    boot_sz = sections.get("bootstrap", 0) if sections else len(raw_sections_text.get("bootstrap", ""))

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
    out.append(f'        <div class="t1-block-item"><span class="t1-seq">1</span><span class="t1-name">identity</span><span class="t1-sz">{id_sz:,}c</span></div>')
    out.append(f'        <div class="t1-block-item"><span class="t1-seq">2</span><span class="t1-name">bootstrap (AGENTS.md)</span><span class="t1-sz">{boot_sz:,}c</span></div>')
    active_class = "t1-empty" if active_skills_sz == 0 else ""
    out.append(f'        <div class="t1-block-item {active_class}"><span class="t1-seq">3</span><span class="t1-name">active_skills</span><span class="t1-sz">{active_skills_text}</span></div>')
    out.append(f'        <div class="t1-block-item t1-linked"><div class="t1-row"><span class="t1-seq">4</span><span class="t1-name">skills_catalogue</span><span class="t1-sz">{cat_sz:,}c</span></div><a href="#tier2-skills-section" class="tier-link-badge tier-link-origin">&#10140; Indexes {corpus_count(skills_status, len(skills))} Skills in Tier 2 ({skills_kb:.1f} KB)</a></div>')
    out.append(f'        <div class="t1-block-item t1-linked"><div class="t1-row"><span class="t1-seq">5</span><span class="t1-name">memory</span><span class="t1-sz">{mem_sz:,}c</span></div><a href="#tier2-memory-section" class="tier-link-badge">&#10140; Indexes {corpus_count(mem_corpus_status, mem_cnt)} files in Tier 2</a></div>')
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
    out.append('    <h3>Tier 1: Assembled Context Blocks (Strict Assembly Order)</h3>')
    out.append('    <p class="section-sub">Blocks strictly follow context.py assembly order. Click to view exact text.</p>')

    block_seq = 1
    reconciliation_rows = []
    total_sections_chars = 0

    if sections:
        for key, label in CANONICAL_ASSEMBLY_ORDER:
            sec_sz = sections.get(key)
            # Check if key is explicitly present in sections (including 0)
            if key in sections:
                sec_sz = sec_sz or 0
                sec_tokens = estimate_tokens(sec_sz)
                total_sections_chars += sec_sz
                sec_text = raw_sections_text.get(key, "")
                if sec_sz == 0:
                    reconciliation_rows.append(f'<tr class="muted-row"><td><code>{esc(key)}</code></td><td>{esc(label)}</td><td class="num">0</td><td class="num">0</td><td>0c (empty under loop profile)</td></tr>')
                    out.append(f'<div class="context-block-empty"><span class="block-seq">#{block_seq}</span><strong>{esc(key)}</strong> &mdash; 0 chars (empty under loop profile)</div>')
                else:
                    reconciliation_rows.append(f"<tr><td><code>{esc(key)}</code></td><td>{esc(label)}</td><td class=\"num\">{sec_sz:,}</td><td class=\"num\">~{sec_tokens:,}</td><td>{sec_sz:,}c</td></tr>")
                    out.append(f'<details class="context-block-details"><summary class="block-summary"><span class="block-seq">#{block_seq}</span><strong class="block-title">{esc(key)}</strong><span class="block-label">({esc(label)})</span><span class="block-meta">{sec_sz:,} chars &bull; ~{sec_tokens:,} tokens</span></summary><div class="block-body"><pre><code>{esc(sec_text if sec_text else "(section text not captured in prompt file)")}</code></pre></div></details>')
                block_seq += 1
            else:
                outside = outside_cap_by_name.get(key)
                if outside is not None:
                    outside_size = outside["actual_chars"]
                    reconciliation_rows.append(f'<tr class="muted-row"><td><code>{esc(key)}</code></td><td>{esc(label)}</td><td class="num"><em>{outside_size:,}</em></td><td class="num">~{estimate_tokens(outside_size):,}</td><td>outside capped prompt</td></tr>')
                    out.append(f'<details class="context-block-details outside-cap-block"><summary class="block-summary"><span class="block-seq">#{block_seq}</span><strong class="block-title">{esc(key)}</strong><span class="block-label">({esc(label)})</span><span class="block-meta">{outside_size:,} chars &bull; outside capped prompt</span></summary><div class="block-body"><pre><code>{esc(outside["text"])}</code></pre></div></details>')
                else:
                    reconciliation_rows.append(f'<tr class="muted-row"><td><code>{esc(key)}</code></td><td>{esc(label)}</td><td class="num"><em>absent</em></td><td class="num">-</td><td>absent from breakdown</td></tr>')
                    out.append(f'<div class="context-block-absent"><span class="block-seq">#{block_seq}</span><strong>{esc(key)}</strong> &mdash; <em>absent</em> (not configured/emitted)</div>')
                block_seq += 1

        sep_count = separator_count
        sep_total_chars = separator_total_chars
        reconciled_total = total_sections_chars + sep_total_chars

        reconciliation_rows.append(f'<tr class="subtotal-row"><td colspan="2"><strong>Sum of Sections</strong></td><td class="num"><strong>{total_sections_chars:,}</strong></td><td class="num">~{estimate_tokens(total_sections_chars):,}</td><td>&sum; section chars</td></tr>')
        reconciliation_rows.append(f'<tr class="sep-row"><td colspan="2"><strong>Separators (\\n\\n---\\n\\n)</strong></td><td class="num"><strong>{sep_total_chars:,}</strong></td><td class="num">~{estimate_tokens(sep_total_chars):,}</td><td>{sep_count} &times; {SEPARATOR_LEN} chars</td></tr>')
        is_match = (reconciled_total == total_chars)
        match_badge = '<span class="status-badge status-present">&#10003; Exact Match &bull; Reconciliation verified</span>' if is_match else f'<span class="status-badge status-missing">Diff: {reconciled_total - total_chars:+d}c</span>'

        out.append('    <div class="reconciliation-box">')
        out.append(f'      <div class="rec-header"><h4>Arithmetic Character Reconciliation</h4>{match_badge}</div>')
        out.append('      <table class="reconciliation-table">')
        out.append('        <thead><tr><th>Key</th><th>Section</th><th class="num">Chars</th><th class="num">Est. Tokens</th><th>Formula Component</th></tr></thead>')
        out.append(f'        <tbody>{"".join(reconciliation_rows)}<tr class="total-row"><td colspan="2"><strong>Total System Prompt</strong></td><td class="num"><strong>{reconciled_total:,}</strong></td><td class="num"><strong>~{estimate_tokens(reconciled_total):,}</strong></td><td><strong>Recorded chars: {total_chars:,}</strong></td></tr></tbody>')
        out.append('      </table>')
        out.append(f'      <p class="rec-note">Formula: &sum;(capped sections: {total_sections_chars:,}c) + {sep_count} separators &times; {SEPARATOR_LEN}c ({sep_total_chars:,}c) = {reconciled_total:,} chars. Outside-cap system context is not included.</p>')
        if parsed_prompt["status"] == "exact":
            evidence = " ".join(
                f'<code>{esc(name)}</code>: recorded {details["recorded_chars"]:,} / parsed {details["actual_chars"]:,} chars (exact)'
                for name, details in parsed_prompt["sections"].items()
            )
            out.append(f'      <p class="rec-note">Structural prompt parse: {evidence}</p>')
        else:
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

    rendered_outside_names = {"goals"}
    for outside in outside_cap:
        if outside["name"] in rendered_outside_names:
            continue
        out.append(f'<details class="context-block-details outside-cap-block"><summary class="block-summary"><strong class="block-title">{esc(outside["name"])}</strong><span class="block-meta">{outside["actual_chars"]:,} chars &bull; outside capped prompt</span></summary><div class="block-body"><pre><code>{esc(outside["text"])}</code></pre></div></details>')

    if task_text:
        t_sz = len(task_text)
        out.append(f'<details class="context-block-details user-block-details"><summary class="block-summary"><span class="block-seq">#{block_seq}</span><strong class="block-title">user (runtime_context + task)</strong><span class="block-meta">{t_sz:,} chars &bull; ~{estimate_tokens(t_sz):,} tokens</span></summary><div class="block-body"><pre><code>{esc(task_text)}</code></pre></div></details>')

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
"""
