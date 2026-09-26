"""Private cycle-detail formatting primitives and loaders (ADR-036 D2)."""
from __future__ import annotations

import gzip
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_DISPLAY_LIMIT = 4000

SECRET_PATTERNS = (
    # Line NAME=value where NAME contains KEY, TOKEN, SECRET, PASSWORD, PASS, AUTH
    (re.compile(r'(?im)^(?P<key>[^\s#=]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[^\s#=]*\s*=\s*)(?P<val>[^\r\n]+)'), r'\g<key>[redacted]'),
    # Basic Auth
    (re.compile(r'(?i)\bAuthorization:\s*Basic\s+[A-Za-z0-9+/=]+'), 'Authorization: Basic [redacted: basic-auth]'),
    (re.compile(r'(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}'), '[redacted: basic-auth]'),
    # user:pass@ in URLs
    (re.compile(r'(?i)([a-z0-9+.-]+://[^/:\s]+):[^/@\s]+(@)'), r'\g<1>:[redacted]\g<2>'),
    # JSON secret fields
    (re.compile(r'(?i)("(?:\w*_)?(?:password|token|api_key|secret)"\s*:\s*)"(?:[^"\\]|\\.)*"'), r'\g<1>"[redacted]"'),
    # Specific API key tokens
    (re.compile(r'\bsk-[A-Za-z0-9_-]{8,}\b'), '[redacted: api-key]'),
    (re.compile(r'(?i)\b(?:ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_]+'), '[redacted: token]'),
    (re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+'), 'Bearer [redacted: bearer]'),
    (re.compile(r'\bAKIA[A-Z0-9]{16}\b'), '[redacted: aws-key]'),
    (re.compile(r'(?i)\bxox[baprs]-[A-Za-z0-9-]+'), '[redacted: slack-token]'),
    (re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'), '[redacted: private-key]'),
)

def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def redact_text(value: str) -> str:
    result = value
    for pattern, repl in SECRET_PATTERNS:
        result = pattern.sub(repl, result)
    return result


def is_env_path(path_str: str) -> bool:
    return "/etc/eeepc-agent" in path_str or bool(re.search(r"\b[\w.-]+\.env\b", path_str))


def sanitize_tool_output(args: str, result: str) -> str:
    if is_env_path(args) or is_env_path(result):
        return "[env file contents withheld]"
    return redact_text(result)


def display_text(value: str, *, limit: int = DEFAULT_DISPLAY_LIMIT) -> str:
    """Redact and truncate only the display copy, with explicit omitted length."""
    safe = redact_text(value)
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
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            return rows, False
    return rows, True

def extract_tool_steps(prompt: dict[str, Any]) -> list[dict[str, Any]]:
    seq = prompt.get("seq", 1)
    source = f"reconstructed from request seq {seq}"
    messages = prompt.get("messages") or []
    if isinstance(messages, str):
        try:
            messages = json.loads(messages)
        except Exception:
            messages = []
    steps: list[dict[str, Any]] = []
    pending_calls: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    cid = tc.get("id") or f"call_{len(steps)}"
                    fn = tc.get("function") or tc
                    args = fn.get("arguments") or ""
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    step = {
                        "kind": "tool",
                        "name": str(fn.get("name") or "tool"),
                        "arguments": str(args),
                        "result": None,
                        "source": source,
                        "status": "pending",
                        "duration": None,
                        "tokens": None,
                    }
                    pending_calls[cid] = step
                    steps.append(step)
        elif role == "tool":
            cid = msg.get("tool_call_id")
            content = str(msg.get("content") or "")
            if cid and cid in pending_calls:
                call_step = pending_calls.pop(cid)
                call_step["result"] = sanitize_tool_output(call_step["arguments"], content)
                call_step["status"] = "ok"
            elif steps:
                for s in reversed(steps):
                    if s["status"] == "pending":
                        s["result"] = sanitize_tool_output(s["arguments"], content)
                        s["status"] = "ok"
                        break
    response_tools = prompt.get("tool_calls") or []
    if isinstance(response_tools, str):
        try:
            response_tools = json.loads(response_tools)
        except Exception:
            response_tools = []
    for tc in response_tools:
        if isinstance(tc, dict):
            fn = tc.get("function") or tc
            args = fn.get("arguments") or ""
            if isinstance(args, dict):
                args = json.dumps(args)
            steps.append({
                "kind": "tool",
                "name": str(fn.get("name") or "tool"),
                "arguments": str(args),
                "result": "[no next request: final tool call without next prompt]",
                "source": source,
                "status": "incomplete",
                "duration": None,
                "tokens": None,
            })
    return steps

def build_cycle_index(state_root: Path, *, days: int = 7, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    reference = now or datetime.now(timezone.utc)
    dates = [(reference.date() - timedelta(days=offset)).isoformat() for offset in range(days)]
    run_paths = []
    active_runs = state_root / "bridge" / "runs.jsonl"
    if active_runs.is_file():
        run_paths.append(active_runs)
    for date in dates:
        for p in (state_root / "bridge" / f"runs-{date}.jsonl", state_root / "bridge" / f"runs-{date}.jsonl.gz"):
            if p.is_file():
                run_paths.append(p)
    prompt_paths = [p for date in dates for p in (state_root / "llm_calls" / "prompts" / f"{date}.jsonl", state_root / "llm_calls" / "prompts" / f"{date}.jsonl.gz") if p.is_file()]
    duration_paths = [p for date in dates for p in (state_root / "llm_calls" / f"{date}.jsonl", state_root / "llm_calls" / f"{date}.jsonl.gz") if p.is_file()]

    raw_runs, runs_ok = _read_jsonl(run_paths)
    raw_prompts, prompts_ok = _read_jsonl(prompt_paths)
    durations, durations_ok = _read_jsonl(duration_paths)

    seen_run_ids = set()
    runs = []
    for r in raw_runs:
        rid = r.get("run_id")
        if rid:
            if rid in seen_run_ids:
                continue
            seen_run_ids.add(rid)
        runs.append(r)

    compactions = []
    compactions_ok = True
    c_path = state_root / "compaction" / "journal.jsonl"
    if c_path.is_file():
        c_rows, compactions_ok = _read_jsonl([c_path])
        compactions = c_rows

    all_reads_ok = runs_ok and prompts_ok and durations_ok and compactions_ok

    dur_by_seq = {(str(r.get("cycle_id")), str(r.get("component")), str(r.get("seq"))): r for r in durations}
    all_cycle_ids = {str(r.get("cycle_id")) for r in runs if r.get("cycle_id")}
    all_cycle_ids.update(str(p.get("cycle_id")) for p in raw_prompts if p.get("cycle_id"))

    index: dict[str, dict[str, Any]] = {}
    for cid in all_cycle_ids:
        c_runs = [r for r in runs if str(r.get("cycle_id")) == cid]
        c_prompts = [p for p in raw_prompts if str(p.get("cycle_id")) == cid]
        c_compactions = [c for c in compactions if str(c.get("cycle_id")) == cid]
        has_compaction = any(c.get("reason") == "compacted" or "compact" in str(c.get("reason", "")) for c in c_compactions)

        sessions_by_role: dict[str, list[dict[str, Any]]] = {}
        history_complete = not has_compaction and all_reads_ok
        for p in c_prompts:
            role = str(p.get("component") or "executor")
            seq = p.get("seq", 1)
            dur = dur_by_seq.get((cid, role, str(seq)), {}).get("duration_ms")
            tools = extract_tool_steps(p)
            if any(t.get("status") in {"incomplete", "pending"} for t in tools):
                history_complete = False
            model_step = {
                "kind": "model",
                "messages": json.dumps(p.get("messages"), ensure_ascii=False) if p.get("messages") else None,
                "answer": p.get("content"),
                "tools": json.dumps(p.get("tool_calls"), ensure_ascii=False) if p.get("tool_calls") else None,
                "reasoning": p.get("reasoning_content"),
                "tokens": (p.get("prompt_tokens") or 0) + (p.get("completion_tokens") or 0),
                "duration": dur,
            }
            sessions_by_role.setdefault(role, []).append(model_step)
            sessions_by_role[role].extend(tools)

        attempts = []
        for r in c_runs:
            killed = r.get("classification") in {"unit_timeout", "killed"}
            if killed:
                history_complete = False
            attempts.append({
                "run_id": r.get("run_id") or "unavailable",
                "classification": r.get("classification") or "unknown",
                "history_complete": not killed and history_complete,
            })

        sessions = []
        for role, steps in sorted(sessions_by_role.items()):
            sessions.append({
                "role": role,
                "history_complete": history_complete,
                "model_calls": sum(1 for s in steps if s.get("kind") == "model"),
                "steps": steps,
            })

        index[cid] = {
            "cycle_id": cid,
            "available": True,
            "attempts": attempts,
            "sessions": sessions,
            "total_model_calls": len(c_prompts),
            "history_complete": history_complete and bool(attempts),
            "multiple_attempts": len(attempts) > 1,
        }
    return index


def load_cycle_detail(state_root: Path, cycle_id: str, *, days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    idx = build_cycle_index(state_root, days=days, now=now)
    if cycle_id in idx:
        return idx[cycle_id]
    return {"available": False, "cycle_id": cycle_id}

def mark_incomplete_history(record: dict[str, Any]) -> dict[str, Any]:
    res = dict(record)
    if res.get("history_complete") is not True:
        res["history_state"] = "history incomplete"
    else:
        res["history_state"] = "history complete"
    return res


def format_model_step(step: dict[str, Any]) -> str:
    parts = ["<div class=\"step-model\"><h4>Model step</h4>"]
    for label in ("messages", "answer", "tools", "reasoning"):
        if step.get(label) is not None:
            parts.append(f"<p><b>{label.title()}:</b> {_escape(display_text(str(step[label])))}</p>")
    if step.get("tokens") is not None:
        parts.append(f"<p>Tokens: {step['tokens']}</p>")
    if step.get("duration") is not None:
        parts.append(f"<p>Duration: {_escape(display_text(str(step['duration'])))}</p>")
    parts.append("</div>")
    return "".join(parts)


def format_tool_step(step: dict[str, Any]) -> str:
    name = display_text(str(step.get("name", "unavailable")))
    args = display_text(str(step.get("arguments", "unavailable")))
    res_val = step.get("result")
    result = display_text(sanitize_tool_output(args, str(res_val))) if res_val is not None else "unavailable"
    dur_val = step.get("duration")
    duration = display_text(str(dur_val)) if dur_val is not None else "unknown"
    source = display_text(str(step.get("source", "reconstructed from request")))
    return f"<div class=\"step-tool\"><p>Tool step: {_escape(name)}({_escape(args)}) → {_escape(result)}; status: {_escape(display_text(str(step.get('status', 'unavailable'))))}; duration: {_escape(duration)}; source: {_escape(source)}</p></div>"


def render_cycle_page(cycle_id: str, data: dict[str, Any] | None) -> str:
    """Render private cycle detail page (ADR-036 D2); missing sources are explicit."""
    if not isinstance(data, dict) or data.get("available") is False:
        return f'<main><h1>Cycle {_escape(cycle_id)}</h1><p class="unavailable">Cycle detail unavailable: source data unavailable.</p></main>'

    attempts = data.get("attempts") or []
    sessions = data.get("sessions") or []
    total_calls = data.get("total_model_calls", 0)

    rows = [f'<main><h1>Cycle {_escape(cycle_id)}</h1>']
    rows.append(f'<div class="cycle-summary"><p>Total model calls: {total_calls}</p>')
    rows.append(f'<p>{"history incomplete" if data.get("history_complete") is not True else "history complete"}</p></div>')

    rows.append('<section class="attempts-section"><h2>Attempts</h2>')
    if not attempts:
        rows.append('<p class="unavailable">No attempt records found.</p>')
    for att in attempts:
        att_marked = mark_incomplete_history(att)
        calls_text = f"<p>Model calls: {att['model_call_count']}</p>" if att.get("model_call_count") is not None else ""
        rows.append(f'<article class="attempt-row"><h3>Attempt {_escape(str(att.get("run_id", "unavailable")))}</h3>{calls_text}<p>Classification: {_escape(str(att.get("classification", "unknown")))}</p><p>{att_marked["history_state"]}</p></article>')
    rows.append('</section>')

    rows.append('<section class="sessions-section"><h2>Sessions</h2>')
    if not sessions:
        rows.append('<p class="unavailable">No session records found.</p>')
    for sess in sessions:
        role = sess.get("role", "unknown")
        sess_marked = mark_incomplete_history(sess)
        rows.append(f'<article class="session-block"><h3>Session {_escape(role)}</h3><p>Model calls: {sess.get("model_calls", 0)}</p><p>{sess_marked["history_state"]}</p>')
        for step in sess.get("steps") or []:
            if step.get("kind") == "tool":
                rows.append(format_tool_step(step))
            else:
                rows.append(format_model_step(step))
        rows.append('</article>')
    rows.append('</section></main>')
    return "".join(rows)
