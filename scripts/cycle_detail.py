"""Private cycle-detail formatting primitives and loaders (ADR-036 D2)."""
from __future__ import annotations

import gzip
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.publish_scan import STANDALONE_PATTERNS

DEFAULT_DISPLAY_LIMIT = 4000

REDACTION_REPLACEMENTS = {
    "eeepc_agent_path": "[internal path]",
    "openai_secret_key": "[redacted: api-key]",
    "github_token": "[redacted: token]",
    "bearer_token": "Bearer [redacted: bearer]",
    "aws_access_key": "[redacted: aws-key]",
    "slack_token": "[redacted: slack-token]",
    "basic_auth": "[redacted: basic-auth]",
    "url_credentials": "[redacted: url-credentials]",
    "private_key_header": "[redacted: private-key]",
    "structural_reasoning_content": "[redacted field]",
    "structural_messages": "[redacted field]",
    "structural_prompt": "[redacted field]",
    "json_secret_field": "[redacted]",
    "env_secret_kv": "[redacted]",
    "openai_secret_key_short": "[redacted: api-key]",
}

class SanitizedText(str):
    """Text that has passed the private-detail sanitizer."""

def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def _replace_secret_fields(value: str) -> str:
    value = re.sub(r'(?i)(["\']?[\w-]*(?:password|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)[\w-]*["\']?\s*:\s*)(["\'])(.*?)(\2)', lambda match: f"{match.group(1)}{match.group(2)}[redacted]{match.group(2)}", value)
    value = re.sub(r'(?i)([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[A-Za-z0-9_]*\s*:\s*)([^\s\r\n]+)', lambda match: f"{match.group(1)}[redacted]", value)
    return re.sub(r'(?i)([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[A-Za-z0-9_]*\s*=\s*)([^\s\r\n]+)', lambda match: f"{match.group(1)}[redacted]", value)


def redact_text(value: str) -> SanitizedText:
    result = value
    for rule in STANDALONE_PATTERNS:
        replacement = REDACTION_REPLACEMENTS.get(rule.name)
        if replacement:
            result = rule.pattern.sub(replacement, result)
    return SanitizedText(_replace_secret_fields(result))


def is_env_path(path_str: str) -> bool:
    return (
        "/etc/eeepc-agent" in path_str
        or bool(re.search(r'(?:^|[/\\ \t\'"])(?:[\w.-]*\.env|\.env(?:\.[\w.-]+)?)(?:$|[/\\ \t\'"])', path_str, re.IGNORECASE))
        or ".env" in path_str
    )


def sanitize_tool_arguments(arguments: str) -> SanitizedText:
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return SanitizedText("[env file contents withheld]" if is_env_path(arguments) else redact_text(arguments))
    if _contains_env_path(parsed):
        return SanitizedText("[env file contents withheld]")
    return SanitizedText(json.dumps(_sanitize_nested_value(parsed), ensure_ascii=False))


def _contains_env_path(value: Any) -> bool:
    if isinstance(value, dict):
        return any((str(key).lower() in {"path", "file", "filename"} and isinstance(item, str) and is_env_path(item)) or _contains_env_path(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_env_path(item) for item in value)
    return False


def _sanitize_nested_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"(?i)(password|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)", key_text):
                sanitized[key] = SanitizedText("[redacted]")
            else:
                sanitized[key] = _sanitize_nested_value(item)
        return sanitized
    if isinstance(value, list):
        return [item if isinstance(item, SanitizedText) else _sanitize_nested_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def sanitize_tool_output(args: str, result: str) -> SanitizedText:
    if is_env_path(args) or is_env_path(result):
        return SanitizedText("[env file contents withheld]")
    return redact_text(result)


def display_text(value: str, *, limit: int = DEFAULT_DISPLAY_LIMIT) -> SanitizedText:
    """Redact and truncate only the display copy, with explicit omitted length."""
    safe = redact_text(value)
    if len(safe) <= limit:
        return SanitizedText(safe)
    return SanitizedText(f"{safe[:limit]}… {len(safe) - limit} characters not shown")


def sanitize_messages(raw_messages: Any) -> list[dict[str, Any]]:
    if isinstance(raw_messages, str):
        try:
            raw_messages = json.loads(raw_messages)
        except (json.JSONDecodeError, TypeError):
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
            raw_content = m.get("content")
            content = raw_content if isinstance(raw_content, str) else json.dumps(raw_content, ensure_ascii=False)
            args = pending_tool_args.pop(str(cid), "") if cid else pending_tool_args.pop("__latest_idless_env_call__", "")
            if is_env_path(args) or is_env_path(content):
                m["content"] = SanitizedText("[env file contents withheld]")
            elif isinstance(raw_content, str):
                m["content"] = redact_text(content)
            else:
                m["content"] = _sanitize_nested_value(raw_content)
        elif "content" in m:
            m["content"] = _sanitize_nested_value(m["content"])
        if role == "assistant" and isinstance(m.get("content"), str):
            m["content"] = redact_text(m["content"])
        if "content" in m and not isinstance(m["content"], str):
            m["content"] = _sanitize_nested_value(m["content"])
        if isinstance(m.get("content"), list):
            m["content"] = _sanitize_nested_value(m["content"])
        if isinstance(m.get("function_call"), dict):
            fn_call = dict(m["function_call"])
            args = fn_call.get("arguments")
            if isinstance(args, (dict, list)):
                args = json.dumps(args, ensure_ascii=False)
            if isinstance(args, str):
                fn_call["arguments"] = sanitize_tool_arguments(args)
            m["function_call"] = fn_call
        if isinstance(m.get("tool_calls"), list):
            safe_calls = []
            for item in m["tool_calls"]:
                if not isinstance(item, dict):
                    safe_calls.append(item)
                    continue
                call = dict(item)
                fn = dict(item.get("function") or item)
                args = fn.get("arguments")
                if isinstance(args, (dict, list)):
                    args = json.dumps(args, ensure_ascii=False)
                if isinstance(args, str):
                    fn["arguments"] = sanitize_tool_arguments(args)
                if "function" in item:
                    call["function"] = fn
                else:
                    call.update(fn)
                safe_calls.append(call)
            m["tool_calls"] = safe_calls
        for key, item in list(m.items()):
            if key != "content" and isinstance(item, (dict, list)):
                m[key] = _sanitize_nested_value(item)
        cleaned.append(m)
    return cleaned

class ReadResult(tuple):
    broken: set[str]

    def __new__(cls, rows: list[dict[str, Any]], ok: bool, broken: set[str] | None = None):
        instance = super().__new__(cls, (rows, ok))
        instance.broken = broken or set()
        return instance


def _read_jsonl(paths: list[Path]) -> ReadResult:
    rows: list[dict[str, Any]] = []
    broken: set[str] = set()
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
                        cycle_match = re.search(r'"cycle_id"\s*:\s*"([^"]+)"', line)
                        broken.add(cycle_match.group(1) if cycle_match else "*")
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except (OSError, EOFError):
            broken.add("*")
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
        except (json.JSONDecodeError, TypeError):
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
                        "arguments": sanitize_tool_arguments(str(args)),
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
            elif not cid and steps:
                for s in reversed(steps):
                    if s["status"] == "pending":
                        s["result"] = sanitize_tool_output(s["arguments"], content)
                        s["status"] = "ok"
                        break
    response_tools = prompt.get("tool_calls") or []
    if isinstance(response_tools, str):
        try:
            response_tools = json.loads(response_tools)
        except (json.JSONDecodeError, TypeError):
            response_tools = []
    observed_ids = set(pending_calls)
    for tc in response_tools:
        if isinstance(tc, dict):
            if tc.get("id") and str(tc["id"]) in observed_ids:
                continue
            fn = tc.get("function") or tc
            args = fn.get("arguments") or ""
            if isinstance(args, dict):
                args = json.dumps(args, ensure_ascii=False)
            args = sanitize_tool_arguments(str(args))
            steps.append({
                "kind": "tool",
                "tool_call_id": str(tc.get("id") or ""),
                "name": str(fn.get("name") or "tool"),
                "arguments": sanitize_tool_arguments(str(args)),
                "result": SanitizedText("[no next request: final tool call without next prompt]"),
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
    prompt_paths = [
        plain if plain.is_file() else compressed
        for date in dates
        for plain, compressed in ((
            state_root / "llm_calls" / "prompts" / f"{date}.jsonl",
            state_root / "llm_calls" / "prompts" / f"{date}.jsonl.gz",
        ),)
        if plain.is_file() or compressed.is_file()
    ]
    duration_paths = [p for date in dates for p in (state_root / "llm_calls" / f"{date}.jsonl", state_root / "llm_calls" / f"{date}.jsonl.gz") if p.is_file()]

    runs_result = _read_jsonl(run_paths)
    prompts_result = _read_jsonl(prompt_paths)
    durations_result = _read_jsonl(duration_paths)
    raw_runs, runs_ok = runs_result
    raw_prompts, prompts_ok = prompts_result
    durations, durations_ok = durations_result
    broken_runs = runs_result.broken
    broken_prompts = prompts_result.broken
    broken_dur = durations_result.broken

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
    broken_comp: set[str] = set()
    if c_path.is_file():
        compaction_result = _read_jsonl([c_path])
        c_rows, compactions_ok = compaction_result
        broken_comp = compaction_result.broken
        compactions = c_rows

    all_reads_ok = runs_ok and prompts_ok and durations_ok and compactions_ok
    duration_rows: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in durations:
        if row.get("seq") is not None:
            key = (str(row.get("cycle_id")), str(row.get("component")), str(row.get("seq")))
            duration_rows.setdefault(key, []).append(row)
    read_state = "ok" if all_reads_ok else "unavailable"

    all_cycle_ids = {str(r.get("cycle_id")) for r in runs if r.get("cycle_id")}
    all_cycle_ids.update(str(p.get("cycle_id")) for p in raw_prompts if p.get("cycle_id"))

    index: dict[str, dict[str, Any]] = {}
    for cid in all_cycle_ids:
        c_runs = [r for r in runs if str(r.get("cycle_id")) == cid]
        c_prompts = [p for p in raw_prompts if str(p.get("cycle_id")) == cid]
        cycle_broken = any(cid in errors for errors in (broken_runs, broken_prompts, broken_dur, broken_comp))
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
        elif cycle_broken or has_compaction:
            reconstruction_state = "incomplete"

        sessions_by_role: dict[str, list[dict[str, Any]]] = {}
        steps_by_prompt: dict[int, list[dict[str, Any]]] = {}
        used_duration_ids: set[int] = set()
        for p in c_prompts:
            role = str(p.get("component") or "executor")
            duration_key = (cid, role, str(p.get("seq")))
            candidates = duration_rows.get(duration_key, [])
            prompt_ts = _parse_timestamp(p.get("ts") or p.get("timestamp"))
            duration_row = min(
                (row for row in candidates if id(row) not in used_duration_ids),
                key=lambda row: abs((_parse_timestamp(row.get("ts") or row.get("timestamp")) - prompt_ts).total_seconds())
                if prompt_ts is not None and _parse_timestamp(row.get("ts") or row.get("timestamp")) is not None
                else float("inf"),
                default=None,
            )
            if duration_row is not None:
                used_duration_ids.add(id(duration_row))
            dur = duration_row.get("duration_ms") if duration_row is not None else None
            sanitized_prompt = dict(p)
            sanitized_prompt["messages"] = sanitize_messages(p.get("messages"))
            tools = extract_tool_steps(sanitized_prompt)
            if any(t.get("status") in {"incomplete", "pending"} for t in tools):
                reconstruction_state = "incomplete"
                cycle_reconstruction_incomplete = True
            if (p.get("finish_reason") == "tool_calls" and p is c_prompts[-1]) or p.get("tool_calls"):
                reconstruction_state = "incomplete"
                cycle_reconstruction_incomplete = True
            sanitized_msgs = sanitize_messages(p.get("messages"))
            response_tools = _sanitize_nested_value(p.get("tool_calls")) if p.get("tool_calls") else None
            legacy_call = _sanitize_nested_value(p.get("function_call")) if p.get("function_call") else None
            model_step = {
                "kind": "model",
                "messages": SanitizedText(redact_text(json.dumps(_strip_tool_ids(sanitized_msgs), ensure_ascii=False))) if sanitized_msgs else None,
                "answer": redact_text(str(p.get("content"))) if p.get("content") is not None else None,
                "tools": SanitizedText(redact_text(json.dumps(response_tools, ensure_ascii=False))) if response_tools else None,
                "function_call": SanitizedText(redact_text(json.dumps(legacy_call, ensure_ascii=False))) if legacy_call else None,
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
                and not cycle_broken
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
    for label in ("messages", "answer", "tools", "function_call", "reasoning"):
        if step.get(label) is not None:
            value = step[label]
            if not isinstance(value, SanitizedText):
                raise TypeError(f"{label} must be SanitizedText")
            safe_value = value
            parts.append(f"<p><b>{label.title()}:</b> {_escape(str(display_text(safe_value)))}</p>")
    if step.get("tokens") is not None:
        parts.append(f"<p>Tokens: {step['tokens']}</p>")
    if step.get("duration") is not None:
        parts.append(f"<p>Duration: {_escape(display_text(str(step['duration'])))}</p>")
    parts.append("</div>")
    return "".join(parts)


def format_tool_step(step: dict[str, Any]) -> str:
    name = display_text(str(step.get("name", "unavailable")))
    raw_args = step.get("arguments", SanitizedText("unavailable"))
    if not isinstance(raw_args, SanitizedText):
        if "arguments" in step and step.get("kind") == "model":
            raise TypeError("arguments must be SanitizedText")
        raw_args = sanitize_tool_arguments(str(raw_args))
    args = display_text(raw_args)
    res_val = step.get("result")
    if res_val is not None and not isinstance(res_val, SanitizedText):
        if step.get("kind") == "model":
            raise TypeError("result must be SanitizedText")
        res_val = sanitize_tool_output(str(raw_args), str(res_val))
    result = display_text(sanitize_tool_output(str(raw_args), str(res_val))) if res_val is not None else SanitizedText("unavailable")
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
    reflection = data.get("reflection") if isinstance(data.get("reflection"), dict) else None

    rows = [f'<main><h1>Cycle {_escape(cycle_id)}</h1>']
    rows.append(f'<div class="cycle-summary"><p>Total model calls: {total_calls}</p>')
    rows.append(f'<p>{"history incomplete" if data.get("history_complete") is not True else "history complete"}</p></div>')

    if reflection:
        rows.append('<section class="reflection-summary"><h2>Reflector summary</h2>')
        rows.append(f'<p>Summary chars: {_escape(str(reflection.get("summary_chars", "unknown")))}</p>')
        rows.append(f'<p>Findings count: {_escape(str(reflection.get("findings_count", "unknown")))}</p>')
        rows.append(f'<p>Recommendations count: {_escape(str(reflection.get("recommendations_count", "unknown")))}</p></section>')

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
