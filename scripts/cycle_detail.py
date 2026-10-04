"""Private cycle-detail formatting primitives and loaders (ADR-036 D2)."""
from __future__ import annotations

import gzip
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.publish_scan import scan_text

DEFAULT_DISPLAY_LIMIT = 4000
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CLASSIFICATIONS = frozenset({
    "completion", "completed", "failed", "failure", "unit_timeout", "killed",
    "loop_breaker_abort", "wall_clock_abort", "progress_watchdog_abort", "timeout",
    "timed_out", "crash", "error", "interrupted", "clean", "signal", "paused-supplier",
    "paused_supplier", "supplier_failure", "other", "unknown",
})
OUTCOMES = frozenset({"success", "failure", "failed", "interrupted", "clean", "unknown"})
SESSION_ROLES = frozenset({"planner", "executor", "unassigned", "unknown"})
TOOL_STATUSES = frozenset({"ok", "incomplete", "pending", "unknown", "result recorded"})


class SanitizedText(str):
    """Display-safe text created only from typed private-cycle projections."""


WITHHELD_SECRET = SanitizedText("[withheld: secret pattern]")
WITHHELD_ENV = SanitizedText("[withheld: env file]")


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def _safe_identifier(value: Any, fallback: str = "unavailable") -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        return fallback
    digest = __import__("hashlib").sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"id-{digest}"


def _safe_tool_name(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        return "unavailable"
    return value if value in {"read_file", "write_file", "shell", "search", "unknown"} else "tool"


def _safe_tool_sequence(value: Any) -> str:
    if not isinstance(value, list):
        return "unknown"
    safe_names = [name for name in (_safe_tool_name(item) for item in value[:100]) if name != "tool"]
    return ", ".join(safe_names) if safe_names else "unknown"


def _safe_enum(value: Any, allowed: frozenset[str], fallback: str = "unknown") -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def _safe_state_text(value: Any) -> str:
    if value == "history complete":
        return "history complete"
    return "history incomplete"


def _safe_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
        return None
    return value


def _safe_duration(value: Any) -> str:
    if isinstance(value, bool):
        return "unknown"
    if isinstance(value, int) and 0 <= value <= 86_400_000:
        return str(value)
    if isinstance(value, float) and 0 <= value <= 86_400_000:
        return f"{value:g}"
    return "unknown"


def _serialized_block(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def sanitize_block(value: Any, *, env_path: bool = False) -> SanitizedText:
    serialized = _serialized_block(value)
    if env_path or is_env_path(serialized):
        return WITHHELD_ENV
    if scan_text(serialized):
        return WITHHELD_SECRET
    return SanitizedText(serialized)


def _replace_secret_fields(value: str) -> str:
    value = re.sub(
        r'(?i)(["\']?[\w-]*(?:password|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)[\w-]*["\']?\s*:\s*)(["\'])(.*?)(\2)',
        lambda match: f"{match.group(1)}{match.group(2)}[redacted]{match.group(2)}",
        value,
    )
    value = re.sub(
        r'(?i)([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[A-Za-z0-9_]*\s*:\s*)([^\s\r\n]+)',
        lambda match: f"{match.group(1)}[redacted]",
        value,
    )
    return re.sub(
        r'(?i)([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)[A-Za-z0-9_]*\s*=\s*)([^\s\r\n]+)',
        lambda match: f"{match.group(1)}[redacted]",
        value,
    )


def _content_fingerprint(value: Any) -> tuple[int, str]:
    if isinstance(value, str):
        payload = value.encode("utf-8", errors="replace")
    else:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8", errors="replace")
    return len(payload), __import__("hashlib").sha256(payload).hexdigest()[:12]


def _project_value(value: Any, *, limit: int = DEFAULT_DISPLAY_LIMIT) -> SanitizedText:
    size, digest = _content_fingerprint(value)
    return SanitizedText(f"[withheld: {size} bytes, sha256:{digest}]")


def redact_text(value: str) -> SanitizedText:
    """Compatibility name; raw content is never redacted-and-rendered, only withheld."""
    return _project_value(value)


def is_env_path(path_str: str) -> bool:
    return (
        "/etc/eeepc-agent" in path_str
        or bool(re.search(r'(?:^|[/\\ \t\'"])(?:[\w.-]*\.env|\.env(?:\.[\w.-]+)?)(?:$|[/\\ \t\'"])', path_str, re.IGNORECASE))
        or ".env" in path_str
    )


def _argument_keys(value: Any, *, limit: int = 50, depth: int = 4) -> list[str]:
    """Project argument shape without disclosing arbitrary mapping keys."""
    if depth <= 0 or not isinstance(value, dict):
        return []
    common = {"command", "content", "cwd", "file", "filename", "flags", "mode", "path", "query", "url"}
    names = []
    for key, item in list(value.items())[:limit]:
        name = str(key)
        if name.lower() in common:
            label = name.lower()
        else:
            digest = __import__("hashlib").sha256(name.encode("utf-8", errors="replace")).hexdigest()[:10]
            label = f"key-{digest}"
        nested = _argument_keys(item, limit=limit, depth=depth - 1)
        names.append(f"{label}{{{','.join(nested)}}}" if nested else label)
    if len(value) > limit:
        names.append("additional-keys-withheld")
    return sorted(names)


def sanitize_tool_arguments(arguments: Any) -> SanitizedText:
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (json.JSONDecodeError, TypeError):
        return _project_value(arguments)
    size, digest = _content_fingerprint(parsed)
    keys = _argument_keys(parsed)
    return SanitizedText(json.dumps({"keys": keys, "size": size, "sha256": digest}, sort_keys=True))


def _contains_env_path(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (str(key).lower() in {"path", "file", "filename"} and isinstance(item, str) and is_env_path(item))
            or _contains_env_path(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_env_path(item) for item in value)
    return False


def _sanitize_nested_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: SanitizedText("[redacted]")
            if re.search(r"(?i)(password|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)", str(key))
            else _sanitize_nested_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_nested_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def sanitize_tool_output(args: Any, result: Any) -> SanitizedText:
    del args
    return _project_value(result)


def _valid_argument_label(value: str, depth: int = 4) -> bool:
    if depth < 0:
        return False
    base, separator, nested = value.partition("{")
    if not re.fullmatch(r"(?:command|content|cwd|file|filename|flags|mode|path|query|url|key-[0-9a-f]{10}|additional-keys-withheld)", base):
        return False
    if not separator:
        return True
    if not nested.endswith("}"):
        return False
    body = nested[:-1]
    labels = body.split(",") if body else []
    return len(labels) <= 50 and all(_valid_argument_label(label, depth - 1) for label in labels)


def _validated_projection(value: str) -> bool:
    if value in {"[withheld: secret pattern]", "[withheld: env file]", "unavailable"}:
        return True
    if re.fullmatch(r"\[withheld: \d+ bytes, sha256:[0-9a-f]{12}\]", value):
        return True
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, dict) or set(parsed) != {"keys", "size", "sha256"}:
        return False
    keys = parsed.get("keys")
    return (
        isinstance(keys, list) and len(keys) <= 51
        and all(isinstance(key, str) and _valid_argument_label(key) for key in keys)
        and _safe_count(parsed.get("size")) is not None
        and isinstance(parsed.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{12}", parsed["sha256"])
    )


def display_text(value: str, *, limit: int = DEFAULT_DISPLAY_LIMIT) -> SanitizedText:
    """Preserve only a recognized safe projection; withhold forged wrappers."""
    del limit
    if isinstance(value, SanitizedText) and _validated_projection(str(value)):
        return value
    return _project_value(str(value))


def sanitize_messages(raw_messages: Any) -> list[dict[str, Any]]:
    """Project message records to safe metadata; never copy source payload text."""
    if isinstance(raw_messages, str):
        try:
            raw_messages = json.loads(raw_messages)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(raw_messages, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for message in raw_messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown")
        row: dict[str, Any] = {"role": role}
        if "content" in message:
            row["content"] = _project_value(message["content"])
        if role == "assistant":
            calls = message.get("tool_calls") or []
            if isinstance(calls, list) and calls:
                row["tool_calls"] = [
                    {
                        "name": str((call.get("function") or call).get("name") or "tool")
                        if isinstance(call, dict) and isinstance(call.get("function") or call, dict) else "tool",
                        "arguments": sanitize_tool_arguments(
                            (call.get("function") or call).get("arguments")
                            if isinstance(call, dict) and isinstance(call.get("function") or call, dict) else None
                        ),
                        "status": "observed",
                    }
                    for call in calls
                ]
            elif isinstance(message.get("function_call"), dict):
                call = message["function_call"]
                row["tool_calls"] = [{
                    "name": str(call.get("name") or "tool"),
                    "arguments": sanitize_tool_arguments(call.get("arguments")),
                    "status": "observed",
                }]
        elif role == "tool":
            row["name"] = str(message.get("name") or "tool")
            row["status"] = "result recorded"
            row["result"] = _project_value(message.get("content"))
        for field in ("error", "metadata", "reasoning_content"):
            if field in message:
                row[field] = _project_value(message[field])
        cleaned.append(row)
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
                    else:
                        broken.add("*")
        except (OSError, EOFError, gzip.BadGzipFile):
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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


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
    prompt_identity = _content_fingerprint(prompt)[1]
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    prompt_id = _safe_count(prompt.get("seq"))
                    prompt_stamp = _parse_timestamp(prompt.get("ts") or prompt.get("timestamp"))
                    stamp_key = prompt_stamp.isoformat() if prompt_stamp is not None else "unknown-time"
                    cid = tc.get("id") or f"call_{prompt.get('component', 'unknown')}_{prompt_id if prompt_id is not None else 'unknown'}_{stamp_key}_{prompt_identity}_{len(steps)}"
                    fn = tc.get("function") or tc
                    args = fn.get("arguments") or ""
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    step = {
                        "kind": "tool",
                        "tool_call_id": str(cid),
                        "name": str(fn.get("name") or "tool"),
                        "arguments": sanitize_tool_arguments(args),
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
                "arguments": sanitize_tool_arguments(args),
                "result": SanitizedText("[no next request: final tool call without next prompt]"),
                "source": source,
                "status": "incomplete",
                "duration": None,
                "tokens": None,
            })
    return steps


def _tool_argument_identity(value: Any) -> str:
    """Stable digest for matching calls without exposing argument text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
    return _content_fingerprint(value)[1]


def _response_tool_continuations(prompt: dict[str, Any], later_prompts: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Describe later-observed tool requests for reconciling response calls.

    IDs are reduced to hashes. Id-less calls use tool name plus the existing
    argument digest; a multiset preserves repeated identical calls.
    """
    observed: list[tuple[str, str, str]] = []
    for later in later_prompts:
        messages = later.get("messages") or []
        if isinstance(messages, str):
            try:
                messages = json.loads(messages)
            except (json.JSONDecodeError, TypeError):
                messages = []
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or call
                if not isinstance(function, dict):
                    continue
                call_id = call.get("id")
                id_digest = __import__("hashlib").sha256(str(call_id).encode()).hexdigest()[:12] if call_id else ""
                args_digest = _tool_argument_identity(function.get("arguments"))
                observed.append((id_digest, str(function.get("name") or "tool"), args_digest))
    return observed


def _later_prompts_in_attempt(prompt: dict[str, Any], later_prompts: list[dict[str, Any]], runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stamp = _parse_timestamp(prompt.get("ts") or prompt.get("timestamp"))
    if stamp is None:
        return []
    owners = []
    for run in runs:
        start = _parse_timestamp(run.get("started_at") or run.get("start_time"))
        finish = _parse_timestamp(run.get("finished_at") or run.get("end_time"))
        if start is not None and finish is not None and start <= stamp <= finish:
            owners.append((start, finish))
    if len(owners) != 1:
        return []
    start, finish = owners[0]
    return [row for row in later_prompts
            if (next_stamp := _parse_timestamp(row.get("ts") or row.get("timestamp"))) is not None
            and start <= next_stamp <= finish]


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
    duration_paths = [
        plain if plain.is_file() else compressed
        for date in dates
        for plain, compressed in ((state_root / "llm_calls" / f"{date}.jsonl", state_root / "llm_calls" / f"{date}.jsonl.gz"),)
        if plain.is_file() or compressed.is_file()
    ]

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
        c_prompts = sorted(
            (p for p in raw_prompts if str(p.get("cycle_id")) == cid),
            key=lambda p: (_parse_timestamp(p.get("ts") or p.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc), str(p.get("seq") or "")),
        )
        cycle_broken = any(cid in errors for errors in (broken_runs, broken_prompts, broken_dur, broken_comp))
        c_compactions = [c for c in compactions if str(c.get("cycle_id")) == cid]
        has_compaction = any(c.get("reason") == "compacted" or "compact" in str(c.get("reason", "")) for c in c_compactions)
        has_truncation = any(bool(p.get("truncated")) for p in c_prompts)
        capture_state = "truncated" if has_truncation else "complete"

        reconstruction_state = "complete"
        cycle_reconstruction_incomplete = False
        if read_state != "ok":
            reconstruction_state = "incomplete"
            cycle_reconstruction_incomplete = True
        elif c_runs and not c_prompts:
            reconstruction_state = "incomplete"
            cycle_reconstruction_incomplete = True
        elif cycle_broken or has_compaction:
            reconstruction_state = "incomplete"
            cycle_reconstruction_incomplete = True
        elif not all_reads_ok:
            reconstruction_state = "incomplete"
            cycle_reconstruction_incomplete = True

        sessions_by_role: dict[str, list[dict[str, Any]]] = {}
        steps_by_prompt: dict[int, list[dict[str, Any]]] = {}
        used_duration_ids: set[int] = set()
        for prompt_index, p in enumerate(c_prompts):
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
            response_calls = p.get("tool_calls") or []
            if isinstance(response_calls, str):
                try:
                    response_calls = json.loads(response_calls)
                except (json.JSONDecodeError, TypeError):
                    response_calls = []
            later_prompts = _later_prompts_in_attempt(p, c_prompts[prompt_index + 1:], c_runs)
            continuation_rows = _response_tool_continuations(p, later_prompts)
            filtered_calls = []
            for call in response_calls if isinstance(response_calls, list) else []:
                if not isinstance(call, dict):
                    filtered_calls.append(call)
                    continue
                function = call.get("function") or call
                if not isinstance(function, dict):
                    filtered_calls.append(call)
                    continue
                call_id = call.get("id")
                id_digest = __import__("hashlib").sha256(str(call_id).encode()).hexdigest()[:12] if call_id else ""
                args_digest = _tool_argument_identity(function.get("arguments"))
                signature = (id_digest, str(function.get("name") or "tool"), args_digest)
                try:
                    continuation_rows.remove(signature)
                except ValueError:
                    filtered_calls.append(call)
            sanitized_prompt["tool_calls"] = filtered_calls
            tools = extract_tool_steps(sanitized_prompt)
            if any(t.get("status") in {"incomplete", "pending"} for t in tools) or any(
                isinstance(message, dict) and message.get("_pending_tool_calls")
                for message in sanitized_prompt.get("messages", [])
            ):
                reconstruction_state = "incomplete"
                cycle_reconstruction_incomplete = True
            if p.get("finish_reason") == "tool_calls" and not _response_tool_continuations(
                p, _later_prompts_in_attempt(p, c_prompts[prompt_index + 1:], c_runs),
            ):
                reconstruction_state = "incomplete"
                cycle_reconstruction_incomplete = True
            sanitized_msgs = sanitize_messages(p.get("messages"))
            model_step = {
                "kind": "model",
                "messages": _project_value(sanitized_msgs) if sanitized_msgs else None,
                "answer": _project_value(p.get("content")) if p.get("content") is not None else None,
                "tools": _project_value(p.get("tool_calls")) if p.get("tool_calls") else None,
                "tool_status": "incomplete" if p.get("finish_reason") == "tool_calls" and not _response_tool_continuations(
                    p, _later_prompts_in_attempt(p, c_prompts[prompt_index + 1:], c_runs),
                ) else "observed",
                "function_call": _project_value(p.get("function_call")) if p.get("function_call") else None,
                "reasoning": _project_value(p.get("reasoning_content")) if p.get("reasoning_content") is not None else None,
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
        used_duration_ids: set[int] = set()
        for run in c_runs:
            run_id = str(run.get("run_id") or "unavailable")
            started = _parse_timestamp(run.get("started_at") or run.get("start_time"))
            finished = _parse_timestamp(run.get("finished_at") or run.get("end_time"))
            owned = [
                prompt for prompt in c_prompts
                if (stamp := _parse_timestamp(prompt.get("ts") or prompt.get("timestamp"))) is not None
                and (started is None or finished is None or started <= stamp <= finished)
            ]
            role_rows: dict[str, list[dict[str, Any]]] = {}
            for prompt in owned:
                role = str(prompt.get("component") or "unknown")
                role_rows.setdefault(role, []).append(prompt)
            sessions = []
            call_count = 0
            for role, prompts_for_role in sorted(role_rows.items()):
                model_steps = []
                tool_names = []
                for prompt in prompts_for_role:
                    call_count += 1
                    key = (cid, role, str(prompt.get("seq")))
                    candidates = duration_rows.get(key, [])
                    prompt_time = _parse_timestamp(prompt.get("ts") or prompt.get("timestamp"))
                    duration = None
                    for candidate in candidates:
                        if id(candidate) in used_duration_ids:
                            continue
                        candidate_time = _parse_timestamp(candidate.get("ts") or candidate.get("timestamp"))
                        if prompt_time is None or candidate_time is None or prompt_time == candidate_time:
                            duration = candidate.get("duration_ms")
                            used_duration_ids.add(id(candidate))
                            break
                    names = []
                    messages = prompt.get("messages") or []
                    if isinstance(messages, str):
                        try:
                            messages = json.loads(messages)
                        except (json.JSONDecodeError, TypeError):
                            messages = []
                    if isinstance(messages, list):
                        for message in messages:
                            if not isinstance(message, dict) or message.get("role") != "assistant":
                                continue
                            for call in message.get("tool_calls") or []:
                                if isinstance(call, dict):
                                    function = call.get("function") or call
                                    if isinstance(function, dict) and function.get("name"):
                                        names.append(str(function["name"]))
                    for call in prompt.get("tool_calls") or []:
                        if isinstance(call, dict):
                            function = call.get("function") or call
                            if isinstance(function, dict) and function.get("name"):
                                names.append(str(function["name"]))
                    tool_names.extend(names)
                    prompt_steps = steps_by_prompt.get(id(prompt), [])
                    model_steps.extend(prompt_steps)
                model_calls = [step for step in model_steps if step.get("kind") == "model"]
                token_values = [step.get("tokens") for step in model_calls]
                duration_values = [step.get("duration") for step in model_calls]
                sessions.append({"role": role, "history_complete": not cycle_broken and read_state == "ok",
                                 "model_calls": len(prompts_for_role), "steps": model_steps,
                                 "tool_names": tool_names,
                                 "tokens": sum(value for value in token_values if isinstance(value, int)) if all(isinstance(value, int) for value in token_values) else None,
                                 "duration_ms": sum(value for value in duration_values if isinstance(value, (int, float))) if all(isinstance(value, (int, float)) for value in duration_values) else None})
            killed = run.get("classification") in {"unit_timeout", "killed"}
            run_state = not killed
            complete = read_state == "ok" and not cycle_broken and not has_compaction and bool(owned) and run_state
            attempts.append({"run_id": run_id, "classification": run.get("classification") or "unknown",
                             "model_call_count": call_count, "sessions": sessions,
                             "history_complete": complete, "outcome": run.get("outcome") or run.get("classification") or "unknown"})
        owned_prompts = set()
        for run in c_runs:
            started = _parse_timestamp(run.get("started_at") or run.get("start_time"))
            finished = _parse_timestamp(run.get("finished_at") or run.get("end_time"))
            for prompt in c_prompts:
                stamp = _parse_timestamp(prompt.get("ts") or prompt.get("timestamp"))
                if stamp is not None and (started is None or finished is None or started <= stamp <= finished):
                    owned_prompts.add(id(prompt))
        unassigned = [prompt for prompt in c_prompts if id(prompt) not in owned_prompts]
        if unassigned:
            tool_names = []
            for prompt in unassigned:
                messages = prompt.get("messages") or []
                if isinstance(messages, list):
                    for message in messages:
                        if isinstance(message, dict) and message.get("role") == "assistant":
                            for call in message.get("tool_calls") or []:
                                fn = call.get("function") or call if isinstance(call, dict) else {}
                                if isinstance(fn, dict) and fn.get("name"):
                                    tool_names.append(str(fn["name"]))
            attempts.append({"run_id": "unassigned", "classification": "unknown",
                             "model_call_count": len(unassigned), "history_complete": False,
                             "outcome": "unknown", "sessions": [{"role": "unassigned",
                             "history_complete": False, "model_calls": len(unassigned),
                             "steps": [{"kind": "model", "tokens": 0, "duration": "unknown"} for _ in unassigned],
                             "tool_names": tool_names}]})

        history_complete = (
            read_state == "ok"
            and capture_state == "complete"
            and reconstruction_state == "complete"
            and not cycle_reconstruction_incomplete
            and not any(a["classification"] in {"unit_timeout", "killed"} for a in attempts)
            and bool(attempts)
            and bool(c_prompts)
        )

        sessions = [session for attempt in attempts for session in attempt.get("sessions", [])]
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
            parts.append(f"<p><b>{label.title()}:</b> {_escape(str(display_text(value)))}</p>")
    tokens = _safe_count(step.get("tokens"))
    if tokens is not None:
        parts.append(f"<p>Tokens: {tokens}</p>")
    if step.get("duration") is not None:
        parts.append(f"<p>Duration: {_escape(_safe_duration(step['duration']))}</p>")
    parts.append("</div>")
    return "".join(parts)


def format_tool_step(step: dict[str, Any]) -> str:
    name = _safe_identifier(step.get("name"))
    raw_args = step.get("arguments", SanitizedText("unavailable"))
    if not isinstance(raw_args, SanitizedText):
        raise TypeError("arguments must be SanitizedText")
    args = display_text(raw_args)
    res_val = step.get("result")
    if res_val is not None and not isinstance(res_val, SanitizedText):
        raise TypeError("result must be SanitizedText")
    result = display_text(sanitize_tool_output(str(raw_args), str(res_val))) if res_val is not None else SanitizedText("unavailable")
    dur_val = step.get("duration")
    duration = _safe_duration(dur_val)
    source_value = step.get("source")
    source = "reconstructed from request" if isinstance(source_value, str) and source_value.startswith("reconstructed from request") else "recorded"
    status = _safe_enum(step.get("status"), TOOL_STATUSES)
    return f"<div class=\"step-tool\"><p>Tool step: {_escape(_safe_tool_name(name))}({_escape(args)}) → {_escape(result)}; status: {_escape(status)}; duration: {_escape(duration)}; source: {_escape(source)}</p></div>"


def render_cycle_page(cycle_id: str, data: dict[str, Any] | None) -> str:
    """Render private cycle details from bounded, typed display fields only."""
    safe_cycle_id = _safe_identifier(cycle_id)
    if (safe_cycle_id == "unavailable" or not isinstance(data, dict)
            or data.get("available") is False):
        return f'<main><h1>Cycle {_escape(safe_cycle_id)}</h1><p class="unavailable">Cycle detail unavailable: source data unavailable.</p></main>'

    attempts = data.get("attempts")
    attempts = attempts[:100] if isinstance(attempts, list) else []
    sessions = data.get("sessions")
    sessions = sessions[:100] if isinstance(sessions, list) else []
    total_calls = _safe_count(data.get("total_model_calls"))
    reflection = data.get("reflection") if isinstance(data.get("reflection"), dict) else None

    rows = [f'<main><h1>Cycle {_escape(safe_cycle_id)}</h1>']
    rows.append(f'<div class="cycle-summary"><p>Total model calls: {total_calls if total_calls is not None else "unknown"}</p>')
    rows.append(f'<p>{_safe_state_text(data.get("history_state") if "history_state" in data else ("history complete" if data.get("history_complete") is True else "history incomplete"))}</p></div>')

    if reflection:
        rows.append('<section class="reflection-summary"><h2>Reflector summary</h2>')
        for label, key in (("Summary chars", "summary_chars"), ("Findings count", "findings_count"), ("Recommendations count", "recommendations_count")):
            count = _safe_count(reflection.get(key))
            rows.append(f'<p>{label}: {count if count is not None else "unknown"}</p>')
        rows.append('</section>')

    rows.append('<section class="attempts-section"><h2>Attempts</h2>')
    if not attempts:
        rows.append('<p class="unavailable">Attempt records unavailable.</p>')
    for att in attempts:
        if not isinstance(att, dict):
            continue
        run_id = _safe_identifier(att.get("run_id"))
        classification = _safe_enum(att.get("classification"), CLASSIFICATIONS)
        call_count = _safe_count(att.get("model_call_count"))
        calls_text = f"<p>Model calls: {call_count}</p>" if call_count is not None else ""
        outcome = _safe_enum(att.get("outcome"), OUTCOMES)
        rows.append(f'<article class="attempt-row"><h3>Attempt {_escape(run_id)}</h3>{calls_text}<p>Classification: {_escape(classification)}</p><p>Outcome: {_escape(outcome)}</p><p>{_safe_state_text("history complete" if att.get("history_complete") is True else "history incomplete")}</p>')
        att_sessions = att.get("sessions")
        att_sessions = att_sessions[:100] if isinstance(att_sessions, list) else []
        for sess in att_sessions:
            if not isinstance(sess, dict):
                continue
            role = _safe_enum(sess.get("role"), SESSION_ROLES)
            model_calls = _safe_count(sess.get("model_calls"))
            tokens = _safe_count(sess.get("tokens"))
            duration = _safe_duration(sess.get("duration_ms"))
            tools = _safe_tool_sequence(sess.get("tool_names"))
            rows.append(f'<section class="attempt-session"><h4>Session {_escape(role)}</h4><p>Model calls: {model_calls if model_calls is not None else "unknown"}</p><p>Tokens: {tokens if tokens is not None else "unknown"}</p><p>Duration: {_escape(duration)}</p><p>Tool sequence: {_escape(tools)}</p>')
            steps = sess.get("steps")
            for step in steps[:500] if isinstance(steps, list) else []:
                if isinstance(step, dict):
                    rows.append(format_tool_step(step) if step.get("kind") == "tool" else format_model_step(step))
            rows.append('</section>')
        rows.append('</article>')
    rows.append('</section>')

    if not attempts:
        rows.append('<section class="sessions-section"><h2>Sessions</h2>')
        if not sessions:
            rows.append('<p class="unavailable">Session records unavailable.</p>')
        for sess in sessions:
            if not isinstance(sess, dict):
                continue
            role = _safe_enum(sess.get("role"), SESSION_ROLES)
            model_calls = _safe_count(sess.get("model_calls"))
            rows.append(f'<article class="session-block"><h3>Session {_escape(role)}</h3><p>Model calls: {model_calls if model_calls is not None else "unknown"}</p><p>{_safe_state_text("history complete" if sess.get("history_complete") is True else "history incomplete")}</p>')
            steps = sess.get("steps")
            for step in steps[:500] if isinstance(steps, list) else []:
                if isinstance(step, dict):
                    rows.append(format_tool_step(step) if step.get("kind") == "tool" else format_model_step(step))
            rows.append('</article>')
        rows.append('</section>')
    rows.append('</main>')
    return "".join(rows)
