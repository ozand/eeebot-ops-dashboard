"""Regression cases for truthful D2 history and deployable reader artifacts."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.cycle_detail import build_cycle_index, render_cycle_page


@pytest.mark.parametrize("with_run,call_id,result_id,complete", [
    (False, None, None, False),
    (True, None, "orphan-private-id", False),
    (True, "expected-private-id", "other-private-id", False),
    (True, "matching-private-id", "matching-private-id", True),
])
def test_history_requires_run_and_tool_provenance(tmp_path, with_run, call_id, result_id, complete):
    if with_run:
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        (bridge / "runs.jsonl").write_text(json.dumps({
            "run_id": "run-one", "cycle_id": "cycle-one", "classification": "completed",
            "started_at": "2026-10-04T09:00:00Z", "finished_at": "2026-10-04T11:00:00Z",
        }) + "\n")
    messages = []
    if call_id:
        messages.append({"role": "assistant", "tool_calls": [{"id": call_id,
            "function": {"name": "read", "arguments": "{}"}}]})
    if result_id:
        messages.append({"role": "tool", "tool_call_id": result_id,
                         "content": "PRIVATE_RESULT_CANARY"})
    prompts = tmp_path / "llm_calls" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "2026-10-04.jsonl").write_text(json.dumps({
        "cycle_id": "cycle-one", "component": "executor", "seq": 1,
        "ts": "2026-10-04T10:00:00Z", "messages": messages, "finish_reason": "stop",
    }) + "\n")
    record = build_cycle_index(tmp_path, now=datetime(2026, 10, 4, 12, tzinfo=timezone.utc))["cycle-one"]
    assert record["history_complete"] is complete
    if not with_run:
        assert record["reconstruction"] == "incomplete"
        assert record["attempts"][0]["history_complete"] is False
    if with_run and result_id and not complete:
        assert record["attempts"][0]["history_complete"] is False
        assert all(session["history_complete"] is False for session in record["sessions"])
        assert any(step.get("status") == "incomplete"
                   for session in record["sessions"] for step in session["steps"])
    page = render_cycle_page("cycle-one", record)
    assert "PRIVATE_RESULT_CANARY" not in page
    assert all(identifier not in page for identifier in (call_id, result_id) if identifier)


@pytest.mark.parametrize("corrupt_row", ['[]', 'null', '"invalid record"', '{BROKEN'])
def test_unattributable_corruption_breaks_all_retained_cycles(tmp_path, corrupt_row):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "runs.jsonl").write_text(json.dumps({
        "run_id": "run-one", "cycle_id": "cycle-one", "classification": "completed",
        "started_at": "2026-10-04T09:00:00Z", "finished_at": "2026-10-04T11:00:00Z",
    }) + "\n")
    prompts = tmp_path / "llm_calls" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "2026-10-04.jsonl").write_text(json.dumps({
        "cycle_id": "cycle-one", "component": "executor", "seq": 1,
        "ts": "2026-10-04T10:00:00Z", "messages": [], "finish_reason": "stop",
    }) + "\n" + corrupt_row + "\n")
    record = build_cycle_index(tmp_path, now=datetime(2026, 10, 4, 12, tzinfo=timezone.utc))["cycle-one"]
    assert record["history_complete"] is False
    assert record["reconstruction"] == "incomplete"


def test_manifest_delivers_private_cycle_reader():
    root = Path(__file__).resolve().parents[1]
    entries = (root / "deploy/sync-manifest.txt").read_text().splitlines()
    assert "scripts/cycle_detail.py" in entries
    assert (root / "scripts/cycle_detail.py").is_file()
