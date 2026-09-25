"""Private cycle-detail formatting primitives (ADR-036 D2)."""
from __future__ import annotations

import gzip
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SECRET_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "bearer"),
    (re.compile(r"(?i)\b(api[_-]?key|token|password)\s*[:=]\s*[^\s,;]+"), "credential"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "api-key"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private-key"),
)
DEFAULT_DISPLAY_LIMIT = 4000


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def redact_text(value: str, *, env_contents: tuple[str, ...] = ()) -> str:
    result = value
    for content in env_contents:
        if content:
            result = result.replace(content, "[redacted: env-file]")
    for pattern, kind in SECRET_PATTERNS:
        result = pattern.sub(f"[redacted: {kind}]", result)
    return result


def display_text(value: str, *, limit: int = DEFAULT_DISPLAY_LIMIT, env_contents: tuple[str, ...] = ()) -> str:
    """Redact and truncate only the display copy, with explicit omitted length."""
    safe = redact_text(value, env_contents=env_contents)
    if len(safe) <= limit:
        return safe
    return f"{safe[:limit]}… {len(safe) - limit} characters not shown"


def _read_jsonl(paths: list[Path]) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            opener = gzip.open if path.name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            return rows, False
    return rows, True


def load_cycle_detail(state_root: Path, cycle_id: str, *, days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    """Read bounded recent run, prompt, duration and compaction records."""
    reference = now or datetime.now(timezone.utc)
    dates = [(reference.date() - timedelta(days=offset)).isoformat() for offset in range(days)]
    run_paths = [p for date in dates for p in (state_root / "bridge" / f"runs-{date}.jsonl", state_root / "bridge" / f"runs-{date}.jsonl.gz") if p.is_file()]
    prompt_paths = [p for date in dates for p in (state_root / "llm_calls" / "prompts" / f"{date}.jsonl", state_root / "llm_calls" / "prompts" / f"{date}.jsonl.gz") if p.is_file()]
    duration_paths = [p for date in dates for p in (state_root / "llm_calls" / f"{date}.jsonl", state_root / "llm_calls" / f"{date}.jsonl.gz") if p.is_file()]
    runs, runs_ok = _read_jsonl(run_paths)
    prompts, prompts_ok = _read_jsonl(prompt_paths)
    durations, durations_ok = _read_jsonl(duration_paths)
    if not runs_ok or not prompts_ok or not durations_ok:
        return {"available": False}
    matching_runs = [row for row in runs if row.get("cycle_id") == cycle_id]
    matching_prompts = [row for row in prompts if row.get("cycle_id") == cycle_id]
    matching_durations = [row for row in durations if row.get("cycle_id") == cycle_id]
    compaction_path = state_root / "compaction" / "journal.jsonl"
    compactions, compactions_ok = _read_jsonl([compaction_path] if compaction_path.is_file() else [])
    if not compactions_ok:
        return {"available": False}
    matching_compactions = [row for row in compactions if row.get("cycle_id") == cycle_id]
    if not matching_runs and not matching_prompts:
        return {"available": False}
    by_seq = {(str(row.get("component")), str(row.get("seq"))): row for row in matching_durations}
    attempts = []
    run_components = {str(row.get("component")) for row in matching_runs if row.get("component")}
    prompt_components = {str(row.get("component")) for row in matching_prompts if row.get("component")}
    session_components = sorted(run_components or prompt_components)
    for run in matching_runs:
        component = run.get("component")
        steps = []
        for prompt in matching_prompts:
            if component and prompt.get("component") != component:
                continue
            timing = by_seq.get((str(prompt.get("component")), str(prompt.get("seq"))), {})
            messages = prompt.get("messages")
            message_text = json.dumps(messages, ensure_ascii=False) if messages is not None else None
            content = prompt.get("content")
            tools = prompt.get("tool_calls")
            steps.append({"kind": "model", "messages": message_text, "answer": content, "tools": json.dumps(tools, ensure_ascii=False) if tools else None, "reasoning": prompt.get("reasoning_content"), "tokens": prompt.get("prompt_tokens", 0) + prompt.get("completion_tokens", 0), "duration": timing.get("duration_ms")})
        compacted = bool(matching_compactions)
        complete = run.get("classification") not in {"unit_timeout", "killed"} and not compacted
        attempts.append({"run_id": run.get("run_id"), "model_call_count": len(steps), "history_complete": complete, "sessions": [{"role": component or "unavailable", "history_complete": complete, "steps": steps}] if steps else None})
    if not attempts:
        attempts = [{"run_id": "unavailable", "model_call_count": 0, "history_complete": False, "sessions": [{"role": component, "history_complete": False, "steps": []} for component in session_components] or None}]
    return {"available": True, "attempts": attempts, "unlinked_run_count": sum(1 for row in runs if not row.get("cycle_id")), "compaction_count": len(matching_compactions), "source_notes": {"runs_without_cycle_id": "unavailable association", "attempt_linkage": "unavailable: no cycle_id on 346/582 inventoried run rows", "tool_steps": "reconstructed from prompt message history; final calls may be absent"}}


def mark_incomplete_history(session: dict[str, Any]) -> dict[str, Any]:
    result = dict(session)
    if result.get("history_complete") is not True:
        result["history_state"] = "history incomplete"
    else:
        result["history_state"] = "complete"
    return result


def format_model_step(step: dict[str, Any]) -> str:
    parts = ["Model step"]
    for label in ("messages", "answer", "tools", "reasoning"):
        if step.get(label) is not None:
            parts.append(f"<p><b>{label.title()}:</b> {_escape(display_text(str(step[label])))}</p>")
    if step.get("tokens") is not None:
        parts.append(f"<p>Tokens: {step['tokens']}</p>")
    if step.get("duration") is not None:
        parts.append(f"<p>Duration: {_escape(display_text(str(step['duration'])))}</p>")
    return "".join(parts)


def format_tool_step(step: dict[str, Any]) -> str:
    name = display_text(str(step.get("name", "unavailable")))
    args = display_text(str(step.get("arguments", "unavailable")))
    result = display_text(str(step.get("result", "unavailable")))
    duration_value = step.get("duration")
    duration = display_text(str(duration_value)) if duration_value is not None else "unknown"
    return f"<p>Tool step: {_escape(name)}({_escape(args)}) → {_escape(result)}; status: {_escape(display_text(str(step.get('status', 'unavailable'))))}; duration: {_escape(duration)}</p>"


def render_cycle_page(cycle_id: str, data: dict[str, Any] | None) -> str:
    """Render private synthetic cycle detail; unknown/missing sources are explicit."""
    if not isinstance(data, dict) or data.get("available") is False:
        return f'<main><h1>Cycle {cycle_id}</h1><p class="unavailable">Cycle detail unavailable: source data unavailable.</p></main>'
    attempts = data.get("attempts")
    if not isinstance(attempts, list):
        return f'<main><h1>Cycle {cycle_id}</h1><p class="unavailable">Cycle detail unavailable: attempt data unavailable.</p></main>'
    rows = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        rows.append(f'<article><h2>Attempt {_escape(str(attempt.get("run_id", "unavailable")))}</h2><p>Model calls: {_escape(str(attempt.get("model_call_count", "unavailable")))}</p><p>{"history incomplete" if attempt.get("history_complete") is not True else "history complete"}</p>')
        sessions = attempt.get("sessions")
        if not isinstance(sessions, list):
            rows.append('<p class="unavailable">Sessions unavailable.</p>')
        else:
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                marked = mark_incomplete_history(session)
                rows.append(f'<section><h3>Session {_escape(str(marked.get("role", "unavailable")))}</h3><p>{marked["history_state"]}</p>')
                steps = session.get("steps")
                if not isinstance(steps, list):
                    rows.append('<p class="unavailable">Steps unavailable.</p>')
                else:
                    for step in steps:
                        if not isinstance(step, dict):
                            continue
                        rows.append(format_tool_step(step) if step.get("kind") == "tool" else format_model_step(step))
                rows.append('</section>')
        rows.append('</article>')
    return f'<main><h1>Cycle {cycle_id}</h1>{"".join(rows)}</main>'
