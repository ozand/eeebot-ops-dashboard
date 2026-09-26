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
    # Key=value anywhere on a line where key contains KEY, TOKEN, SECRET, PASSWORD, PASS, AUTH
    (re.compile(r'(?i)\b(?P<key>[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[A-Za-z0-9_]*\s*=\s*)(?P<val>[^\s\r\n"\'`]+|"[^"]*"|\'[^\']*\')'), r'\g<key>[redacted]'),
    (re.compile(r'(?i)\b(?P<key>[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS)[A-Za-z0-9_]*\s*:\s*)(?P<val>[^\s\r\n"\'`]+|"[^"]*"|\'[^\']*\')'), r'\g<key>[redacted]'),
    # Basic Auth
    (re.compile(r'(?i)\bAuthorization:\s*Basic\s+[A-Za-z0-9+/=]+'), 'Authorization: Basic [redacted: basic-auth]'),
    (re.compile(r'(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}'), '[redacted: basic-auth]'),
    # user:pass@ in URLs
    (re.compile(r'(?i)([a-z0-9+.-]+://[^/:\s]+):[^/@\s]+(@)'), r'\g<1>:[redacted]\g<2>'),
    # JSON secret fields
    (re.compile(r'(?i)("(?:\w*_)?(?:password|token|api_key|secret)"\s*:\s*)"(?:[^"\\]|\\.)*"'), r'\g<1>"[redacted]"'),
    # Specific API key tokens
    (re.compile(r'\bsk-[A-Za-z0-9_-]{8,}\b'), '[redacted: api-key]'),
    (re.compile(r'(?i)\b(?:ghp_|gho_|ghs_|ghu_|github_pat_)[A-Za-z0-9_]+'), '[redacted: token]'),
    (re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+'), 'Bearer [redacted: bearer]'),
    (re.compile(r'\bAKIA[A-Z0-9]{16}\b'), '[redacted: aws-key]'),
    (re.compile(r'(?i)\bxox[baprs]-[A-Za-z0-9-]+'), '[redacted: slack-token]'),
    (re.compile(r'-----BEGIN (?:[A-Z0-9_-]+ )*PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9_-]+ )*PRIVATE KEY-----'), '[redacted: private-key]'),
)

def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def redact_text(value: str) -> str:
    result = value
    for pattern, repl in SECRET_PATTERNS:
        result = pattern.sub(repl, result)
    return result


def is_env_path(path_str: str) -> bool:
    return (
        "/etc/eeepc-agent" in path_str
        or bool(re.search(r'(?:^|[/\\ \t\'"])(?:[\w.-]*\.env|\.env(?:\.[\w.-]+)?)(?:$|[/\\ \t\'"])', path_str, re.IGNORECASE))
        or ".env" in path_str
    )


def sanitize_tool_arguments(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return "[env file contents withheld]" if is_env_path(arguments) else redact_text(arguments)
    if isinstance(parsed, dict) and any(
        key in {"path", "file", "filename"} and isinstance(value, str) and is_env_path(value)
        for key, value in parsed.items()
    ):
        return "[env file contents withheld]"
    return json.dumps(_sanitize_nested_value(parsed), ensure_ascii=False)


def _sanitize_nested_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"(?i)(password|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)", key_text):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_nested_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_nested_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


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


def sanitize_messages(raw_messages: Any) -> list[dict[str, Any]]:
    if isinstance(raw_messages, str):
        try:
            raw_messages = json.loads(raw_messages)
        except Exception:
            return []
    if not isinstance(raw_messages, list):
        return []
    cleaned: list[dict[str, Any]] = []
    pending_tool_args: dict[str, str] = {}
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        m = dict(msg)
        role = m.get("role")
        if role == "assistant":
            calls = []
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    calls.append(tc)
                    continue
                call = dict(tc)
                fn = dict(tc.get("function") or tc)
                args = str(fn.get("arguments") or "")
                safe_args = sanitize_tool_arguments(args)
                fn["arguments"] = safe_args
                if "function" in tc:
                    call["function"] = fn
                else:
                    call.update(fn)
                calls.append(call)
                cid = tc.get("id")
                if cid:
                    pending_tool_args[str(cid)] = args
                elif is_env_path(args):
                    pending_tool_args["__latest_idless_env_call__"] = args
            if "tool_calls" in m:
                m["tool_calls"] = calls
        elif role == "tool":
            cid = m.get("tool_call_id")
            content = str(m.get("content") or "")
            args = pending_tool_args.pop(str(cid), "") if cid else pending_tool_args.pop("__latest_idless_env_call__", "")
            if is_env_path(args) or is_env_path(content):
                m["content"] = "[env file contents withheld]"
            else:
                m["content"] = redact_text(content)
        elif "content" in m and isinstance(m["content"], str):
            m["content"] = redact_text(m["content"])
        cleaned.append(m)
    return cleaned

class ReadResult(tuple):
    broken: bool

    def __new__(cls, rows: list[dict[str, Any]], ok: bool, broken: bool = False):
        instance = super().__new__(cls, (rows, ok))
        instance.broken = broken
        return instance


def _read_jsonl(paths: list[Path]) -> ReadResult:
    rows: list[dict[str, Any]] = []
    broken = False
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
                        broken = True
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            return ReadResult(rows, False, broken=broken)
    return ReadResult(rows, True, broken=broken)

def _strip_tool_ids(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = []
    for message in messages:
        item = dict(message)
        if item.get("role") == "assistant" and isinstance(item.get("tool_calls"), list):
            item["tool_calls"] = [
                {**call, "id": "[tool call]"} if isinstance(call, dict) else call
                for call in item["tool_calls"]
            ]
        if item.get("role") == "tool":
            item.pop("tool_call_id", None)
        cleaned.append(item)
    return cleaned


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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
                        "tool_call_id": str(cid),
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
    response_ids = {str(tc.get("id")) for tc in response_tools if isinstance(tc, dict) and tc.get("id")}
    observed_ids = set(pending_calls)
    if response_ids:
        for call_id in response_ids - observed_ids:
            response = next(tc for tc in response_tools if isinstance(tc, dict) and str(tc.get("id")) == call_id)
            fn = response.get("function") or response
            args = fn.get("arguments") or ""
            steps.append({"kind": "tool", "tool_call_id": call_id,
                          "name": str(fn.get("name") or "tool"), "arguments": str(args),
                          "result": "[response tool call not reconciled to request history]",
                          "source": source, "status": "incomplete", "duration": None, "tokens": None})
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
                "tool_call_id": str(tc.get("id") or ""),
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

    runs_result = _read_jsonl(run_paths)
    prompts_result = _read_jsonl(prompt_paths)
    durations_result = _read_jsonl(duration_paths)
    raw_runs, runs_ok = runs_result
    raw_prompts, prompts_ok = prompts_result
    durations, durations_ok = durations_result
    broken_runs = getattr(runs_result, "broken", False)
    broken_prompts = getattr(prompts_result, "broken", False)
    broken_dur = getattr(durations_result, "broken", False)

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
    broken_comp = False
    if c_path.is_file():
        compaction_result = _read_jsonl([c_path])
        c_rows, compactions_ok = compaction_result
        broken_comp = getattr(compaction_result, "broken", False)
        compactions = c_rows

    all_reads_ok = runs_ok and prompts_ok and durations_ok and compactions_ok
    dur_by_seq = {
        (str(row.get("cycle_id")), str(row.get("component")), str(row.get("seq"))): row
        for row in durations if row.get("seq") is not None
    }
    read_state = "ok" if all_reads_ok else "unavailable"

    all_cycle_ids = {str(r.get("cycle_id")) for r in runs if r.get("cycle_id")}
    all_cycle_ids.update(str(p.get("cycle_id")) for p in raw_prompts if p.get("cycle_id"))

    index: dict[str, dict[str, Any]] = {}
    for cid in all_cycle_ids:
        c_runs = [r for r in runs if str(r.get("cycle_id")) == cid]
        c_prompts = [p for p in raw_prompts if str(p.get("cycle_id")) == cid]
        c_compactions = [c for c in compactions if str(c.get("cycle_id")) == cid]
        has_compaction = any(c.get("reason") == "compacted" or "compact" in str(c.get("reason", "")) for c in c_compactions)
        has_truncation = any(bool(p.get("truncated")) for p in c_prompts)
        capture_state = "truncated" if has_truncation else "complete"

        reconstruction_state = "complete"
        cycle_reconstruction_incomplete = False
        if read_state != "ok":
            reconstruction_state = "unknown"
        elif not c_prompts and c_runs:
            reconstruction_state = "incomplete"
        elif not all_reads_ok:
            reconstruction_state = "unknown"
        elif broken_runs or broken_prompts or broken_dur or broken_comp or has_compaction:
            reconstruction_state = "incomplete"

        sessions_by_role: dict[str, list[dict[str, Any]]] = {}
        steps_by_prompt: dict[int, list[dict[str, Any]]] = {}
        for p in c_prompts:
            role = str(p.get("component") or "executor")
            duration_row = dur_by_seq.get((cid, role, str(p.get("seq"))))
            dur = duration_row.get("duration_ms") if duration_row is not None else None
            tools = extract_tool_steps(p)
            if any(t.get("status") in {"incomplete", "pending"} for t in tools):
                reconstruction_state = "incomplete"
                cycle_reconstruction_incomplete = True
            if (p.get("finish_reason") == "tool_calls" and p is c_prompts[-1]) or p.get("tool_calls"):
                reconstruction_state = "incomplete"
                cycle_reconstruction_incomplete = True
            sanitized_msgs = sanitize_messages(p.get("messages"))
            model_step = {
                "kind": "model",
                "messages": json.dumps(_strip_tool_ids(sanitized_msgs), ensure_ascii=False) if sanitized_msgs else None,
                "answer": redact_text(str(p.get("content"))) if p.get("content") is not None else None,
                "tools": json.dumps(p.get("tool_calls"), ensure_ascii=False) if p.get("tool_calls") else None,
                "reasoning": redact_text(str(p.get("reasoning_content"))) if p.get("reasoning_content") is not None else None,
                "tokens": (p.get("prompt_tokens") or 0) + (p.get("completion_tokens") or 0),
                "duration": dur if dur is not None else "unknown",
            }
            sessions_by_role.setdefault(role, []).append(model_step)
            sessions_by_role[role].extend(tools)
            steps_by_prompt[id(p)] = [model_step, *tools]

        cycle_reconstruction_incomplete = cycle_reconstruction_incomplete or any(
            run.get("classification") in {"unit_timeout", "killed"} for run in c_runs
        )
        attempts = []
        assigned_prompts: set[int] = set()
        seen_tool_ids_cycle: set[str] = set()
        for r in c_runs:
            rid = str(r.get("run_id") or "unavailable")
            killed = r.get("classification") in {"unit_timeout", "killed"}
            started = _parse_timestamp(r.get("started_at") or r.get("start_time"))
            finished = _parse_timestamp(r.get("finished_at") or r.get("end_time"))
            attributed = [p for p in c_prompts if started is not None and finished is not None
                          and (stamp := _parse_timestamp(p.get("ts") or p.get("timestamp"))) is not None
                          and started <= stamp <= finished]
            assigned_prompts.update(id(p) for p in attributed)
            role_steps: dict[str, list[dict[str, Any]]] = {}
            for p in attributed:
                role = str(p.get("component") or "unassigned")
                role_steps.setdefault(role, []).extend(steps_by_prompt.get(id(p), [])[:1])
                for tool in steps_by_prompt.get(id(p), [])[1:]:
                    tool_id = str(tool.get("tool_call_id") or "")
                    if not tool_id or tool_id not in seen_tool_ids_cycle:
                        role_steps[role].append(tool)
                        if tool_id:
                            seen_tool_ids_cycle.add(tool_id)
            attempt_prompts = [prompt for prompt in c_prompts if started is not None and finished is not None
                               and (stamp := _parse_timestamp(prompt.get("ts") or prompt.get("timestamp"))) is not None
                               and started <= stamp <= finished]
            if len(c_runs) == 1 and not attempt_prompts:
                attempt_prompts = list(c_prompts)
            attempt_complete = (
                not killed
                and read_state == "ok"
                and not any(compaction.get("reason") == "compacted" or "compact" in str(compaction.get("reason", "")) for compaction in c_compactions)
                and bool(attempt_prompts)
                and all(
                    not prompt.get("truncated")
                    and not (prompt.get("finish_reason") == "tool_calls" and prompt is c_prompts[-1])
                    and not any(step.get("status") in {"incomplete", "pending"} for step in extract_tool_steps(prompt))
                    for prompt in attempt_prompts
                )
            )
            att_sessions = [{"role": role, "history_complete": attempt_complete,
                             "model_calls": sum(step.get("kind") == "model" for step in steps), "steps": steps}
                            for role, steps in sorted(role_steps.items())]
            if killed:
                reconstruction_state = "incomplete"
            attempts.append({"run_id": rid, "classification": r.get("classification") or "unknown",
                             "model_call_count": len(attributed), "sessions": att_sessions,
                             "history_complete": attempt_complete})
        unassigned = [p for p in c_prompts if id(p) not in assigned_prompts]
        if unassigned:
            unassigned_steps = []
            for p in unassigned:
                unassigned_steps.extend(steps_by_prompt.get(id(p), []))
            attempts.append({"run_id": "unassigned", "classification": "unknown", "model_call_count": len(unassigned),
                             "sessions": [{"role": "unassigned", "history_complete": False,
                                          "model_calls": sum(step.get("kind") == "model" for step in unassigned_steps),
                                          "steps": unassigned_steps}],
                             "history_complete": False})

        history_complete = (
            read_state == "ok"
            and capture_state == "complete"
            and reconstruction_state == "complete"
            and not any(a["classification"] in {"unit_timeout", "killed"} for a in attempts)
            and bool(attempts)
            and bool(c_prompts)
        )

        sessions = [
            {"role": role, "history_complete": history_complete,
             "model_calls": sum(step.get("kind") == "model" for step in steps), "steps": steps}
            for role, steps in sorted(sessions_by_role.items())
        ]
        index[cid] = {
            "cycle_id": cid,
            "available": True,
            "attempts": attempts,
            "sessions": sessions,
            "total_model_calls": len(c_prompts),
            "read": read_state,
            "capture": capture_state,
            "reconstruction": reconstruction_state,
            "history_complete": history_complete,
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
    raw_args = str(step.get("arguments", "unavailable"))
    args = "[env file contents withheld]" if is_env_path(raw_args) else display_text(raw_args)
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
        rows.append(f'<article class="attempt-row"><h3>Attempt {_escape(str(att.get("run_id", "unavailable")))}</h3>{calls_text}<p>Classification: {_escape(str(att.get("classification", "unknown")))}</p><p>{att_marked["history_state"]}</p>')
        for sess in att.get("sessions") or []:
            rows.append(f'<section class="attempt-session"><h4>Session {_escape(str(sess.get("role", "unassigned")))}</h4><p>Model calls: {sess.get("model_calls", 0)}</p>')
            for step in sess.get("steps") or []:
                rows.append(format_tool_step(step) if step.get("kind") == "tool" else format_model_step(step))
            rows.append('</section>')
        rows.append('</article>')
    rows.append('</section>')

    if not attempts:
        rows.append('<section class="sessions-section"><h2>Sessions</h2>')
    if not attempts and not sessions:
        rows.append('<p class="unavailable">No session records found.</p>')
    for sess in sessions if not attempts else []:
        role = sess.get("role", "unknown")
        sess_marked = mark_incomplete_history(sess)
        rows.append(f'<article class="session-block"><h3>Session {_escape(role)}</h3><p>Model calls: {sess.get("model_calls", 0)}</p><p>{sess_marked["history_state"]}</p>')
        for step in sess.get("steps") or []:
            if step.get("kind") == "tool":
                rows.append(format_tool_step(step))
            else:
                rows.append(format_model_step(step))
        rows.append('</article>')
    if not attempts:
        rows.append('</section>')
    rows.append('</main>')
    return "".join(rows)
