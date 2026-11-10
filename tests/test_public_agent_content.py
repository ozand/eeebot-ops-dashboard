from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import techtree_autopublish as ap
from scripts import techtree_viewer as tv
from scripts.two_sinks import split_render_inputs


def _fixtures(state: Path, repo: Path, secret: str = "") -> dict:
    (state / "prompts").mkdir(parents=True, exist_ok=True)
    (state / "prompts/c1.system.txt").write_text("request: " + secret + " prompt", encoding="utf-8")
    (state / "prompts/c1.task.txt").write_text("task: prompt task", encoding="utf-8")
    (state / "ledger").mkdir(parents=True, exist_ok=True)
    (state / "ledger/cycles.jsonl").write_text(json.dumps({"phase": "system_prompt", "cycle_id": "c1", "chars": 20, "cap": 24000, "sections": {"identity": 20}}) + "\n", encoding="utf-8")
    (state / "subagents").mkdir(exist_ok=True)
    (state / "subagents/sa.json").write_text(json.dumps({"cycle_id": "c1", "subagent_id": "sa", "status": "done", "task": "subagent task", "summary": "subagent result", "result": "subagent response", "origin": {"channel": "system", "chat_id": "self-evolving-agent"}, "workspace": str(repo), "runtime_state_root": str(state)}), encoding="utf-8")
    (state / "subagents/legacy.json").write_text(json.dumps({"cycle_id": "c1", "subagent_id": "legacy", "status": "done", "task": "legacy own task", "summary": "legacy own summary", "result": "legacy own result"}), encoding="utf-8")
    (state / "subagents/foreign.json").write_text(json.dumps({"cycle_id": "c1", "subagent_id": "foreign", "status": "done", "task": "foreign origin task", "summary": "foreign origin summary", "result": "foreign origin response", "origin": {"channel": "telegram", "chat_id": "unrelated-chat"}, "workspace": str(repo), "runtime_state_root": str(state)}), encoding="utf-8")
    (state / "subagents/foreign-workspace.json").write_text(json.dumps({"cycle_id": "c1", "subagent_id": "foreign-workspace", "status": "done", "task": "foreign workspace task", "summary": "foreign workspace summary", "result": "foreign workspace response", "origin": {"channel": "system", "chat_id": "self-evolving-agent"}, "workspace": str(repo.parent / "other-project"), "runtime_state_root": str(state)}), encoding="utf-8")
    (state / "subagents/foreign-root.json").write_text(json.dumps({"cycle_id": "c1", "subagent_id": "foreign-root", "status": "done", "task": "foreign root task", "summary": "foreign root summary", "result": "foreign root response", "origin": {"channel": "system", "chat_id": "self-evolving-agent"}, "workspace": str(repo), "runtime_state_root": str(state.parent / "other-state").replace("\\\\", "/")}), encoding="utf-8")
    (state / "public").mkdir(exist_ok=True)
    (state / "public/derived_view.json").write_text(json.dumps({"status": "present", "charter": {"source": "release_goals_md", "merged": False, "text": "Release charter quote"}}), encoding="utf-8")
    (state / "goals").mkdir(exist_ok=True)
    (state / "goals/goal_text.json").write_text(json.dumps({"text": "raw operator goal must stay private"}), encoding="utf-8")
    (state / "reflector").mkdir(exist_ok=True)
    (state / "reflector/reflections.jsonl").write_text(json.dumps({"cycle_id": "c1", "summary": "reflection response", "findings": ["own finding"]}) + "\n", encoding="utf-8")
    (repo / "skills/demo").mkdir(parents=True, exist_ok=True)
    (repo / "skills/demo/SKILL.md").write_text("# Demo\n\nSkill response body", encoding="utf-8")
    (repo / "memory").mkdir(exist_ok=True)
    (repo / "memory/MEMORY.md").write_text("Own memory context", encoding="utf-8")
    (repo / "lessons").mkdir(exist_ok=True)
    (repo / "lessons/LESS-20261110-own.md").write_text("# Lesson\n\n## Root Causes\n\nown issue\n\n## Solution\n\nown fix\n", encoding="utf-8")
    (repo / "lessons/index.md").write_text("# Lessons\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Agent instructions\nRead your own context.", encoding="utf-8")
    return tv.read_local_state(str(state), str(repo))


def test_owned_content_real_reader_to_public_projection_render_scan_and_dry_run(tmp_path, monkeypatch, capsys):
    state, repo, publisher_state = tmp_path / "state", tmp_path / "repo", tmp_path / "publisher-state"
    state.mkdir(); repo.mkdir(); publisher_state.mkdir()
    source = _fixtures(state, repo)
    assert "Skill response body" in source["agent_context"]["tier2_skills"][0]["content"]
    assert "Own memory context" in source["agent_context"]["tier2_memory"]["files"][0]["content"]
    public, _private = split_render_inputs(source)
    pages = tv.render_public_pages(public, "synthetic")
    from scripts.publish_scan import scan_pages
    scan_pages(pages)
    combined = "".join(pages.values())
    for marker in ("prompt", "prompt task", "subagent task", "subagent result", "subagent response", "legacy own task", "legacy own summary", "legacy own result", "Release charter quote", "reflection response", "own finding", "Skill response body", "Own memory context", "own issue", "own fix", "Read your own context"):
        assert marker in combined
    assert "foreign origin" not in combined
    assert "foreign workspace" not in combined
    assert "foreign root" not in combined
    assert len(source["subagent_records"]) == 2
    public_record = public["subagent_records"][0]
    assert not {"origin", "parent_context", "workspace", "runtime_state_root", "runtime_state_source"} & public_record.keys()
    assert "raw operator goal must stay private" not in combined
    assert "Release charter quote" in pages["index.html"]
    endpoints = []
    original_gh = tv._gh
    captured = {}
    monkeypatch.setattr(tv, "_gh", lambda args, input_text=None: endpoints.append(args) or (_ for _ in ()).throw(AssertionError("dry run called gh")))
    monkeypatch.setattr(ap, "compute_tree_digest", lambda _root: "fixture-digest")
    original_render = ap.tv.render_public_pages
    monkeypatch.setattr(ap.tv, "render_public_pages", lambda data, host_label: captured.update(pages=original_render(data, host_label)) or captured["pages"])
    args = ap.parse_args(["--state-root", str(state), "--state-dir", str(publisher_state), "--instance-repo", str(repo), "--site-root", str(tmp_path / "site"), "--dry-run"])
    assert ap.run(args) == 0
    assert endpoints == []
    assert "Skill response body" in "".join(captured["pages"].values())
    assert "Own memory context" in "".join(captured["pages"].values())
    assert "Release charter quote" in captured["pages"]["index.html"]
    from scripts.publish_scan import scan_pages
    scan_pages(captured["pages"])
    assert "dry-run" in capsys.readouterr().out
    monkeypatch.setattr(tv, "_gh", original_gh)


def test_embedded_reader_applies_same_subagent_source_gate(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir(); repo.mkdir()
    _fixtures(state, repo)
    script = tv.REMOTE_READER_SCRIPT.replace(
        'STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"', f'STATE_ROOT = {str(state)!r}',
    ).replace(
        'INSTANCE_REPO = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"', f'INSTANCE_REPO = {str(repo)!r}',
    )
    result = subprocess.run([sys.executable], input=script, capture_output=True, text=True, check=True, timeout=30)
    remote = json.loads(result.stdout)
    records = remote["subagent_records"]
    assert {row["subagent_id"] for row in records} == {"sa", "legacy"}
    assert "subagent task" in json.dumps(records) and "legacy own task" in json.dumps(records)
    assert all(not {"origin", "workspace", "runtime_state_root"} & row.keys() for row in records)


def test_wrong_shape_tier2_content_drops_but_safe_sibling_survives():
    from scripts.two_sinks import _sanitize_public_value
    projected = _sanitize_public_value("agent_context", {
        "tier2_skills": [
            {"name": "safe", "size_bytes": 10, "content": "eligible own body", "desc": "eligible description", "path": "skills/safe/SKILL.md"},
            {"name": "bad", "size_bytes": 5, "content": ["not text"], "desc": {"wrong": "shape"}, "path": "untrusted/path"},
        ],
        "tier2_memory": {"index_status": "present", "corpus_status": "present", "total_files": 1, "total_size_bytes": 5,
                         "files": [{"name": "memory/MEMORY.md", "size_bytes": 5, "content": ["not text"], "path": "secret/path"}]},
    })
    assert projected["tier2_skills"][0]["content"] == "eligible own body"
    assert projected["tier2_skills"][1].get("content") is None
    assert projected["tier2_skills"][1].get("desc") is None
    assert projected["tier2_memory"]["files"][0].get("content") is None
    encoded = json.dumps(projected)
    assert "path" not in encoded and "not text" not in encoded and "eligible own body" in encoded


def test_owned_prompt_with_nested_bearer_credential_is_rejected(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir(); repo.mkdir()
    source = _fixtures(state, repo, "Authorization: Bearer mytoken1234567890abcdef")
    public, _ = split_render_inputs(source)
    pages = tv.render_public_pages(public, "synthetic")
    from scripts.publish_scan import PublicationScanError, scan_pages
    with pytest.raises(PublicationScanError, match="bearer_token"):
        scan_pages(pages)
