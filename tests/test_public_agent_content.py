from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import agent_context as ac
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
    (state / "subagents/null-origin.json").write_text(json.dumps({"cycle_id": "c1", "subagent_id": "null-origin", "task": "null origin must not publish", "origin": None}), encoding="utf-8")
    (state / "subagents/null-parent-origin.json").write_text(json.dumps({"cycle_id": "c1", "subagent_id": "null-parent-origin", "task": "null parent origin must not publish", "parent_context": {"origin": None}}), encoding="utf-8")
    (state / "subagents/foreign-parent.json").write_text(json.dumps({"cycle_id": "c1", "subagent_id": "foreign-parent", "task": "foreign parent origin must not publish", "origin": {"channel": "system", "chat_id": "self-evolving-agent"}, "parent_context": {"origin": {"channel": "telegram", "chat_id": "other-chat"}}}), encoding="utf-8")
    (state / "subagents/null-parent-conflict.json").write_text(json.dumps({"cycle_id": "c1", "subagent_id": "null-parent-conflict", "task": "null parent conflict must not publish", "origin": {"channel": "system", "chat_id": "self-evolving-agent"}, "parent_context": {"origin": None}}), encoding="utf-8")
    (state / "subagents/own-parent.json").write_text(json.dumps({"cycle_id": "c1", "subagent_id": "own-parent", "task": "both origins own" , "origin": {"channel": "system", "chat_id": "self-evolving-agent"}, "parent_context": {"origin": {"channel": "system", "chat_id": "self-evolving-agent"}}}), encoding="utf-8")
    (state / "subagents/legacy-parent.json").write_text(json.dumps({"cycle_id": "c1", "subagent_id": "legacy-parent", "task": "legacy absent parent origin", "parent_context": {}}), encoding="utf-8")
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
    if source["agent_context"]["tier2_skills_status"] == "present":
        assert "Skill response body" in source["agent_context"]["tier2_skills"][0]["content"]
    else:
        assert source["agent_context"]["tier2_skills_status"] == "unavailable"
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert "Own memory context" in source["agent_context"]["tier2_memory"]["files"][0]["content"]
    else:
        assert source["agent_context"]["tier2_memory"]["corpus_status"] == "unavailable"
    public, _private = split_render_inputs(source)
    pages = tv.render_public_pages(public, "synthetic")
    from scripts.publish_scan import scan_pages
    scan_pages(pages)
    combined = "".join(pages.values())
    markers = ["prompt", "prompt task", "subagent task", "subagent result", "subagent response", "legacy own task", "legacy own summary", "legacy own result", "legacy absent parent origin", "both origins own", "Release charter quote", "reflection response", "own finding", "own issue", "own fix", "Read your own context"]
    if source["agent_context"]["tier2_skills_status"] == "present":
        markers.append("Skill response body")
    if ac._MEMORY_FD_WALK_SUPPORTED:
        markers.append("Own memory context")
    if not ac._MEMORY_FD_WALK_SUPPORTED:
        markers = [marker for marker in markers if marker not in {"prompt task", "own issue", "own fix", "Read your own context"}]
    for marker in markers:
        assert marker in combined
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert "own issue" in combined and "own fix" in combined
    else:
        assert "own issue" not in combined and "own fix" not in combined
    assert "foreign origin" not in combined
    assert "null origin must not publish" not in combined
    assert "null parent origin must not publish" not in combined
    assert "foreign parent origin must not publish" not in combined
    assert "null parent conflict must not publish" not in combined
    assert "null-origin" not in json.dumps(source["subagent_records"])
    assert "null-parent-origin" not in json.dumps(source["subagent_records"])
    assert "foreign workspace" not in combined
    assert "foreign root" not in combined
    assert len(source["subagent_records"]) == 4
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
    if source["agent_context"]["tier2_skills_status"] == "present":
        assert "Skill response body" in "".join(captured["pages"].values())
    else:
        assert "Skill response body" not in "".join(captured["pages"].values())
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert "Own memory context" in "".join(captured["pages"].values())
    else:
        assert "Own memory context" not in "".join(captured["pages"].values())
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert "prompt task" in "".join(captured["pages"].values())
    else:
        assert "prompt task" not in "".join(captured["pages"].values())
    assert "Release charter quote" in captured["pages"]["index.html"]
    from scripts.publish_scan import scan_pages
    scan_pages(captured["pages"])
    assert "dry-run" in capsys.readouterr().out
    monkeypatch.setattr(tv, "_gh", original_gh)


def test_cycle_detail_browser_displays_bounded_subagent_text(tmp_path):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    source = {
        "ledger_tail": [{"phase": "outcome", "cycle_id": "cycle-synthetic", "outcome": "success"}],
        "subagent_records": [{
            "cycle_id": "cycle-synthetic", "subagent_id": "sa-synthetic", "status": "done",
            "label": "synthetic worker", "task": "TASK_VISIBLE_SYNTHETIC",
            "summary": "SUMMARY_VISIBLE_SYNTHETIC", "result": "RESULT_VISIBLE_SYNTHETIC",
        }],
    }
    public, _ = split_render_inputs(source)
    pages = tv.render_public_pages(public, "synthetic")
    for name, contents in pages.items():
        (tmp_path / name).write_text(contents, encoding="utf-8")

    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/cycle.html?id=cycle-synthetic")
            page.wait_for_selector(".cycle-detail-subagent", timeout=5000)
            body = page.locator("#cycle-detail-body")
            for text in ("TASK_VISIBLE_SYNTHETIC", "SUMMARY_VISIBLE_SYNTHETIC", "RESULT_VISIBLE_SYNTHETIC"):
                assert text in body.inner_text()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


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
    assert {row["subagent_id"] for row in records} == {"sa", "legacy", "legacy-parent", "own-parent"}
    assert "subagent task" in json.dumps(records) and "legacy own task" in json.dumps(records)
    assert "null-origin" not in json.dumps(records) and "null-parent-origin" not in json.dumps(records)
    assert "foreign-parent" not in json.dumps(records) and "null-parent-conflict" not in json.dumps(records)
    assert all(not {"origin", "workspace", "runtime_state_root"} & row.keys() for row in records)


def test_instance_text_readers_reject_external_agents_and_lesson_links(tmp_path):
    if not ac._MEMORY_FD_WALK_SUPPORTED:
        pytest.skip("fd-relative O_NOFOLLOW instance reads are unavailable")
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir(); repo.mkdir()
    _fixtures(state, repo)
    external_agents = tmp_path / "external-agents.md"
    external_agents.write_text("SYNTHETIC_EXTERNAL_AGENTS_SENTINEL", encoding="utf-8")
    external_lesson = tmp_path / "external-lesson.md"
    external_lesson.write_text("## Solution\\n\\nSYNTHETIC_EXTERNAL_LESSON_SENTINEL", encoding="utf-8")
    (repo / "AGENTS.md").unlink()
    (repo / "AGENTS.md").symlink_to(external_agents)
    (repo / "lessons/LESS-20261110-own.md").unlink()
    (repo / "lessons/LESS-20261110-own.md").symlink_to(external_lesson)
    source = tv.read_local_state(str(state), str(repo))
    encoded = json.dumps(source)
    assert "SYNTHETIC_EXTERNAL_AGENTS_SENTINEL" not in encoded
    assert "SYNTHETIC_EXTERNAL_LESSON_SENTINEL" not in encoded
    assert source["agents_md"] is None
    assert source["lessons"] == []

    script = tv.REMOTE_READER_SCRIPT.replace(
        'STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"', f'STATE_ROOT = {str(state)!r}',
    ).replace(
        'INSTANCE_REPO = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"', f'INSTANCE_REPO = {str(repo)!r}',
    )
    result = subprocess.run([sys.executable], input=script, capture_output=True, text=True, check=True, timeout=30)
    embedded = json.loads(result.stdout)
    assert embedded["agents_md"] is None
    assert embedded["lessons"] == []
    assert "SYNTHETIC_EXTERNAL_AGENTS_SENTINEL" not in result.stdout
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert "SYNTHETIC_EXTERNAL_LESSON_SENTINEL" not in result.stdout


def test_instance_state_prompt_readers_reject_external_symlinks(tmp_path):
    if not ac._MEMORY_FD_WALK_SUPPORTED:
        pytest.skip("fd-relative O_NOFOLLOW instance reads are unavailable")
    state, repo = tmp_path / "state", tmp_path / "repo"
    (state / "prompts").mkdir(parents=True)
    repo.mkdir()
    external = tmp_path / "outside-prompt.txt"
    external.write_text("SYNTHETIC_EXTERNAL_PROMPT_SENTINEL", encoding="utf-8")
    (state / "prompts/c1.system.txt").symlink_to(external)
    (state / "prompts/c1.task.txt").symlink_to(external)
    (state / "prompts/c2.system.txt").write_text("SYNTHETIC_OWN_PROMPT_POSITIVE", encoding="utf-8")
    (state / "prompts/c2.task.txt").write_text("SYNTHETIC_OWN_TASK_POSITIVE", encoding="utf-8")
    (state / "ledger").mkdir()
    (state / "ledger/cycles.jsonl").write_text(json.dumps({"phase": "system_prompt", "cycle_id": "c1", "chars": 5, "cap": 20}) + "\n", encoding="utf-8")
    source = ac.read_agent_context_dict(state, repo)
    assert source["prompt_text"] is None
    assert source["task_text"] is None
    assert ac.compute_truncation_streak([{"phase": "system_prompt", "cycle_id": "c1"}])["total_rows"] == 1
    assert ac.read_instance_file_text(state, state / "prompts/c2.system.txt", 100)[1] == "SYNTHETIC_OWN_PROMPT_POSITIVE"
    local_prompts = tv.read_local_state(str(state), str(repo))["cycle_prompts"]
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert "c1" not in local_prompts
        assert local_prompts["c2"]["system"]["text"] == "SYNTHETIC_OWN_PROMPT_POSITIVE"
    else:
        assert local_prompts == {}

    script = tv.REMOTE_READER_SCRIPT.replace(
        'STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"', f'STATE_ROOT = {str(state)!r}',
    ).replace(
        'INSTANCE_REPO = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"', f'INSTANCE_REPO = {str(repo)!r}',
    )
    result = subprocess.run([sys.executable], input=script, capture_output=True, text=True, check=True, timeout=30)
    embedded = json.loads(result.stdout)
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert embedded["agent_context"]["prompt_text"] is None
        assert "SYNTHETIC_EXTERNAL_PROMPT_SENTINEL" not in result.stdout
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert embedded["cycle_prompts"]["c2"]["system"]["text"] == "SYNTHETIC_OWN_PROMPT_POSITIVE"
        assert embedded["cycle_prompts"]["c2"]["task"]["text"] == "SYNTHETIC_OWN_TASK_POSITIVE"
    else:
        assert embedded["cycle_prompts"] == {}

    external_dir = tmp_path / "external-prompts"
    external_dir.mkdir()
    (external_dir / "c3.task.txt").write_text("SYNTHETIC_EXTERNAL_PROMPT_DIR_SENTINEL", encoding="utf-8")
    state2 = tmp_path / "state2"
    state2.mkdir()
    try:
        (state2 / "prompts").symlink_to(external_dir, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        if sys.platform == "win32":
            pytest.skip(f"directory symlink creation unavailable: {type(exc).__name__}")
        raise
    external_script = tv.REMOTE_READER_SCRIPT.replace(
        'STATE_ROOT = "/var/lib/eeepc-agent/self-evolving-agent/state"', f'STATE_ROOT = {str(state2)!r}',
    ).replace(
        'INSTANCE_REPO = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"', f'INSTANCE_REPO = {str(repo)!r}',
    )
    external_result = subprocess.run([sys.executable], input=external_script, capture_output=True, text=True, check=True, timeout=30)
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert "SYNTHETIC_EXTERNAL_PROMPT_DIR_SENTINEL" not in external_result.stdout


def test_instance_lesson_archive_reads_bounded_gzip_without_following_links(tmp_path):
    if not ac._MEMORY_FD_WALK_SUPPORTED:
        pytest.skip("fd-relative O_NOFOLLOW instance reads are unavailable")
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir(); repo.mkdir()
    lessons = repo / "lessons"
    (lessons / "archive").mkdir(parents=True)
    import gzip
    archive_body = b"lessons:\n- id: OWN-ARCHIVE\n  result: OWN_ARCHIVE_POSITIVE\n"
    (lessons / "archive/lessons-2026-11-10.yaml.gz").write_bytes(gzip.compress(archive_body))
    external = tmp_path / "external.yaml.gz"
    external.write_bytes(gzip.compress(b"lessons:\n- id: EXT-ARCHIVE\n  result: SYNTHETIC_EXTERNAL_ARCHIVE_SENTINEL\n"))
    (lessons / "archive/lessons-2026-11-09.yaml.gz").symlink_to(external)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tv, "INSTANCE_REPO", str(repo))
    try:
        monkeypatch.setattr(tv.os, "listdir", lambda path: ["lessons-2026-11-10.yaml.gz", "lessons-2026-11-09.yaml.gz"] if str(path).endswith("archive") else [])
        script = tv.REMOTE_READER_SCRIPT.replace(
            'INSTANCE_REPO = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"', f'INSTANCE_REPO = {str(repo)!r}',
        )
        namespace = {}
        exec(script, namespace)
        rows = namespace["_read_lessons_archive"](str(lessons))
        assert any(row.get("id") == "OWN-ARCHIVE" and row.get("result") == "OWN_ARCHIVE_POSITIVE" for row in rows)
        assert not any(row.get("id") == "EXT-ARCHIVE" for row in rows)
        local_source = tv.read_local_state(str(state), str(repo))
        local_rows = local_source["lessons"]
        assert any(row.get("id") == "OWN-ARCHIVE" and row.get("result") == "OWN_ARCHIVE_POSITIVE" for row in local_rows)
        assert not any(row.get("id") == "EXT-ARCHIVE" for row in local_rows)
    finally:
        monkeypatch.undo()


def test_instance_readers_reject_escaping_skill_symlink(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir(); repo.mkdir()
    external = tmp_path / "external-skill.md"
    external.write_text("SYNTHETIC_EXTERNAL_SKILL_SENTINEL", encoding="utf-8")
    (repo / "skills/demo").mkdir(parents=True)
    try:
        (repo / "skills/demo/SKILL.md").symlink_to(external)
    except (OSError, NotImplementedError) as exc:
        if sys.platform == "win32":
            pytest.skip(f"symlink creation unavailable: {type(exc).__name__}")
        raise
    source = tv.read_local_state(str(state), str(repo))
    assert "SYNTHETIC_EXTERNAL_SKILL_SENTINEL" not in json.dumps(source)
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert source["agent_context"]["tier2_skills"] == []
    else:
        assert source["agent_context"]["tier2_skills_status"] == "unavailable"


def test_memory_reader_rejects_external_symlink_and_keeps_internal_memory(tmp_path):
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir(); repo.mkdir()
    external = tmp_path / "external-private.md"
    external.write_text("SYNTHETIC_EXTERNAL_PRIVATE_SENTINEL", encoding="utf-8")
    (repo / "memory").mkdir()
    (repo / "memory/inside.md").write_text("SYNTHETIC_INTERNAL_MEMORY", encoding="utf-8")
    try:
        (repo / "memory/outside.md").symlink_to(external)
    except (OSError, NotImplementedError) as exc:
        if sys.platform == "win32":
            pytest.skip(f"symlink creation unavailable: {type(exc).__name__}")
        raise

    source = tv.read_local_state(str(state), str(repo))
    memory = source["agent_context"]["tier2_memory"]
    names = {row["name"] for row in memory["files"]}
    contents = json.dumps(memory)
    assert "SYNTHETIC_EXTERNAL_PRIVATE_SENTINEL" not in contents
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert "memory/outside.md" not in names
        assert "memory/inside.md" in names
        assert "SYNTHETIC_INTERNAL_MEMORY" in contents
    else:
        assert memory["corpus_status"] == "unavailable"


def test_memory_reader_rejects_ancestor_replacement_before_open(tmp_path, monkeypatch):
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir(); repo.mkdir()
    memory = repo / "memory"
    memory.mkdir()
    inside = memory / "inside.md"
    inside.write_text("SYNTHETIC_INSIDE_TARGET", encoding="utf-8")
    external = tmp_path / "external-private.md"
    external.write_text("SYNTHETIC_RACE_OUTSIDE_SENTINEL", encoding="utf-8")
    ancestor = memory / "ancestor"
    ancestor.mkdir()
    ancestor_file = ancestor / "race.md"
    ancestor_file.write_text("SYNTHETIC_ANCESTOR_OWNED_TARGET", encoding="utf-8")

    import scripts.agent_context as agent_context

    original_open = agent_context.os.open
    swapped = False

    def open_then_swap(path, *args, **kwargs):
        nonlocal swapped
        dir_fd = kwargs.get("dir_fd")
        if dir_fd is not None and path == "ancestor" and not swapped:
            swapped = True
            ancestor.rename(memory / "detached")
            (memory / "ancestor").symlink_to(tmp_path / "outside")
        return original_open(path, *args, **kwargs)

    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/race.md").write_text("SYNTHETIC_ANCESTOR_OUTSIDE_SENTINEL", encoding="utf-8")
    monkeypatch.setattr(agent_context.os, "open", open_then_swap)
    source = tv.read_local_state(str(state), str(repo))
    rows = source["agent_context"]["tier2_memory"]["files"]
    if ac._MEMORY_FD_WALK_SUPPORTED:
        assert swapped
        assert any(row["content"] == "SYNTHETIC_INSIDE_TARGET" for row in rows)
        assert not any(row["content"] == "SYNTHETIC_ANCESTOR_OWNED_TARGET" for row in rows)
        assert not any(row["content"] == "SYNTHETIC_ANCESTOR_OUTSIDE_SENTINEL" for row in rows)
    else:
        assert not swapped
        assert source["agent_context"]["tier2_memory"]["corpus_status"] == "unavailable"
        assert not rows


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
    if not ac._MEMORY_FD_WALK_SUPPORTED:
        pytest.skip("secret scan of owner-prompt reader requires POSIX no-follow read support")
    with pytest.raises(PublicationScanError, match="bearer_token"):
        scan_pages(pages)
