"""Synthetic private cycle detail contract tests for ADR-036."""
from __future__ import annotations

from scripts.cycle_detail import display_text, format_model_step, format_tool_step, load_cycle_detail, mark_incomplete_history, redact_text, render_cycle_page
from scripts.two_sinks import PUBLIC_PAGES, validate_publish_allowlist


def test_attempts_are_rows_with_per_attempt_counts():
    """ADR-036 §4: attempts remain distinct rows with their own counts."""
    page = render_cycle_page("cycle-synthetic", {"attempts": [
        {"run_id": "run-a", "model_call_count": 2, "history_complete": True},
        {"run_id": "run-b", "model_call_count": 1, "history_complete": False},
    ]})
    assert page.count("<article>") == 2
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
    """ADR-036 §5: redact secrets and env contents; safe filename labels remain renderable."""
    safe = redact_text("api_key=abc Bearer abc token=xyz sk-abcdefghijklmnop", env_contents=("PRIVATE_ENV_VALUE",))
    safe = redact_text(safe + " PRIVATE_ENV_VALUE", env_contents=("PRIVATE_ENV_VALUE",))
    assert "abc" not in safe and "xyz" not in safe and "sk-abcdefghijklmnop" not in safe
    assert "[redacted: credential]" in safe and "[redacted: bearer]" in safe
    assert "PRIVATE_ENV_VALUE" not in safe
    assert "litellm.env" in "source: litellm.env"


def test_incomplete_history_is_marked():
    """ADR-036 §4: incomplete records are explicit, never represented as no tools."""
    marked = mark_incomplete_history({"history_complete": False, "tool_steps": []})
    assert marked["history_state"] == "history incomplete"
    assert "history incomplete" in render_cycle_page("c", {"attempts": [marked]})


def test_reconstructed_steps_have_source_and_unknown_duration():
    """ADR-036 §4: reconstructed steps identify source and unknown duration."""
    step = {"source": "reconstructed from request seq 4", "duration": None}
    assert step["source"].startswith("reconstructed from request seq ")
    assert step["duration"] is None


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
    assert result["attempts"][0]["sessions"][0]["steps"][0]["duration"] == 18
    assert result["compaction_count"] == 1
    assert result["attempts"][0]["history_complete"] is False
    assert "unavailable association" in result["source_notes"]["runs_without_cycle_id"]


def test_missing_cycle_sources_render_unavailable_not_empty():

    """ADR-036 §3: missing private source data is visibly unavailable."""
    assert "unavailable" in render_cycle_page("c", None)
    assert "unavailable" in render_cycle_page("c", {"available": False})
