from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import agent_context as ac
from scripts import techtree_viewer as tv
from scripts.agent_context import read_agent_context_dict
from scripts.two_sinks import split_render_inputs


@pytest.mark.parametrize("width", [390, 1440])
def test_owned_agent_content_is_escaped_visible_and_script_safe(tmp_path: Path, width: int) -> None:
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    state, repo = tmp_path / "state", tmp_path / "repo"
    (state / "ledger").mkdir(parents=True)
    (state / "prompts").mkdir()
    (state / "public").mkdir()
    (state / "public/derived_view.json").write_text(json.dumps({"status": "present", "charter": {"source": "release_goals_md", "merged": False, "text": "Release charter visible marker"}, "derived_status": "absent"}), encoding="utf-8")
    (repo / "skills" / "demo").mkdir(parents=True)
    (repo / "memory").mkdir()
    (repo / "lessons").mkdir()
    (state / "ledger/cycles.jsonl").write_text(json.dumps({"phase": "system_prompt", "cycle_id": "c1", "chars": 5, "cap": 24000, "sections": {"identity": 5}}) + "\n", encoding="utf-8")
    (state / "prompts/c1.system.txt").write_text("<img src=x onerror=window.__ownedXss=1>", encoding="utf-8")
    (state / "prompts/c1.task.txt").write_text("task own marker", encoding="utf-8")
    (repo / "skills/demo/SKILL.md").write_text("# Demo\n\n<script>window.__ownedXss=2</script>skill own marker", encoding="utf-8")
    (repo / "memory/MEMORY.md").write_text("<svg onload=window.__ownedXss=3>memory own marker", encoding="utf-8")
    (repo / "lessons/LESS-20261110-own.md").write_text("## Root Causes\nproblem own marker\n## Solution\nsolution own marker\n", encoding="utf-8")
    (repo / "lessons/index.md").write_text("# Lessons", encoding="utf-8")
    (repo / "AGENTS.md").write_text("Read own context marker", encoding="utf-8")
    source = tv.read_local_state(str(state), str(repo))
    source["agent_context"] = read_agent_context_dict(state, repo)
    public, _ = split_render_inputs(source)
    pages = tv.render_public_pages(public, "synthetic")
    index_path = tmp_path / "index.html"
    index_path.write_text(pages["index.html"], encoding="utf-8")
    page_path = tmp_path / "agent.html"
    page_path.write_text(pages["agent.html"], encoding="utf-8")
    evidence = tmp_path / "browser-evidence"
    evidence.mkdir()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 1000})
        page.goto(index_path.as_uri())
        page.wait_for_load_state("load")
        assert page.get_by_text("Release charter visible marker").count() == 1
        page.goto(page_path.as_uri())
        page.wait_for_load_state("load")
        if ac._MEMORY_FD_WALK_SUPPORTED:
            page.get_by_text("View SKILL.md").first.click()
            page.get_by_text("View memory/MEMORY.md").click()
            page.locator(".memory-file-entry").screenshot(path=str(evidence / f"owned-content-{width}.png"))
        else:
            assert "skill reads unavailable" in page.locator("#panel-agent").inner_text().lower()
            assert page.get_by_text("memory own marker").count() == 0
        page.locator(".context-block-details").first.evaluate("el => el.open = true")
        page.screenshot(path=str(evidence / f"owned-context-detail-{width}.png"), full_page=True)
        if ac._MEMORY_FD_WALK_SUPPORTED:
            assert page.get_by_text("task own marker").count() == 1
        else:
            assert page.get_by_text("task own marker").count() == 0
        assert page.locator(".agent-owned-context").count() >= 1
        assert page.locator("script").filter(has_text="window.__ownedXss=2").count() == 0
        assert page.locator("img[src=x]").count() == 0
        assert page.evaluate("window.__ownedXss || 0") == 0
        if ac._MEMORY_FD_WALK_SUPPORTED:
            assert "&lt;img" in page.content()
        else:
            assert "&lt;img" not in page.content()
        if ac._MEMORY_FD_WALK_SUPPORTED:
            assert page.get_by_text("memory own marker").count() == 1
        else:
            assert page.get_by_text("memory own marker").count() == 0
        if ac._MEMORY_FD_WALK_SUPPORTED:
            assert page.get_by_text("skill own marker").count() == 1
        else:
            assert page.get_by_text("skill own marker").count() == 0
        browser.close()
