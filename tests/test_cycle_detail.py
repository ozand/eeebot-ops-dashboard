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
    except ValueError as exc:
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
    assert result["total_model_calls"] == 1


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
