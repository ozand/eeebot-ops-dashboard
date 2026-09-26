"""Synthetic private cycle detail contract tests for ADR-036."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from scripts.cycle_detail import display_text, format_model_step, format_tool_step, load_cycle_detail, mark_incomplete_history, redact_text, render_cycle_page
from scripts.two_sinks import PUBLIC_PAGES, validate_publish_allowlist


def test_attempts_are_rows_with_per_attempt_counts():
    """ADR-036 §4: attempts remain distinct rows with their own counts."""
    page = render_cycle_page("cycle-synthetic", {"attempts": [
        {"run_id": "run-a", "model_call_count": 2, "history_complete": True},
        {"run_id": "run-b", "model_call_count": 1, "history_complete": False},
    ]})
    assert page.count('<article class="attempt-row">') == 2
    assert "Attempt run-a" in page and "Model calls: 2" in page
    assert "Attempt run-b" in page and "Model calls: 1" in page


def test_model_and_tool_steps_render_readably():
    """ADR-036 §4: D2 formatting primitives keep model/tool labels and no tool tokens."""
    model = {"kind": "model", "messages": "synthetic ask", "answer": "synthetic answer", "tools": "search(query: demo)", "reasoning": "collapsed", "tokens": 12, "duration": "2s"}
    tool = {"kind": "tool", "name": "search", "arguments": "query: demo", "result": "synthetic result", "status": "ok", "tokens": None}
    rendered = format_model_step(model) + format_tool_step(tool)
    assert "synthetic ask" in rendered and "synthetic answer" in rendered
    assert "Tools:" in rendered and "collapsed" in rendered and "Tokens: 12" in rendered
    assert "search(query: demo)" in rendered and "synthetic result" in rendered
    assert "Tokens" not in format_tool_step(tool)
    assert "duration: unknown" in format_tool_step(tool)


def test_redaction_covers_ghu_tokens_in_private_details() -> None:
    token = "ghu_" + "SYNTHETIC_CANARY_123456789"
    assert token not in redact_text(token)


def test_redaction_covers_extended_private_key_blocks() -> None:
    private_block = "-----BEGIN ENCRYPTED PRIVATE KEY-----\n" + "SYNTHETIC_PRIVATE_KEY_CANARY\n" + "-----END ENCRYPTED PRIVATE KEY-----"
    safe = redact_text(private_block)
    assert "SYNTHETIC_PRIVATE_KEY_CANARY" not in safe
    assert "[redacted: private-key]" in safe


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
    assert "litellm.env" in safe  # Safe filename reference remains visible


def test_tool_arguments_with_env_path_are_withheld() -> None:
    step = {"name": "read_file", "arguments": json.dumps({"path": "/tmp/.env"}), "result": "not shown"}
    rendered = format_tool_step(step)
    assert "/tmp/.env" not in rendered
    assert "[env file contents withheld]" in rendered


def test_env_file_content_in_tool_arguments_is_withheld_everywhere() -> None:
    from scripts import cycle_detail as cd

    canary = "OTHER=ENV_ARGUMENT_CANARY_8221"
    messages = [{"role": "assistant", "tool_calls": [{"id": "read-env", "function": {
        "name": "write_file", "arguments": json.dumps({"path": "/tmp/.env", "content": canary}),
    }}]}]
    cleaned = cd.sanitize_messages(messages)
    assert canary not in json.dumps(cleaned)
    rendered = cd.format_tool_step({"name": "write_file", "arguments": messages[0]["tool_calls"][0]["function"]["arguments"], "result": "ok"})
    assert canary not in rendered


def test_structured_tool_message_content_is_recursively_sanitized() -> None:
    from scripts.cycle_detail import sanitize_messages

    secret = "NESTED_SECRET_CANARY_6512"
    messages = [{"role": "tool", "content": {"nested": {"password": secret}, "items": ["ordinary value"]}}]
    safe = json.dumps(sanitize_messages(messages))
    assert secret not in safe
    assert "[redacted]" in safe
    assert "ordinary value" in safe


def test_env_withholding_does_not_corrupt_structured_non_env_arguments() -> None:
    from scripts.cycle_detail import sanitize_messages

    message = {"role": "assistant", "tool_calls": [{"id": "ordinary", "function": {
        "name": "read_file", "arguments": json.dumps({"path": "/tmp/data.json"}),
    }}]}
    sanitized = sanitize_messages([message])
    args = json.loads(sanitized[0]["tool_calls"][0]["function"]["arguments"])
    assert args == {"path": "/tmp/data.json"}


def test_tool_step_reading_env_file_withholds_content():
    """ADR-036 Decision 1 (a): reading /etc/eeepc-agent/*.env withholds contents unconditionally."""
    dummy_secret = "CANARY_SECRET_VAL_7711"
    step = {
        "kind": "tool",
        "name": "read_file",
        "arguments": json.dumps({"path": "/etc/eeepc-agent/test.env"}),
        "result": f"SECRET_KEY={dummy_secret}\nOTHER=123",
        "status": "ok",
    }
    rendered = format_tool_step(step)
    assert dummy_secret not in rendered
    assert "[env file contents withheld]" in rendered


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


def test_mismatched_tool_response_id_does_not_complete_pending_call() -> None:
    from scripts.cycle_detail import extract_tool_steps

    steps = extract_tool_steps({"seq": 1, "messages": [
        {"role": "assistant", "tool_calls": [{"id": "expected", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "different", "content": "response"},
    ]})
    assert len(steps) == 1
    assert steps[0]["status"] == "pending"


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
    assert "pattern" in step["arguments"]
    assert step["result"] == "search result output"
    assert step["source"] == "reconstructed from request seq 4"
    assert step["duration"] is None
    assert step["tokens"] is None


def test_truncation_is_marked_and_display_only():
    """ADR-036 §4: display truncation is labelled and leaves source unchanged."""
    original = "synthetic " * 20
    shown = display_text(original, limit=20)
    assert "characters not shown" in shown
    assert len(original) > len(shown)
    assert original == "synthetic " * 20


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
    assert result["sessions"][0]["role"] == "planner"
    assert result["sessions"][0]["steps"][0]["duration"] == 18
    assert result["total_model_calls"] == 1


def test_existing_cycle_detail_links_are_rendered_privately(tmp_path: Path) -> None:
    from scripts.two_sinks import build_private_cycle_pages

    pages = build_private_cycle_pages({"lineage": '<a href="cycle.html?id=cycle-linked">details</a>'}, tmp_path)
    assert "cycles/cycle-linked.html" in pages
    assert "cycle-linked" in pages["cycles/cycle-linked.html"]


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
    from scripts.two_sinks import build_private_cycle_pages, split_render_inputs

    public, private = split_render_inputs({"reflections": [{
        "cycle_id": "cycle-reflection-host", "summary": "some summary",
        "findings": [{"kind": "wasted_steps", "detail": "finding"}],
        "recommendations": [{"kind": "good_practice", "detail": "recommendation"}],
    }]})
    private["cycle_details"] = build_cycle_details([], None, None, public.get("reflections"))
    pages = build_private_cycle_pages(private, tmp_path)
    page = pages["cycles/cycle-reflection-host.html"]
    assert "Summary chars: 12" in page
    assert "Findings count: 1" in page
    assert "Recommendations count: 1" in page
    assert "some summary" not in page


def test_manual_cycle_page_links_are_safely_encoded_and_bounded(tmp_path: Path) -> None:
    from scripts.two_sinks import build_private_cycle_pages

    pages = build_private_cycle_pages({"lineage": '<a href="cycle.html?id=../../escape">details</a>'}, tmp_path)
    assert "cycles/../../escape.html" not in pages
    assert all(Path(key).name == key.split("/", 1)[1] for key in pages if key.startswith("cycles/"))


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
    assert roles == {"planner", "executor"}

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
    assert any(step.get("status") == "incomplete" for s in c3["sessions"] for step in s["steps"])


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
    monkeypatch.setattr(ap.tv, "publish_to_pages", lambda pages, **_: (published_to_gh.append(pages) or 0, {}))

    args = ap.parse_args(["--state-root", str(root), "--state-dir", str(state_dir), "--site-root", str(site_root)])
    rc = ap.run(args)
    assert rc == 0

    assert (site_root / "current" / "cycles" / "c-end-to-end.html").is_file()
    assert (site_root / "current" / "index.html").is_file()

    assert len(published_to_gh) == 1
    gh_pages = published_to_gh[0]
    assert "cycles/c-end-to-end.html" not in gh_pages
    assert "index.html" in gh_pages


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
            return rows, False
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


def test_tool_result_applies_display_limit_truncation() -> None:
    """Codex comment 4109820846: Tool results exceeding display limit must be truncated with omitted char count."""
    long_result = "output line\n" * 500  # ~6000 chars > DEFAULT_DISPLAY_LIMIT 4000
    step = {
        "name": "bash",
        "arguments": "git status",
        "result": long_result,
        "source": "reconstructed",
    }
    rendered = format_tool_step(step)
    assert "characters not shown" in rendered


def test_f6_env_sanitization_and_inline_assignment_redaction(tmp_path: Path) -> None:
    """External review F6: env content withheld in model step copy, inline assignments masked, dotenv matched."""
    from scripts import cycle_detail as cd

    # 1. is_env_path on /tmp/.env
    assert cd.is_env_path("/tmp/.env") is True
    assert cd.is_env_path(".env") is True

    # 2. inline assignment redaction
    redacted = cd.redact_text("command output: GH_TOKEN=abcdefghijklmnop")
    assert "abcdefghijklmnop" not in redacted
    assert "GH_TOKEN=[redacted]" in redacted

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
    assert "[env file contents withheld]" in page_html


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
    prompt_file.write_text('{"cycle_id": "c-broken-line", "seq": 1}\n{not-valid-json\n', encoding="utf-8")
    detail3 = cd.load_cycle_detail(root, "c-broken-line", now=fixed_now)
    assert detail3["history_complete"] is False
    assert detail3.get("read") == "ok"
    assert detail3.get("capture") == "complete"
    assert detail3.get("reconstruction") == "incomplete"


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
    unassigned = next(attempt for attempt in detail["attempts"] if attempt["run_id"] == "unassigned")
    session = unassigned["sessions"][0]
    assert session["model_calls"] == 1


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
    assert len(detail["attempts"][0]["sessions"][0]["steps"]) == 3
    assert len(detail["attempts"][1]["sessions"][0]["steps"]) == 1
    assert [len(a["sessions"]) for a in detail["attempts"]] == [1, 1]
    assert detail["attempts"][0]["sessions"][0]["history_complete"] is False
    assert detail["attempts"][1]["sessions"][0]["history_complete"] is True
    assert detail["attempts"][1]["history_complete"] is True
    steps = [step for attempt in detail["attempts"] for session in attempt["sessions"] for step in session["steps"]]
    assert sum(step.get("kind") == "tool" for step in steps) == 1
    assert all(step.get("duration") == "unknown" for step in steps if step.get("kind") == "model")
    page = cd.render_cycle_page("c-f8", detail)
    assert page.count('class="attempt-row"') == 2
    assert "Session executor" in page
    assert page.count("tool-1") <= 1
    assert "Duration: unknown" in page
