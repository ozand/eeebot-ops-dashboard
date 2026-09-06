"""Issue #227: Two-tier agent context model reader and renderer.
Visualizes:
- Tier 1: In active context (system prompt sections in assembly order, history, user)
- Tier 2: Reachable on disk (skills, lessons, memory)
Zero import of `nanobot` runtime; uses recorded data and filesystem.
"""
from __future__ import annotations

import html
import json
import os
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


def read_agent_context_dict(state_root: Path, instance_repo: Path | str | None = None) -> dict[str, Any]:
    """Read agent context data locally from state_root and instance_repo."""
    state_root = Path(state_root)
    inst_path = Path(instance_repo) if instance_repo else None

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
    skills_list: list[dict[str, Any]] = []
    if inst_path and inst_path.is_dir():
        skills_dir = inst_path / "skills"
        if skills_dir.is_dir():
            try:
                for sdir in sorted(skills_dir.iterdir()):
                    s_file = sdir / "SKILL.md"
                    if s_file.is_file():
                        size = s_file.stat().st_size
                        content = ""
                        desc = ""
                        try:
                            content = s_file.read_text(encoding="utf-8", errors="replace")[:15000]
                            lines = [line_str.strip() for line_str in content.splitlines() if line_str.strip()]
                            for line_str in lines:
                                if not line_str.startswith("#") and len(line_str) > 10:
                                    desc = line_str
                                    break
                        except Exception:
                            pass
                        skills_list.append({
                            "name": sdir.name,
                            "size_bytes": size,
                            "desc": desc[:200],
                            "content": content,
                            "path": f"skills/{sdir.name}/SKILL.md",
                        })
            except Exception:
                pass

    # 3. Tier 2: Lessons
    lessons_index_status = "missing"
    lessons_files: list[dict[str, Any]] = []
    total_lessons_size = 0
    if inst_path and inst_path.is_dir():
        lessons_dir = inst_path / "lessons"
        lessons_index_path = lessons_dir / "index.md"
        lessons_index_status = "present" if lessons_index_path.is_file() else "missing"
        if lessons_dir.is_dir():
            try:
                for f in sorted(lessons_dir.iterdir()):
                    if f.name.endswith(".md") and f.name != "index.md":
                        sz = f.stat().st_size
                        total_lessons_size += sz
                        lessons_files.append({"name": f.name, "size_bytes": sz})
            except Exception:
                pass

    # 4. Tier 2: Memory
    memory_index_status = "missing"
    memory_files: list[dict[str, Any]] = []
    total_memory_size = 0
    if inst_path and inst_path.is_dir():
        memory_dir = inst_path / "memory"
        memory_index_path = memory_dir / "index.md"
        memory_index_status = "present" if memory_index_path.is_file() else "missing"
        if memory_dir.is_dir():
            try:
                for root_m, _, files_m in os.walk(memory_dir):
                    for fm in files_m:
                        fpath = Path(root_m) / fm
                        try:
                            rel = str(fpath.relative_to(inst_path)).replace("\\", "/")
                            sz = fpath.stat().st_size
                            total_memory_size += sz
                            memory_files.append({"name": rel, "size_bytes": sz})
                        except Exception:
                            pass
            except Exception:
                pass

    return {
        "system_prompt": sys_prompt_row,
        "prompt_text": prompt_text,
        "task_text": task_text,
        "tier2_skills": skills_list,
        "tier2_lessons": {
            "index_status": lessons_index_status,
            "corpus_count": len(lessons_files),
            "total_size_bytes": total_lessons_size,
            "files": lessons_files[:50],
        },
        "tier2_memory": {
            "index_status": memory_index_status,
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
    lessons = agent_context.get("tier2_lessons") or {}
    memory = agent_context.get("tier2_memory") or {}

    chars = sys_prompt.get("chars")
    cap = sys_prompt.get("cap", 30000)
    overflow = sys_prompt.get("overflow", False)
    over_by = sys_prompt.get("over_by", 0)
    sections = sys_prompt.get("sections")
    dropped = sys_prompt.get("dropped") or []
    cid = sys_prompt.get("cycle_id", "")
    ts_str = str(sys_prompt.get("ts") or "")

    ts_display = ts_str.replace("T", " ").replace("Z", " MSK") if ts_str else "active cycle"

    raw_sections_text: dict[str, str] = {}
    if prompt_text:
        for p in prompt_text.split(SEPARATOR):
            lines = [line_str.strip() for line_str in p.splitlines() if line_str.strip()]
            fl = lines[0].lower() if lines else ""
            if "nanobot" in fl or "identity" in fl:
                raw_sections_text["identity"] = p
            elif "agents.md" in fl or "bootstrap" in fl:
                raw_sections_text["bootstrap"] = p
            elif "# skills" in fl or "skills catalogue" in fl or "catalogue" in fl:
                raw_sections_text["skills_catalogue"] = p
            elif "# memory" in fl or "working memory" in fl or "facts" in fl:
                raw_sections_text["memory"] = p
            elif "charter" in fl or "goals" in fl or "immutable" in fl:
                raw_sections_text["goals"] = p

    if chars is None and sections:
        chars = sum(sections.values()) + max(0, len(sections) - 1) * SEPARATOR_LEN

    total_chars = chars or (len(prompt_text) if prompt_text else 0)
    total_tokens = estimate_tokens(total_chars)

    if overflow or (total_chars and cap and total_chars > cap):
        ov_amount = over_by if over_by else (total_chars - cap)
        headroom_badge = f'<span class="context-badge badge-danger context-badge-overflow">OVERFLOW (+{ov_amount:,} chars over cap)</span>'
        headroom_text = f'<span class="stat-warn">-{ov_amount:,} chars (OVERFLOW)</span>'
        bar_pct = min(100, int((total_chars / cap) * 100)) if cap else 100
        bar_color = "var(--color-danger, #f85149)"
    elif total_chars and cap:
        spare = cap - total_chars
        pct = (total_chars / cap) * 100
        headroom_badge = f'<span class="context-badge badge-success context-badge-safe">WITHIN BUDGET (+{spare:,} chars spare)</span>'
        headroom_text = f'<span class="stat-good">+{spare:,} chars ({spare/cap*100:.1f}%)</span>'
        bar_pct = min(100, int(pct))
        bar_color = "var(--color-warning, #d29922)" if pct > 85 else "var(--color-success, #2ea043)"
    else:
        headroom_badge = '<span class="context-badge badge-secondary">CAPACITY UNKNOWN</span>'
        headroom_text = '<span>n/a</span>'
        bar_pct = 50
        bar_color = "var(--color-accent, #58a6ff)"

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
    lessons_cnt = lessons.get("corpus_count", 0)
    lessons_kb = lessons.get("total_size_bytes", 0) / 1024
    mem_st = memory.get("index_status", "unknown")
    mem_cnt = memory.get("total_files", 0)
    mem_kb = memory.get("total_size_bytes", 0) / 1024
    t2_kb = skills_kb + lessons_kb + mem_kb
    t2_files = len(skills) + lessons_cnt + mem_cnt

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
    out.append(f'      <span class="cycle-pill">{esc(cid or "latest")}</span>')
    out.append('    </div>')
    out.append('  </div>')

    out.append('  <div class="context-kpis">')
    out.append('    <div class="context-kpi-card">')
    out.append('      <span class="kpi-label">Active Prompt Load</span>')
    out.append(f'      <span class="kpi-value">{total_chars:,} <span class="kpi-unit">chars</span></span>')
    out.append(f'      <span class="kpi-sub">~{total_tokens:,} est. tokens</span>')
    out.append('    </div>')
    out.append('    <div class="context-kpi-card">')
    out.append('      <span class="kpi-label">Context Budget Cap</span>')
    out.append(f'      <span class="kpi-value">{cap:,} <span class="kpi-unit">chars</span></span>')
    out.append(f'      <span class="kpi-sub">~{estimate_tokens(cap):,} est. tokens limit</span>')
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

    out.append('  <div class="context-meter-box">')
    out.append(f'    <div class="meter-labels"><span>Prompt Budget Utilization: <strong>{bar_pct}%</strong> ({total_chars:,} / {cap:,} chars)</span><span>{ts_display}</span></div>')
    out.append('    <div class="context-progress-bar">')
    out.append(f'      <div class="context-progress-fill" style="width:{bar_pct}%;background:{bar_color};"></div>')
    out.append('    </div>')
    out.append('  </div>')

    if dropped_html:
        out.append(f'  {dropped_html}')

    out.append('  <div class="two-tier-canvas">')
    out.append('    <div class="tier-col tier1-col">')
    out.append('      <div class="tier-col-header"><span class="tier-tag tag-t1">TIER 1</span><div><h3>In Active Context (Attention Window)</h3><p>Assembled into system and user messages.</p></div></div>')
    out.append('      <div class="tier1-blocks-list">')
    out.append(f'        <div class="t1-block-item"><span class="t1-seq">1</span><span class="t1-name">identity</span><span class="t1-sz">{id_sz:,}c</span></div>')
    out.append(f'        <div class="t1-block-item"><span class="t1-seq">2</span><span class="t1-name">bootstrap (AGENTS.md)</span><span class="t1-sz">{boot_sz:,}c</span></div>')
    out.append('        <div class="t1-block-item t1-empty"><span class="t1-seq">3</span><span class="t1-name">active_skills</span><span class="t1-sz">0c (empty under loop profile)</span></div>')
    out.append(f'        <div class="t1-block-item t1-linked"><div class="t1-row"><span class="t1-seq">4</span><span class="t1-name">skills_catalogue</span><span class="t1-sz">{cat_sz:,}c</span></div><a href="#tier2-skills-section" class="tier-link-badge tier-link-origin">&#10140; Indexes {len(skills)} Skills in Tier 2 ({skills_kb:.1f} KB)</a></div>')
    out.append(f'        <div class="t1-block-item t1-linked"><div class="t1-row"><span class="t1-seq">5</span><span class="t1-name">memory</span><span class="t1-sz">{mem_sz:,}c</span></div><a href="#tier2-memory-section" class="tier-link-badge">&#10140; Indexes Working Memory in Tier 2 ({mem_cnt} files)</a></div>')
    out.append('        <div class="t1-block-item t1-sep-row"><span class="t1-name">&#8230; 4 &times; "\n\n---\n\n" Separators</span><span class="t1-sz">28c</span></div>')
    out.append('        <div class="t1-block-item t1-msg"><span class="t1-seq">msg</span><span class="t1-name">history [1..n]</span><span class="t1-desc">turn messages</span></div>')
    out.append(f'        <div class="t1-block-item t1-msg"><span class="t1-seq">user</span><span class="t1-name">runtime_context + task</span><span class="t1-sz">{len(task_text) if task_text else 0:,}c</span></div>')
    out.append('      </div>')
    out.append('    </div>')

    out.append('    <div class="tier-bridge">')
    out.append('      <div class="bridge-card">')
    out.append('        <span class="bridge-arrow">&#10132;</span>')
    out.append('        <strong>On-Demand Read</strong>')
    out.append('        <p>read_file tool call</p>')
    out.append(f'        <span class="bridge-cost">Catalogue costs {cat_sz:,}c &mdash; unlocks {skills_kb:.1f} KB</span>')
    out.append('      </div>')
    out.append('    </div>')

    out.append('    <div class="tier-col tier2-col">')
    out.append('      <div class="tier-col-header"><span class="tier-tag tag-t2">TIER 2</span><div><h3>Reachable on Disk (Zero Base Context)</h3><p>Full instructions, lessons, and memory records.</p></div></div>')
    out.append('      <div class="tier2-targets-list">')
    out.append(f'        <div class="t2-target-card"><div class="t2-card-top"><strong>Skills Store: {len(skills)} skills</strong><span class="t2-size">{skills_kb:.1f} KB</span></div><p>Full SKILL.md specs.</p><a href="#tier2-skills-section" class="t2-explore-btn">Inspect Skills &darr;</a></div>')
    out.append(f'        <div class="t2-target-card"><div class="t2-card-top"><strong>Lessons Corpus: {lessons_cnt} lessons</strong><span class="t2-size">{lessons_kb:.1f} KB</span></div><p>Index status: <span class="status-badge status-{lessons_st}">{lessons_st}</span></p><a href="#tier2-lessons-section" class="t2-explore-btn">Inspect Lessons &darr;</a></div>')
    out.append(f'        <div class="t2-target-card"><div class="t2-card-top"><strong>Memory Store: {mem_cnt} files</strong><span class="t2-size">{mem_kb:.1f} KB</span></div><p>Index status: <span class="status-badge status-{mem_st}">{mem_st}</span></p><a href="#tier2-memory-section" class="t2-explore-btn">Inspect Memory &darr;</a></div>')
    out.append('      </div>')
    out.append('    </div>')
    out.append('  </div>')

    out.append('  <div class="context-detail-section">')
    out.append('    <h3>Tier 1: Assembled Context Blocks (Strict Assembly Order)</h3>')
    out.append('    <p class="section-sub">Blocks strictly follow context.py assembly order. Click to view exact text.</p>')

    block_seq = 1
    reconciliation_rows = []
    total_sections_chars = 0
    non_empty_count = 0

    if sections:
        for key, label in CANONICAL_ASSEMBLY_ORDER:
            sec_sz = sections.get(key)
            if sec_sz is not None:
                sec_tokens = estimate_tokens(sec_sz)
                total_sections_chars += sec_sz
                if sec_sz > 0:
                    non_empty_count += 1
                sec_text = raw_sections_text.get(key, "")
                reconciliation_rows.append(f"<tr><td><code>{esc(key)}</code></td><td>{esc(label)}</td><td class=\"num\">{sec_sz:,}</td><td class=\"num\">~{sec_tokens:,}</td><td>{sec_sz:,}c</td></tr>")
                out.append(f'<details class="context-block-details"><summary class="block-summary"><span class="block-seq">#{block_seq}</span><strong class="block-title">{esc(key)}</strong><span class="block-label">({esc(label)})</span><span class="block-meta">{sec_sz:,} chars &bull; ~{sec_tokens:,} tokens</span></summary><div class="block-body"><pre><code>{esc(sec_text if sec_text else "(section text not captured in prompt file)")}</code></pre></div></details>')
                block_seq += 1
            elif key == "active_skills":
                reconciliation_rows.append('<tr class="muted-row"><td><code>active_skills</code></td><td>Active Skills (Always Loaded)</td><td class="num">0</td><td class="num">0</td><td>0c (empty under loop profile)</td></tr>')
                out.append(f'<div class="context-block-empty"><span class="block-seq">#{block_seq}</span><strong>active_skills</strong> &mdash; 0 chars (empty under current loop profile)</div>')
                block_seq += 1

        sep_count = max(0, non_empty_count - 1)
        sep_total_chars = sep_count * SEPARATOR_LEN
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
        out.append(f'      <p class="rec-note">Formula: &sum;(sections: {total_sections_chars:,}c) + {sep_count} separators &times; {SEPARATOR_LEN}c ({sep_total_chars:,}c) = {reconciled_total:,} chars.</p>')
        out.append('    </div>')
    else:
        out.append('    <div class="reconciliation-box rec-unavailable">')
        out.append('      <div class="rec-header"><h4>Arithmetic Character Reconciliation</h4><span class="status-badge status-missing">sections: unavailable</span></div>')
        out.append(f'      <p class="unavailable-note"><strong>sections breakdown: unavailable</strong> &mdash; This cycle row was recorded prior to structured section logging (#1379). Recorded total chars: <strong>{total_chars:,}</strong>. Per honesty rules, section sizes are not reconstructed.</p>')
        out.append('    </div>')
        if prompt_text:
            out.append(f'<details class="context-block-details"><summary class="block-summary"><span class="block-seq">#1</span><strong class="block-title">system_prompt (full text)</strong><span class="block-meta">{total_chars:,} chars &bull; ~{total_tokens:,} tokens</span></summary><div class="block-body"><pre><code>{esc(prompt_text)}</code></pre></div></details>')

    if task_text:
        t_sz = len(task_text)
        out.append(f'<details class="context-block-details user-block-details"><summary class="block-summary"><span class="block-seq">#{block_seq}</span><strong class="block-title">user (runtime_context + task)</strong><span class="block-meta">{t_sz:,} chars &bull; ~{estimate_tokens(t_sz):,} tokens</span></summary><div class="block-body"><pre><code>{esc(task_text)}</code></pre></div></details>')

    out.append('  </div>')

    out.append('  <div class="tier2-deep-section">')
    out.append('    <h3>Tier 2: Reachable On-Demand Knowledge Base</h3>')
    out.append(f'    <p class="section-sub">Assets residing on disk, accessible by tool calls during cycle loop. Total: <strong>~{t2_kb:.1f} KB</strong> across <strong>{t2_files}</strong> files.</p>')

    out.append('    <div class="t2-group" id="tier2-skills-section">')
    out.append(f'      <div class="t2-group-header"><h4>Skills Store ({len(skills)} skills &bull; ~{skills_kb:.1f} KB)</h4><span class="t2-group-note">Indexed in Tier 1 via <code>skills_catalogue</code> ({cat_sz:,} chars)</span></div>')
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
    out.append(f'      <div class="t2-group-header"><h4>Lessons Corpus</h4><span class="status-badge status-{lessons_st}">lessons/index.md: {lessons_st.upper()}</span></div>')
    if lessons_st == "missing":
        out.append(f'      <div class="missing-artifact-callout"><span class="callout-icon">&#8505;</span><div><strong>lessons/index.md is MISSING</strong><p>The lessons index is generated once daily and cleared between cycles by <code>git clean -fd</code>. The underlying corpus of <strong>{lessons_cnt} lesson files</strong> (~{lessons_kb:.1f} KB) remains intact on disk.</p></div></div>')
    out.append(f'      <div class="lessons-compact-list"><p><strong>Corpus Files:</strong> {lessons_cnt} lesson records on disk (~{lessons_kb:.1f} KB total):</p><div class="lessons-pills">')
    for lf in lessons.get("files", [])[:30]:
        out.append(f'<span class="lesson-pill">{esc(lf.get("name", ""))} ({lf.get("size_bytes", 0):,} B)</span> ')
    if lessons_cnt > 30:
        out.append(f'<span class="lesson-pill pill-more">+{lessons_cnt - 30} more lessons...</span>')
    out.append('      </div></div>')
    out.append('    </div>')

    out.append('    <div class="t2-group" id="tier2-memory-section">')
    out.append(f'      <div class="t2-group-header"><h4>Working Memory Store</h4><span class="status-badge status-{mem_st}">memory/index.md: {mem_st.upper()}</span></div>')
    out.append(f'      <div class="memory-compact-list"><p>Indexed via Tier 1 <code>memory</code> block. <strong>{mem_cnt} files</strong> on disk (~{mem_kb:.1f} KB total):</p><div class="memory-pills">')
    for mf in memory.get("files", [])[:30]:
        out.append(f'<span class="memory-pill">{esc(mf.get("name", ""))} ({mf.get("size_bytes", 0):,} B)</span> ')
    if mem_cnt > 30:
        out.append(f'<span class="memory-pill pill-more">+{mem_cnt - 30} more files...</span>')
    out.append('      </div></div>')
    out.append('    </div>')

    out.append('  </div>')
    out.append('</section>')

    return "\n".join(out)

AGENT_CONTEXT_CSS = """
/* Issue #227: Two-tier agent context visualization styling */
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
