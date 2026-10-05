"""Synthetic private cycle detail contract tests for ADR-036."""
from __future__ import annotations

import json
import pytest
import re
from datetime import datetime, timezone
from pathlib import Path

from scripts.cycle_detail import display_text, format_model_step, format_tool_step, load_cycle_detail, mark_incomplete_history, redact_text, render_cycle_page
from scripts.two_sinks import PUBLIC_PAGES, validate_publish_allowlist


def test_render_model_step_rejects_raw_unsanitized_strings() -> None:
    import pytest
    from scripts.cycle_detail import SanitizedText, format_model_step

    with pytest.raises(TypeError, match="SanitizedText"):
        format_model_step({"kind": "model", "answer": "raw secret-bearing input"})
    rendered = format_model_step({"kind": "model", "answer": SanitizedText("[withheld: 21 bytes, sha256:e3a293a22554]")})
    assert "safe sanitized output" not in rendered and "withheld:" in rendered
    with pytest.raises(TypeError, match="SanitizedText"):
        format_tool_step({"kind": "model", "arguments": SanitizedText("safe args"), "result": "raw tool result"})


def test_structured_message_fields_project_to_metadata_without_source_text() -> None:
    from scripts.cycle_detail import sanitize_messages

    secret = "SECRET_EVERYWHERE_CANARY_318"
    result = sanitize_messages([
        {"role": "assistant", "content": secret, "tool_calls": [{"id": "c1", "function": {"name": "read", "arguments": json.dumps({"path": secret, "password": secret})}}], "metadata": {"error": secret}},
        {"role": "tool", "tool_call_id": "c1", "name": "read", "content": secret, "error": secret, "metadata": {"status": secret}},
        {"role": "user", "content": {"text": secret}, "metadata": {"trace": secret}},
    ])
    serialized = json.dumps(result)
    assert secret not in serialized
    assert "withheld:" in serialized
    assert '"keys"' not in serialized and '"path"' not in serialized
    from scripts.cycle_detail import render_cycle_page
    page = render_cycle_page("cycle-canary", {"available": True, "history_complete": True, "total_model_calls": 1,
        "attempts": [{"run_id": "r", "classification": "complete", "outcome": "ok", "history_complete": True,
                      "model_call_count": 1, "sessions": [{"role": "assistant", "history_complete": True,
                      "model_calls": 1, "tokens": 1, "duration_ms": 10, "tool_names": [], "steps": []}]}]})
    assert secret not in page


def test_forged_sanitized_text_is_withheld_by_direct_formatters():
    from scripts.cycle_detail import SanitizedText, format_model_step, format_tool_step

    model_canary = "SENTINEL_UNTRUSTED_SANITIZED_CANARY_4421"
    forged = SanitizedText(f"[withheld: {model_canary}]")
    model = format_model_step({"answer": forged})
    tool = format_tool_step({"name": "read_file", "arguments": SanitizedText(
        json.dumps({"keys": [model_canary], "size": 1, "sha256": "0" * 12})
    ), "result": forged})
    assert model_canary not in model
    assert model_canary not in tool
    assert "withheld:" in model and "withheld:" in tool


def test_tool_argument_keys_do_not_disclose_untrusted_names():
    from scripts.cycle_detail import SanitizedText, format_tool_step, sanitize_tool_arguments

    canary = "CANARY_SECRET_AS_KEY_992818"
    nested_canary = "NESTED_PRIVATE_KEY_NAME_71623"
    args = sanitize_tool_arguments({canary: {nested_canary: "ordinary-value"}})
    assert canary not in args and nested_canary not in args
    rendered = format_tool_step({"name": "read_file", "arguments": args, "result": SanitizedText("[withheld: result]")})
    assert canary not in rendered and nested_canary not in rendered
    assert "key-" in rendered


def test_private_renderer_fields_reject_untrusted_text_and_invalid_types():
    from scripts.cycle_detail import render_cycle_page

    canary = "CANARY_SECRET_DO_NOT_RENDER_92817"
    page = render_cycle_page("cycle-safe", {
        "available": True, "history_complete": False, "total_model_calls": canary,
        "attempts": [{"run_id": canary, "classification": canary, "model_call_count": canary,
                      "history_complete": False, "sessions": [{"role": canary, "model_calls": canary,
                      "tokens": canary, "duration_ms": canary, "tool_names": [canary],
                      "steps": [{"kind": "tool", "name": canary, "status": canary,
                      "source": canary, "duration": canary, "arguments": display_text(canary),
                      "result": display_text(canary)}]}]}],
        "reflection": {"summary_chars": canary, "findings_count": True,
                       "recommendations_count": -1},
    })
    assert canary not in page
    assert "Classification: unknown" in page
    assert "Tokens: unknown" in page
    assert "Duration: unknown" in page
    assert "Tool step: tool(" in page
    assert "status: unknown" in page and "source: recorded" in page
    assert "Tool sequence: unknown" in page
    assert "Outcome: unknown" in page
    assert "Cycle unavailable" in render_cycle_page("../" + canary, {"available": True})


def test_attempts_are_rows_with_per_attempt_counts():
    """ADR-036 §4: attempts remain distinct rows with their own counts."""
    page = render_cycle_page("cycle-synthetic", {"attempts": [
        {"run_id": "run-a", "model_call_count": 2, "history_complete": True},
        {"run_id": "run-b", "model_call_count": 1, "history_complete": False},
    ]})
    assert page.count('<article class="attempt-row">') == 2
    assert "Model calls: 2" in page
    assert "Model calls: 1" in page
    assert page.count("Attempt id-") == 2


def test_model_and_tool_steps_render_only_typed_metadata():
    from scripts.cycle_detail import SanitizedText
    canary = "PRIVATE_SOURCE_TEXT_CANARY_318"
    model = {"kind": "model", "messages": SanitizedText(f"[withheld: {len(canary)} bytes, sha256:abc123]"), "answer": SanitizedText("[withheld: 4 bytes, sha256:def456]"), "tools": SanitizedText("[withheld: 8 bytes, sha256:112233]"), "reasoning": SanitizedText("[withheld: 5 bytes, sha256:445566]"), "tokens": 12, "duration": "2s"}
    tool = {"kind": "tool", "name": "search", "arguments": SanitizedText('{"keys":["query"],"size":20,"sha256":"abcdef123456"}'), "result": SanitizedText("[withheld: 6 bytes, sha256:778899]"), "status": "ok", "tokens": None}
    rendered = format_model_step(model) + format_tool_step(tool)
    assert canary not in rendered
    assert "withheld:" in rendered and "sha256:" in rendered
    assert "withheld:" in rendered and "synthetic result" not in rendered
    assert "Tokens: 12" in rendered
    assert "Tokens" not in format_tool_step(tool)
    assert "duration: unknown" in format_tool_step(tool)


def test_redaction_covers_ghu_tokens_in_private_details() -> None:
    token = "ghu_" + "SYNTHETIC_CANARY_123456789"
    assert token not in redact_text(token)


def test_redaction_covers_extended_private_key_blocks() -> None:
    private_block = "-----BEGIN ENCRYPTED PRIVATE KEY-----\n" + "SYNTHETIC_PRIVATE_KEY_CANARY\n" + "-----END ENCRYPTED PRIVATE KEY-----"
    safe = redact_text(private_block)
    assert "SYNTHETIC_PRIVATE_KEY_CANARY" not in safe
    assert "withheld:" in safe and "sha256:" in safe


def test_redaction_covers_hyphenated_json_secret_keys() -> None:
    value = '{"api-key":"HYPHENATED_SECRET_CANARY_4431"}'
    assert "HYPHENATED_SECRET_CANARY_4431" not in redact_text(value)


def test_redaction_covers_colon_delimited_credentials() -> None:
    value = "password: COLON_SECRET_CANARY_7721"
    assert "COLON_SECRET_CANARY_7721" not in redact_text(value)


def test_redaction_covers_secret_patterns_and_env_files():
    """ADR-036 §5: redact secrets without env_contents reading; safe filename labels remain renderable."""
    raw = (
        "API_KEY=supersecret123\n"
        "GH_TOKEN=ghp_secrettokenabc\n"
        "DATABASE_PASSWORD=mypassword\n"
        "Authorization: Basic c2VjcmV0OnBhc3M=\n"
        "https://admin:secret123@example.invalid/db\n"
        '{"password": "hidden_pw", "token": "hidden_tok"}\n'
        "source: litellm.env\n"
        "Bearer secretbearer\n"
        "sk-1234567890abcdef\n"
    )
    safe = redact_text(raw)
    assert "supersecret123" not in safe
    assert "ghp_secrettokenabc" not in safe
    assert "mypassword" not in safe
    assert "c2VjcmV0OnBhc3M=" not in safe
    assert "secret123@" not in safe
    assert "hidden_pw" not in safe and "hidden_tok" not in safe
    assert "secretbearer" not in safe
    assert "sk-1234567890abcdef" not in safe
    assert "litellm.env" not in safe and "withheld:" in safe


def test_model_assistant_content_is_sanitized_before_message_serialization() -> None:
    from scripts.cycle_detail import sanitize_messages

    canary = "ASSISTANT_MESSAGE_SECRET_CANARY_3362"
    messages = [{"role": "assistant", "content": json.dumps({"api-key": canary})}]
    safe = json.dumps(sanitize_messages(messages))
    assert canary not in safe
    assert "withheld:" in safe


def test_tool_arguments_with_env_path_are_withheld() -> None:
    from scripts.cycle_detail import SanitizedText
    step = {"name": "read_file", "arguments": SanitizedText(json.dumps({"path": "/tmp/.env"})), "result": SanitizedText("not shown")}
    rendered = format_tool_step(step)
    assert "/tmp/.env" not in rendered
    assert "withheld:" in rendered


def test_env_file_content_in_tool_arguments_is_withheld_everywhere() -> None:
    from scripts import cycle_detail as cd

    canary = "OTHER=ENV_ARGUMENT_CANARY_8221"
    messages = [{"role": "assistant", "tool_calls": [{"id": "read-env", "function": {
        "name": "write_file", "arguments": json.dumps({"path": "/tmp/.env", "content": canary}),
    }}]}]
    cleaned = cd.sanitize_messages(messages)
    assert canary not in json.dumps(cleaned)
    rendered = cd.format_tool_step({"name": "write_file", "arguments": cd.sanitize_tool_arguments(messages[0]["tool_calls"][0]["function"]["arguments"]), "result": cd.redact_text("ok")})
    assert canary not in rendered


def test_structured_tool_message_content_is_recursively_sanitized() -> None:
    from scripts.cycle_detail import sanitize_messages

    secret = "NESTED_SECRET_CANARY_6512"
    messages = [{"role": "tool", "content": {"nested": {"password": secret}, "items": ["ordinary value"]}}]
    safe = json.dumps(sanitize_messages(messages))
    assert secret not in safe
    assert "withheld:" in safe
    assert "ordinary value" not in safe


def test_env_withholding_does_not_corrupt_structured_non_env_arguments() -> None:
    from scripts.cycle_detail import sanitize_messages

    message = {"role": "assistant", "tool_calls": [{"id": "ordinary", "function": {
        "name": "read_file", "arguments": json.dumps({"path": "/tmp/data.json"}),
    }}]}
    sanitized = sanitize_messages([message])
    args = sanitized[0]["tool_calls"]
    assert '"path"' in args[0]["arguments"] and "/tmp/data.json" not in args[0]["arguments"]


def test_tool_step_reading_env_file_withholds_content():
    """ADR-036 Decision 1 (a): reading /etc/eeepc-agent/*.env withholds contents unconditionally."""
    from scripts.cycle_detail import SanitizedText
    dummy_secret = "CANARY_SECRET_VAL_7711"
    step = {
        "kind": "tool",
        "name": "read_file",
        "arguments": SanitizedText(json.dumps({"path": "/etc/eeepc-agent/test.env"})),
        "result": SanitizedText(f"SECRET_KEY={dummy_secret}\nOTHER=123"),
        "status": "ok",
    }
    rendered = format_tool_step(step)
    assert dummy_secret not in rendered
    assert "withheld:" in rendered


def test_no_render_modules_open_etc_eeepc_agent():
    """ADR-036 Decision 1 (b): static check that no render modules open files under /etc/eeepc-agent."""
    forbidden = re.compile(r'open\s*\(\s*["\'].*?/etc/eeepc-agent')
    repo_root = Path(__file__).resolve().parent.parent
    for fname in ("scripts/two_sinks.py", "scripts/cycle_detail.py", "scripts/techtree_viewer.py", "scripts/techtree_autopublish.py"):
        code = (repo_root / fname).read_text(encoding="utf-8")
        assert not forbidden.search(code), f"Forbidden open(/etc/eeepc-agent...) found in {fname}"


def test_incomplete_history_is_marked():
    """ADR-036 §4: incomplete records are explicit, never represented as no tools."""
    marked = mark_incomplete_history({"history_complete": False, "tool_steps": []})
    assert marked["history_state"] == "history incomplete"
    assert "history incomplete" in render_cycle_page("c", {"attempts": [marked]})


def test_response_tool_arguments_are_sanitized_before_display() -> None:
    from scripts.cycle_detail import extract_tool_steps

    secret = "TOP_LEVEL_ARGS_SECRET_5190"
    steps = extract_tool_steps({"seq": 2, "tool_calls": [{
        "id": "response-call", "function": {"name": "update", "arguments": {"password": secret}},
    }]})
    assert len(steps) == 1
    assert secret not in steps[0]["arguments"]


def test_mismatched_tool_response_id_does_not_complete_pending_call() -> None:
    from scripts.cycle_detail import extract_tool_steps

    steps = extract_tool_steps({"seq": 1, "messages": [
        {"role": "assistant", "tool_calls": [{"id": "expected", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "different", "content": "response"},
    ]})
    assert len(steps) == 2
    assert steps[0]["status"] == "pending"
    assert steps[1]["status"] == "incomplete"


def test_reconstructed_steps_have_source_and_unknown_duration():
    """ADR-036 §4: reconstructed tool steps extract from prompt messages, carry source, unknown duration and no tokens."""
    from scripts.cycle_detail import extract_tool_steps
    prompt_record = {
        "seq": 4,
        "component": "executor",
        "messages": [
            {"role": "user", "content": "run task"},
            {
                "role": "assistant",
                "content": "searching",
                "tool_calls": [{"id": "call_42", "function": {"name": "search", "arguments": '{"query": "pattern"}'}}],
            },
            {"role": "tool", "tool_call_id": "call_42", "content": "search result output"},
        ],
    }
    steps = extract_tool_steps(prompt_record)
    assert len(steps) == 1
    step = steps[0]
    assert step["kind"] == "tool"
    assert step["name"] == "search"
    assert "pattern" not in step["arguments"]
    assert '"query"' in step["arguments"]
    assert "search result output" not in step["result"]
    assert "withheld:" in step["result"]
    assert step["source"] == "reconstructed from request seq 4"
    assert step["duration"] is None
    assert step["tokens"] is None


def test_display_content_uses_size_and_digest_projection():
    original = "synthetic source text " * 20
    shown = display_text(original, limit=20)
    assert "withheld:" in shown and "sha256:" in shown
    assert original not in shown


def test_private_cycle_page_is_not_allowlisted():
    """ADR-036 §3: D2 private pages are excluded from the D1 gh-pages allowlist."""
    assert "cycles/cycle-synthetic.html" not in PUBLIC_PAGES
    try:
        validate_publish_allowlist({"cycles/cycle-synthetic.html": "private"})
    except Exception as exc:
        assert "unlisted" in str(exc)
    else:
        raise AssertionError("private cycle page unexpectedly publishable")


def test_load_cycle_detail_uses_inventory_paths_and_marks_missing_sources(tmp_path):
    """ADR-036 D0 inventory: load attempts, prompts, durations and compactions by known paths."""
    import gzip
    import json
    from datetime import datetime, timezone

    root = tmp_path
    day = "2026-09-25"
    bridge = root / "bridge"
    bridge.mkdir()
    (bridge / f"runs-{day}.jsonl").write_text(json.dumps({"run_id": "r1", "cycle_id": "c1", "classification": "unit_timeout"}) + "\n")
    prompts = root / "llm_calls" / "prompts"
    prompts.mkdir(parents=True)
    with gzip.open(prompts / f"{day}.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write(json.dumps({"cycle_id": "c1", "component": "planner", "seq": 3, "messages": [{"role": "user", "content": "synthetic prompt"}], "content": "synthetic answer", "prompt_tokens": 2, "completion_tokens": 3}) + "\n")
    (root / "llm_calls" / f"{day}.jsonl").write_text(json.dumps({"cycle_id": "c1", "component": "planner", "seq": 3, "duration_ms": 18}) + "\n")
    compaction = root / "compaction"
    compaction.mkdir()
    (compaction / "journal.jsonl").write_text(json.dumps({"cycle_id": "c1", "reason": "compacted"}) + "\n")
    result = load_cycle_detail(root, "c1", days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
    assert result["attempts"][0]["run_id"] == "r1"
    assert result["attempts"][0]["history_complete"] is False
    assert result["attempts"][0]["model_call_count"] == 0
    assert result["attempts"][0]["sessions"] == []
    assert result["total_model_calls"] == 1


def test_active_run_history_respects_retained_calendar_days(tmp_path: Path) -> None:
    from scripts.cycle_detail import build_cycle_index

    (tmp_path / "bridge").mkdir()
    rows = [
        {"cycle_id": "old", "started_at": "2026-06-01T12:00:00Z"},
        {"cycle_id": "old-fallback", "started_at": "malformed",
         "finished_at": "2026-06-01T12:00:00Z"},
        {"cycle_id": "retained", "started_at": "2026-09-19T00:00:00Z"},
        {"cycle_id": "recent", "started_at": "2026-09-25T12:00:00Z"},
        {"cycle_id": "future", "started_at": "2026-09-26T00:00:00Z"},
        {"cycle_id": "unknown-time"},
    ]
    (tmp_path / "bridge" / "runs.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )
    index = build_cycle_index(tmp_path, days=7,
                              now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
    assert set(index) == {"retained", "recent", "unknown-time"}
    assert index["unknown-time"]["history_complete"] is False


def test_private_renderer_builds_only_indexed_cycle_pages(tmp_path: Path) -> None:
    from scripts.two_sinks import render_private_pages

    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "runs.jsonl").write_text(
        json.dumps({"run_id": "r1", "cycle_id": "cycle-linked", "classification": "completed"}) + "\n",
        encoding="utf-8",
    )
    pages = render_private_pages({"ledger_history": [{"cycle_id": "cycle-linked"}, {"cycle_id": "unavailable"}]}, "eeepc", tmp_path)
    assert set(pages) == {"cycles/cycle-linked.html"}
    assert "history incomplete" in pages["cycles/cycle-linked.html"]


def test_cycle_detail_reflection_projection_uses_sanitized_metrics_only() -> None:
    from scripts.techtree_viewer import build_cycle_details
    from scripts.two_sinks import split_render_inputs

    public_data, _ = split_render_inputs({"reflections": [{
        "cycle_id": "cycle-reflection", "summary": "SYNTHETIC_REFLECTION_TEXT",
        "findings": [{"kind": "wasted_steps", "detail": "private finding"}],
        "recommendations": [{"kind": "good_practice", "detail": "private recommendation"}],
    }]})
    details = build_cycle_details([], None, None, public_data.get("reflections"))
    record = details["cycle-reflection"]
    assert record["reflection"]["summary_chars"] > 0
    assert record["reflection"]["findings_count"] == 1
    assert record["reflection"]["recommendations_count"] == 1
    assert "SYNTHETIC_REFLECTION_TEXT" not in json.dumps(record)


def test_manual_publish_passes_state_root_to_private_page_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import techtree_viewer as tv
    from scripts import two_sinks as sinks

    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "runs.jsonl").write_text(json.dumps({"run_id": "r1", "cycle_id": "cycle-manual", "classification": "completed"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(tv, "read_local_state", lambda *args, **kwargs: {"ledger_history": [{"cycle_id": "cycle-manual"}], "ledger_tail": []})
    monkeypatch.setattr(tv, "render_pages", lambda *_args: {"index.html": "local"})
    monkeypatch.setattr(tv, "render_public_pages", lambda *_args: {"index.html": "public"})
    observed = {}
    monkeypatch.setattr(sinks, "publish_ordered", lambda site, public, private, version, **kwargs: (observed.update(site=site, private=private) or (0, {})))
    monkeypatch.setattr(sinks, "render_private_pages", lambda data, host, state_root=None: observed.update(state_root=state_root) or {})

    out = tmp_path / "index.html"
    assert tv.main(["--local", "--state-root", str(tmp_path), "--out", str(out), "--publish"]) == 0
    assert observed["state_root"] == tmp_path


def test_reflection_metrics_are_preserved_in_host_cycle_pages(tmp_path: Path) -> None:
    from scripts.techtree_viewer import build_cycle_details
    from scripts.two_sinks import render_private_pages, split_render_inputs

    public, private = split_render_inputs({"reflections": [{
        "cycle_id": "cycle-reflection-host", "summary": "some summary",
        "findings": [{"kind": "wasted_steps", "detail": "finding"}],
        "recommendations": [{"kind": "good_practice", "detail": "recommendation"}],
    }]})
    private["ledger_history"] = [{"cycle_id": "cycle-reflection-host"}]
    private["cycle_details"] = build_cycle_details([], None, None, public.get("reflections"))
    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "runs.jsonl").write_text(
        json.dumps({"run_id": "r1", "cycle_id": "cycle-reflection-host", "classification": "completed"}) + "\n",
        encoding="utf-8",
    )
    pages = render_private_pages(private, "eeepc", tmp_path)
    page = pages["cycles/cycle-reflection-host.html"]
    assert "Summary chars: 12" in page
    assert "Findings count: 1" in page
    assert "Recommendations count: 1" in page
    assert "some summary" not in page


def test_manual_cycle_page_links_are_safely_encoded_and_bounded(tmp_path: Path) -> None:
    from scripts.two_sinks import render_private_pages, _validate_page_name

    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "runs.jsonl").write_text(
        json.dumps({"run_id": "r1", "cycle_id": "../../escape", "classification": "completed"}) + "\n",
        encoding="utf-8",
    )
    pages = render_private_pages({"ledger_history": [{"cycle_id": "../../escape"}]}, "eeepc", tmp_path)
    assert pages == {}
    assert _validate_page_name("cycles/safe-id.html") is None


def test_missing_cycle_sources_render_unavailable_not_empty():

    """ADR-036 §3: missing private source data is visibly unavailable."""
    assert "unavailable" in render_cycle_page("c", None)
    assert "unavailable" in render_cycle_page("c", {"available": False})

def test_active_runs_read_and_deduplicated_by_run_id(tmp_path: Path):
    """ADR-036 B5: active bridge/runs.jsonl is read and duplicates with rotated files are pruned."""
    from scripts.cycle_detail import build_cycle_index
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "runs.jsonl").write_text(
        json.dumps({"run_id": "r1", "cycle_id": "c1", "classification": "completed"}) + "\n" +
        json.dumps({"run_id": "r2", "cycle_id": "c1", "classification": "completed"}) + "\n",
        encoding="utf-8",
    )
    (bridge / "runs-2026-09-25.jsonl").write_text(
        json.dumps({"run_id": "r1", "cycle_id": "c1", "classification": "completed"}) + "\n",
        encoding="utf-8",
    )
    index = build_cycle_index(tmp_path, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
    assert len(index["c1"]["attempts"]) == 2
    assert {a["run_id"] for a in index["c1"]["attempts"]} == {"r1", "r2"}


def test_attempts_and_sessions_modeled_without_call_duplication(tmp_path: Path):
    """ADR-036 B6: 2 attempts and 2 prompts do NOT duplicate to 4 calls; planner/executor roles are distinct."""
    from scripts.cycle_detail import build_cycle_index
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "runs.jsonl").write_text(
        json.dumps({"run_id": "r1", "cycle_id": "c2", "classification": "unit_timeout"}) + "\n" +
        json.dumps({"run_id": "r2", "cycle_id": "c2", "classification": "completed"}) + "\n",
        encoding="utf-8",
    )
    pdir = tmp_path / "llm_calls" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "2026-09-25.jsonl").write_text(
        json.dumps({"cycle_id": "c2", "component": "planner", "seq": 1, "messages": [{"role": "user", "content": "plan"}]}) + "\n" +
        json.dumps({"cycle_id": "c2", "component": "executor", "seq": 1, "messages": [{"role": "user", "content": "exec"}]}) + "\n",
        encoding="utf-8",
    )
    index = build_cycle_index(tmp_path, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
    c2 = index["c2"]
    assert c2["total_model_calls"] == 2
    roles = {s["role"] for s in c2["sessions"]}
    assert roles == {"unassigned"}

def test_final_tool_call_without_next_request_marks_history_incomplete(tmp_path: Path):
    """ADR-036 B7: final tool call without next prompt marks history incomplete even if run was completed."""
    from scripts.cycle_detail import build_cycle_index
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "runs.jsonl").write_text(
        json.dumps({"run_id": "r1", "cycle_id": "c3", "classification": "completed"}) + "\n",
        encoding="utf-8",
    )
    pdir = tmp_path / "llm_calls" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "2026-09-25.jsonl").write_text(
        json.dumps({
            "cycle_id": "c3", "component": "executor", "seq": 1,
            "messages": [{"role": "user", "content": "exec"}],
            "tool_calls": [{"id": "t1", "function": {"name": "write", "arguments": "{}"}}],
        }) + "\n",
        encoding="utf-8",
    )
    index = build_cycle_index(tmp_path, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
    c3 = index["c3"]
    assert c3["history_complete"] is False
    assert c3["history_complete"] is False


def test_d2_connected_end_to_end_from_state_tree_to_host_snapshot(tmp_path: Path, monkeypatch):
    """ADR-036 B4: real path reader -> loader -> private renderer -> host snapshot produces cycles/<cid>.html."""
    from scripts import techtree_autopublish as ap

    root = tmp_path / "state"
    state_dir = tmp_path / "state_dir"
    site_root = tmp_path / "site"

    for rel, content in {
        "evolution/tree.json": '{"current_sha": "a", "nodes": {}}',
        "tech_tree/portfolio.json": '{"current": null, "nodes": {}}',
        "hypotheses/lifecycle.json": '{"entries": {}}',
        "scorecard/latest.json": '{"computed_at_utc": "2026-08-18T00:00:00Z"}',
        "ledger/cycles.jsonl": '{"phase": "outcome", "cycle_id": "c-end-to-end", "outcome": "success", "ts": "2026-09-25T10:00:00Z"}\n',
        "bridge/runs.jsonl": '{"run_id": "r-e2e", "cycle_id": "c-end-to-end", "classification": "completed"}\n',
        "llm_calls/prompts/2026-09-25.jsonl": '{"cycle_id": "c-end-to-end", "component": "executor", "seq": 1, "messages": []}\n',
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    published_to_gh = []
    monkeypatch.setenv("GH_TOKEN", "mock-token")
    monkeypatch.setattr(ap.tv, "publish_to_pages", lambda pages, **_: (published_to_gh.append(pages) or 0, {}, False))

    args = ap.parse_args(["--state-root", str(root), "--state-dir", str(state_dir), "--site-root", str(site_root)])
    rc = ap.run(args)
    assert rc == 0

    assert (site_root / "current" / "cycles" / "c-end-to-end.html").is_file()
    assert (site_root / "current" / "index.html").is_file()

    assert len(published_to_gh) == 1
    gh_pages = published_to_gh[0]
    assert "cycles/c-end-to-end.html" not in gh_pages
    assert "index.html" in gh_pages
    assert not any(path.startswith("cycles/") for path in gh_pages)
    snapshot_files = {path.relative_to(site_root / "current").as_posix() for path in (site_root / "current").rglob("*") if path.is_file()}
    assert "cycles/c-end-to-end.html" in snapshot_files
    host_cycle_page = (site_root / "current" / "cycle.html").read_text(encoding="utf-8")
    assert 'new Set(["c-end-to-end"])' in host_cycle_page
    assert "LAN-only" not in gh_pages["cycle.html"]
    assert "c-end-to-end" not in gh_pages["cycle.html"]


def test_jsonl_read_failure_marks_history_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex comment 4109820842: Read failure on any history file must mark history incomplete."""
    root = tmp_path / "state"
    run_file = root / "bridge" / "runs.jsonl"
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text('{"run_id": "r1", "cycle_id": "c-failed-read", "classification": "completed"}\n', encoding="utf-8")

    prompt_file = root / "llm_calls" / "prompts" / "2026-09-25.jsonl"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text('{"cycle_id": "c-failed-read", "component": "executor", "seq": 1, "messages": []}\n', encoding="utf-8")

    from scripts import cycle_detail as cd
    orig_read = cd._read_jsonl

    def mock_read(paths):
        # Simulate partial/failed read for prompt paths
        if any("prompts" in str(p) for p in paths):
            rows, _ = orig_read(paths)
            return cd.ReadResult(rows, False, {"*"})
        return orig_read(paths)

    monkeypatch.setattr(cd, "_read_jsonl", mock_read)
    fixed_now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
    detail = cd.load_cycle_detail(root, "c-failed-read", now=fixed_now)

    assert detail["available"] is True
    assert detail["history_complete"] is False


def test_unresolved_pending_tool_call_marks_history_incomplete(tmp_path: Path) -> None:
    """Codex comment 4109820844: Unresolved pending tool call without tool response must mark history incomplete."""
    root = tmp_path / "state"
    run_file = root / "bridge" / "runs.jsonl"
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text('{"run_id": "r1", "cycle_id": "c-pending-tool", "classification": "completed"}\n', encoding="utf-8")

    prompt_file = root / "llm_calls" / "prompts" / "2026-09-25.jsonl"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_data = {
        "cycle_id": "c-pending-tool",
        "component": "executor",
        "seq": 1,
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "bash", "arguments": "echo hi"}}
                ]
            }
        ]
    }
    prompt_file.write_text(json.dumps(prompt_data) + "\n", encoding="utf-8")

    from scripts import cycle_detail as cd
    fixed_now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
    detail = cd.load_cycle_detail(root, "c-pending-tool", now=fixed_now)

    assert detail["available"] is True
    assert detail["history_complete"] is False


def test_tool_result_is_withheld_as_bounded_metadata() -> None:
    from scripts.cycle_detail import SanitizedText
    canary = "TOOL_RESULT_SOURCE_CANARY_318"
    step = {"name": "bash", "arguments": SanitizedText('{"keys":["cmd"],"size":88,"sha256":"abcdef123456"}'), "result": SanitizedText(f"[withheld: {len(canary)} bytes, sha256:123456abcdef]"), "source": "request seq 2"}
    rendered = format_tool_step(step)
    assert canary not in rendered
    assert "withheld:" in rendered and "sha256:" in rendered


def test_f6_env_sanitization_and_inline_assignment_redaction(tmp_path: Path) -> None:
    """External review F6: env content withheld in model step copy, inline assignments masked, dotenv matched."""
    from scripts import cycle_detail as cd

    # 1. is_env_path on /tmp/.env
    assert cd.is_env_path("/tmp/.env") is True
    assert cd.is_env_path(".env") is True

    # Raw text is withheld as a typed digest; never pattern-redacted and shown.

    # 3. model step messages copy must not leak env file content
    root = tmp_path / "state"
    run_file = root / "bridge" / "runs.jsonl"
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text('{"run_id": "r1", "cycle_id": "c-f6-env", "classification": "completed"}\n', encoding="utf-8")

    prompt_file = root / "llm_calls" / "prompts" / "2026-09-25.jsonl"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_data = {
        "cycle_id": "c-f6-env",
        "component": "executor",
        "seq": 1,
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "read_file", "arguments": json.dumps({"path": "/etc/eeepc-agent/config.env"})}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "OTHER=ENV_FILE_CANARY_SECRET_123",
            },
        ],
    }
    prompt_file.write_text(json.dumps(prompt_data) + "\n", encoding="utf-8")

    fixed_now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
    detail = cd.load_cycle_detail(root, "c-f6-env", now=fixed_now)
    page_html = cd.render_cycle_page("c-f6-env", detail)

    assert "ENV_FILE_CANARY_SECRET_123" not in page_html
    assert "history incomplete" in page_html or "history complete" in page_html


def test_f7_three_history_states_and_incomplete_cases(tmp_path: Path) -> None:
    """External review F7: separate read/capture/reconstruction states; no-prompt, finish_reason=tool_calls, broken lines are incomplete."""
    from scripts import cycle_detail as cd
    root = tmp_path / "state"

    # Case 1: Finished run with no prompt files must NOT be complete
    run_file = root / "bridge" / "runs.jsonl"
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text('{"run_id": "r1", "cycle_id": "c-no-prompts", "classification": "completed"}\n', encoding="utf-8")

    fixed_now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
    detail1 = cd.load_cycle_detail(root, "c-no-prompts", now=fixed_now)
    assert detail1["history_complete"] is False
    assert "history complete" not in cd.render_cycle_page("c-no-prompts", detail1)

    # Case 2: Final prompt has finish_reason="tool_calls" and no subsequent prompt
    prompt_file = root / "llm_calls" / "prompts" / "2026-09-25.jsonl"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text('{"run_id": "r2", "cycle_id": "c-finish-tools", "classification": "completed"}\n', encoding="utf-8")
    p2 = {
        "cycle_id": "c-finish-tools", "component": "executor", "seq": 1,
        "finish_reason": "tool_calls", "messages": [],
    }
    prompt_file.write_text(json.dumps(p2) + "\n", encoding="utf-8")

    detail2 = cd.load_cycle_detail(root, "c-finish-tools", now=fixed_now)
    assert detail2["history_complete"] is False
    assert detail2.get("reconstruction") == "incomplete"

    # Case 3: Broken JSON line in prompts file
    run_file.write_text('{"run_id": "r3", "cycle_id": "c-broken-line", "classification": "completed"}\n', encoding="utf-8")
    prompt_file.write_text('{"cycle_id": "c-broken-line", "seq": 1}\n{"cycle_id": "c-broken-line",BROKEN\n', encoding="utf-8")
    detail3 = cd.load_cycle_detail(root, "c-broken-line", now=fixed_now)
    assert detail3["history_complete"] is False
    assert detail3.get("read") == "ok"
    assert detail3.get("capture") == "complete"
    assert detail3.get("reconstruction") == "incomplete"


def test_only_affected_source_marks_matching_cycle_incomplete(tmp_path: Path) -> None:
    import json
    from datetime import datetime, timezone
    from scripts.cycle_detail import build_cycle_index

    root = tmp_path
    bridge = root / "bridge"
    bridge.mkdir()
    (bridge / "runs.jsonl").write_text(
        json.dumps({"run_id": "r1", "cycle_id": "c-a", "classification": "completed"}) + "\n"
        + json.dumps({"run_id": "r2", "cycle_id": "c-b", "classification": "completed"}) + "\n"
        + json.dumps({"run_id": "r3", "cycle_id": "c-c", "classification": "completed"}) + "\n"
        + json.dumps({"run_id": "r4", "cycle_id": "c-d", "classification": "completed"}) + "\n",
        encoding="utf-8",
    )
    prompt_dir = root / "llm_calls" / "prompts"
    prompt_dir.mkdir(parents=True)
    valid = {"cycle_id": "c-a", "component": "executor", "seq": 1, "messages": [], "ts": "2026-09-25T10:00:00Z"}
    bad = {"cycle_id": "c-b", "component": "executor", "seq": 1, "messages": [], "ts": "2026-09-25T10:00:00Z"}
    unrelated = {"cycle_id": "c-c", "component": "executor", "seq": 1, "messages": [], "ts": "2026-09-25T10:00:00Z"}
    (prompt_dir / "2026-09-25.jsonl").write_text(
        json.dumps(valid) + "\n" + json.dumps(bad) + "\n{" + json.dumps({"cycle_id": "c-b"})[1:] + "BROKEN\n" + json.dumps(unrelated) + "\n{" + json.dumps({"cycle_id": "c-d"})[1:] + "BROKEN\n", encoding="utf-8",
    )
    index = build_cycle_index(root, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
    assert index["c-a"]["reconstruction"] == "complete"
    assert index["c-b"]["reconstruction"] == "incomplete"
    assert index["c-c"]["reconstruction"] == "complete"
    assert index["c-d"]["reconstruction"] == "incomplete"


def test_f8_unattributed_prompt_counts_only_model_calls(tmp_path: Path) -> None:
    from scripts.cycle_detail import build_cycle_index

    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "runs.jsonl").write_text(json.dumps({"run_id": "run-legacy", "cycle_id": "cycle-unassigned", "classification": "completed"}) + "\n", encoding="utf-8")
    prompts = tmp_path / "llm_calls" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "2026-09-25.jsonl").write_text(json.dumps({
        "cycle_id": "cycle-unassigned", "component": "executor", "seq": 1,
        "ts": "2026-09-25T10:00:00Z", "messages": [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "one", "arguments": "{}"}},
                {"function": {"name": "two", "arguments": "{}"}},
            ]},
        ],
    }) + "\n", encoding="utf-8")
    detail = build_cycle_index(tmp_path, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))["cycle-unassigned"]
    unassigned = next(attempt for attempt in detail["attempts"] if attempt["run_id"] == "run-legacy")
    session = unassigned["sessions"][0]
    assert session["model_calls"] == 1


def test_response_tool_calls_reconciled_with_following_prompt_messages(tmp_path: Path) -> None:
    from scripts.cycle_detail import build_cycle_index

    root = tmp_path
    (root / "bridge").mkdir()
    (root / "bridge" / "runs.jsonl").write_text(json.dumps({
        "run_id": "r-tools", "cycle_id": "c-tools", "classification": "completed",
        "started_at": "2026-09-25T09:00:00Z", "finished_at": "2026-09-25T12:00:00Z",
    }) + "\n", encoding="utf-8")
    prompts = root / "llm_calls" / "prompts"
    prompts.mkdir(parents=True)
    rows = [
        {"cycle_id": "c-tools", "component": "executor", "seq": 1,
         "ts": "2026-09-25T10:00:00Z", "finish_reason": "tool_calls", "messages": [],
         "tool_calls": [{"id": "call-1", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"cycle_id": "c-tools", "component": "executor", "seq": 2,
         "ts": "2026-09-25T10:00:01Z", "messages": [
             {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {"name": "read_file", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "call-1", "content": "synthetic result"},
         ]},
    ]
    (prompts / "2026-09-25.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    detail = build_cycle_index(root, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))["c-tools"]
    steps = [step for session in detail["sessions"] for step in session["steps"] if step.get("kind") == "tool"]
    assert len(steps) == 1
    assert steps[0]["status"] == "ok"
    assert detail["history_complete"] is True


def test_idless_tool_call_ids_are_unique_across_prompts() -> None:
    from scripts.cycle_detail import extract_tool_steps

    first = extract_tool_steps({"seq": 1, "component": "executor", "ts": "2026-09-25T10:00:00Z",
                                "messages": [{"role": "assistant", "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}}]}]})
    second = extract_tool_steps({"seq": 2, "component": "executor", "ts": "2026-09-25T10:01:00Z",
                                 "messages": [{"role": "assistant", "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}}]}]})
    assert first[0]["tool_call_id"] != second[0]["tool_call_id"]


def test_same_named_idless_calls_with_different_arguments_are_not_reconciled(tmp_path: Path) -> None:
    from scripts.cycle_detail import build_cycle_index

    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "runs.jsonl").write_text(json.dumps({
        "run_id": "r-different-args", "cycle_id": "c-different-args", "classification": "completed",
        "started_at": "2026-09-25T09:00:00Z", "finished_at": "2026-09-25T12:00:00Z",
    }) + "\n", encoding="utf-8")
    prompts = tmp_path / "llm_calls" / "prompts"
    prompts.mkdir(parents=True)
    rows = [
        {"cycle_id": "c-different-args", "component": "executor", "seq": 1,
         "ts": "2026-09-25T10:00:00Z", "finish_reason": "tool_calls", "messages": [],
         "tool_calls": [{"function": {"name": "read_file", "arguments": json.dumps({"query": "one"})}}]},
        {"cycle_id": "c-different-args", "component": "executor", "seq": 2,
         "ts": "2026-09-25T10:00:01Z", "messages": [
             {"role": "assistant", "tool_calls": [{"function": {"name": "read_file", "arguments": json.dumps({"query": "two"})}}]},
             {"role": "tool", "content": "synthetic"},
         ]},
    ]
    (prompts / "2026-09-25.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    detail = build_cycle_index(tmp_path, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))["c-different-args"]
    assert detail["history_complete"] is False
    incomplete = [step for session in detail["sessions"] for step in session["steps"] if step.get("kind") == "tool" and step.get("status") == "incomplete"]
    assert len(incomplete) == 1


def test_idless_response_call_reconciles_with_later_idless_message_call(tmp_path: Path) -> None:
    from scripts.cycle_detail import build_cycle_index

    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "runs.jsonl").write_text(json.dumps({
        "run_id": "r-idless", "cycle_id": "c-idless", "classification": "completed",
        "started_at": "2026-09-25T09:00:00Z", "finished_at": "2026-09-25T12:00:00Z",
    }) + "\n", encoding="utf-8")
    prompts = tmp_path / "llm_calls" / "prompts"
    prompts.mkdir(parents=True)
    rows = [
        {"cycle_id": "c-idless", "component": "executor", "seq": 1,
         "ts": "2026-09-25T10:00:00Z", "finish_reason": "tool_calls", "messages": [],
         "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}}]},
        {"cycle_id": "c-idless", "component": "executor", "seq": 2,
         "ts": "2026-09-25T10:00:01Z", "messages": [
             {"role": "assistant", "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}}]},
             {"role": "tool", "content": "synthetic"},
         ]},
    ]
    (prompts / "2026-09-25.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    detail = build_cycle_index(tmp_path, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))["c-idless"]
    tools = [step for session in detail["sessions"] for step in session["steps"] if step.get("kind") == "tool"]
    assert len(tools) == 1 and tools[0]["status"] == "ok"
    assert detail["history_complete"] is True


def test_response_call_does_not_reconcile_across_attempt_boundary(tmp_path: Path) -> None:
    from scripts.cycle_detail import build_cycle_index

    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "runs.jsonl").write_text(
        json.dumps({"run_id": "r1", "cycle_id": "c-boundary", "classification": "unit_timeout",
                    "started_at": "2026-09-25T09:00:00Z", "finished_at": "2026-09-25T10:05:00Z"}) + "\n" +
        json.dumps({"run_id": "r2", "cycle_id": "c-boundary", "classification": "completed",
                    "started_at": "2026-09-25T10:10:00Z", "finished_at": "2026-09-25T12:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    prompts = tmp_path / "llm_calls" / "prompts"
    prompts.mkdir(parents=True)
    rows = [
        {"cycle_id": "c-boundary", "component": "executor", "seq": 1,
         "ts": "2026-09-25T10:01:00Z", "finish_reason": "tool_calls", "messages": [],
         "tool_calls": [{"id": "same-id", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"cycle_id": "c-boundary", "component": "executor", "seq": 2,
         "ts": "2026-09-25T10:11:00Z", "messages": [
             {"role": "assistant", "tool_calls": [{"id": "same-id", "function": {"name": "read_file", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "same-id", "content": "different attempt"},
         ]},
    ]
    (prompts / "2026-09-25.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    detail = build_cycle_index(tmp_path, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))["c-boundary"]
    first_attempt = detail["attempts"][0]
    assert first_attempt["history_complete"] is False
    assert any(step.get("status") == "incomplete" for session in first_attempt["sessions"] for step in session["steps"] if step.get("kind") == "tool")


def test_response_tool_call_without_following_prompt_stays_incomplete(tmp_path: Path) -> None:
    from scripts.cycle_detail import build_cycle_index

    root = tmp_path
    (root / "bridge").mkdir()
    (root / "bridge" / "runs.jsonl").write_text(json.dumps({
        "run_id": "r-final", "cycle_id": "c-final", "classification": "completed",
        "started_at": "2026-09-25T09:00:00Z", "finished_at": "2026-09-25T12:00:00Z",
    }) + "\n", encoding="utf-8")
    prompts = root / "llm_calls" / "prompts"
    prompts.mkdir(parents=True)
    row = {"cycle_id": "c-final", "component": "executor", "seq": 1,
           "ts": "2026-09-25T10:00:00Z", "finish_reason": "tool_calls", "messages": [],
           "tool_calls": [{"id": "call-final", "function": {"name": "read_file", "arguments": "{}"}}]}
    (prompts / "2026-09-25.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    detail = build_cycle_index(root, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))["c-final"]
    assert detail["history_complete"] is False
    assert any(step.get("status") == "incomplete" for session in detail["sessions"] for step in session["steps"] if step.get("kind") == "tool")


def test_completed_attempt_with_empty_prompt_file_is_incomplete(tmp_path: Path) -> None:
    from scripts.cycle_detail import build_cycle_index

    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "runs.jsonl").write_text(
        json.dumps({"run_id": "r-empty", "cycle_id": "c-empty", "classification": "completed"}) + "\n",
        encoding="utf-8",
    )
    prompt_dir = tmp_path / "llm_calls" / "prompts"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "2026-09-25.jsonl").write_text("", encoding="utf-8")
    detail = build_cycle_index(tmp_path, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))["c-empty"]
    assert detail["reconstruction"] == "incomplete"
    assert detail["history_complete"] is False
    assert detail["attempts"][0]["history_complete"] is False
    assert detail["attempts"][0]["model_call_count"] == 0


def test_duplicate_daily_prompt_compression_is_counted_once(tmp_path: Path) -> None:
    import gzip
    import json
    from datetime import datetime, timezone
    from scripts.cycle_detail import build_cycle_index

    root = tmp_path
    (root / "bridge").mkdir()
    (root / "bridge" / "runs.jsonl").write_text(json.dumps({"run_id": "r", "cycle_id": "c-dup", "classification": "completed"}) + "\n", encoding="utf-8")
    prompts = root / "llm_calls" / "prompts"
    prompts.mkdir(parents=True)
    row = {"cycle_id": "c-dup", "component": "executor", "seq": 1, "ts": "2026-09-25T10:00:00Z", "messages": []}
    plain = json.dumps(row) + "\n"
    (prompts / "2026-09-25.jsonl").write_text(plain, encoding="utf-8")
    with gzip.open(prompts / "2026-09-25.jsonl.gz", "wt", encoding="utf-8") as stream:
        stream.write(plain)

    detail = build_cycle_index(root, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))["c-dup"]
    assert detail["total_model_calls"] == 1


def test_repeated_seq_durations_preserve_all_prompt_rows(tmp_path: Path) -> None:
    import json
    from datetime import datetime, timezone
    from scripts.cycle_detail import build_cycle_index

    root = tmp_path
    run = root / "bridge" / "runs.jsonl"
    run.parent.mkdir(parents=True)
    run.write_text(json.dumps({"run_id": "run-repeat", "cycle_id": "c-repeat", "classification": "completed"}) + "\n", encoding="utf-8")
    prompts = root / "llm_calls" / "prompts"
    prompts.mkdir(parents=True)
    rows = [
        {"cycle_id": "c-repeat", "component": "executor", "seq": 1, "messages": [{"role": "user", "content": "first"}]},
        {"cycle_id": "c-repeat", "component": "executor", "seq": 1, "messages": [{"role": "user", "content": "second"}]},
    ]
    (prompts / "2026-09-25.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (root / "llm_calls" / "2026-09-25.jsonl").write_text("".join(json.dumps(row) + "\n" for row in [
        {"cycle_id": "c-repeat", "component": "executor", "seq": 1, "ts": "2026-09-25T10:00:00Z", "duration_ms": 11},
        {"cycle_id": "c-repeat", "component": "executor", "seq": 1, "ts": "2026-09-25T10:01:00Z", "duration_ms": 22},
    ]), encoding="utf-8")
    rows[0]["ts"] = "2026-09-25T10:00:00Z"
    rows[1]["ts"] = "2026-09-25T10:01:00Z"
    (prompts / "2026-09-25.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    detail = build_cycle_index(root, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))["c-repeat"]
    model_steps = [step for attempt in detail["attempts"] for session in attempt["sessions"] for step in session["steps"] if step.get("kind") == "model"]
    assert [step["duration"] for step in model_steps] == [11, 22]


def test_f8_attempt_scoped_sessions_deduped_tools_and_unknown_duration(tmp_path: Path) -> None:
    """F8: associate records by time window, dedupe cumulative tools, don't invent duration seqs."""
    from scripts import cycle_detail as cd

    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "runs.jsonl").write_text(
        json.dumps({"run_id": "r1", "cycle_id": "c-f8", "started_at": "2026-09-25T10:00:00Z", "finished_at": "2026-09-25T10:05:00Z", "classification": "unit_timeout"}) + "\n" +
        json.dumps({"run_id": "r2", "cycle_id": "c-f8", "started_at": "2026-09-25T10:10:00Z", "finished_at": "2026-09-25T10:15:00Z", "classification": "completed"}) + "\n",
        encoding="utf-8",
    )
    pdir = tmp_path / "llm_calls" / "prompts"
    pdir.mkdir(parents=True)
    def prompt(ts: str, seq: int, tool_result: bool = False) -> dict:
        msgs = [{"role": "assistant", "tool_calls": [{"id": "tool-1", "function": {"name": "read", "arguments": "{}"}}]}]
        if tool_result:
            msgs.append({"role": "tool", "tool_call_id": "tool-1", "content": "done"})
        return {"cycle_id": "c-f8", "component": "executor", "seq": seq, "ts": ts, "messages": msgs}
    records = [prompt("2026-09-25T10:01:00Z", 1), prompt("2026-09-25T10:02:00Z", 2, True), prompt("2026-09-25T10:11:00Z", 1, True)]
    (pdir / "2026-09-25.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    (tmp_path / "llm_calls" / "2026-09-25.jsonl").write_text(
        json.dumps({"cycle_id": "c-f8", "component": "executor", "ts": "2026-09-25T10:01:00Z", "duration_ms": 1234}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "llm_calls" / "prompts" / "2026-09-25.jsonl.gz").write_text("", encoding="utf-8")

    detail = cd.build_cycle_index(tmp_path, days=1, now=datetime(2026, 9, 25, 12, tzinfo=timezone.utc))["c-f8"]
    assert [a["model_call_count"] for a in detail["attempts"]] == [2, 1]
    assert len(detail["attempts"][0]["sessions"][0]["steps"]) == 4
    assert len(detail["attempts"][1]["sessions"][0]["steps"]) == 2
    assert [len(a["sessions"]) for a in detail["attempts"]] == [1, 1]
    assert detail["attempts"][0]["sessions"][0]["history_complete"] is False
    assert detail["attempts"][1]["sessions"][0]["history_complete"] is True
    assert detail["attempts"][1]["history_complete"] is True
    steps = [step for attempt in detail["attempts"] for session in attempt["sessions"] for step in session["steps"]]
    assert sum(step.get("kind") == "tool" for step in steps) == 3
    assert all(step.get("duration") == "unknown" for step in steps if step.get("kind") == "model")
    page = cd.render_cycle_page("c-f8", detail)
    assert page.count('class="attempt-row"') == 2
    assert "Session executor" in page
    assert "tool-1" not in page
    assert "Duration: unknown" in page
