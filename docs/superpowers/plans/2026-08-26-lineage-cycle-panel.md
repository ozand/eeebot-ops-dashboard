# Lineage Cycle Details Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every lineage node clickable and expose its bounded cycle record, lesson, reflection, gate, file, and deep-link data in a self-contained panel.

**Architecture:** Extend the existing fail-soft collector with reflector JSONL data and build one bounded per-cycle details map during page rendering. Embed that map as JSON in `lineage.html`; add data attributes and a small inline script that controls one responsive panel, selected-node styling, Escape/close behavior, and `#node-<cycle_id>` restoration. Preserve the current SVG geometry, colors, and hover titles.

**Tech Stack:** Python 3.11 stdlib, existing HTML/SVG renderer, inline CSS/JavaScript, pytest.

---

### Task 1: Add reflector data to the collector

**Files:**
- Modify: `scripts/techtree_viewer.py` collector constants, embedded remote reader, local mirror, and `read_local_state()` assembly
- Test: `tests/test_techtree_viewer.py`

- [ ] **Step 1: Write the failing test** for a reflection JSONL source being returned and joined by cycle ID, including fail-soft malformed lines.
- [ ] **Step 2: Run the focused test** with `python -m pytest tests/test_techtree_viewer.py -q`; confirm failure because the `reflections` key/source is absent.
- [ ] **Step 3: Implement the minimal reader** in both the remote script and local mirror. Read `state/reflector/reflections.jsonl`, parse each JSON object independently, ignore malformed lines, and expose `reflections` as a list.
- [ ] **Step 4: Run the focused test** and confirm it passes.
- [ ] **Step 5: Commit** with `git add scripts/techtree_viewer.py tests/test_techtree_viewer.py && git commit -m "feat(lineage): collect reflector summaries"`.

### Task 2: Build bounded cycle detail records and title fallback

**Files:**
- Modify: `scripts/techtree_viewer.py` near `build_archive_tree()` and page rendering
- Test: `tests/test_techtree_viewer.py`

- [ ] **Step 1: Write failing tests** asserting detail records include ledger fields, lesson insight, reflection summary/findings/recommendations, capped files/text, and title precedence: ledger title, commit subject, first file, then `(untitled cycle)`.
- [ ] **Step 2: Run the focused tests** and confirm failure because no detail payload builder exists and current integrated nodes still use `no title`.
- [ ] **Step 3: Implement `build_cycle_details(...)`** as one small pure helper. Index source rows by `cycle_id`; merge the latest/most complete values; cap `files_changed` to 20 and long text fields to 500 characters; return JSON-safe records keyed by cycle ID.
- [ ] **Step 4: Pass the resolved title into lineage node rendering** and use `(untitled cycle)` only as the explicit final fallback.
- [ ] **Step 5: Run the focused tests** and confirm they pass.
- [ ] **Step 6: Commit** with `git add scripts/techtree_viewer.py tests/test_techtree_viewer.py && git commit -m "feat(lineage): assemble bounded cycle details"`.

### Task 3: Add the panel markup, interactions, and URL state

**Files:**
- Modify: `scripts/techtree_viewer.py` lineage SVG generation, page CSS/script assembly
- Test: `tests/test_techtree_viewer.py`

- [ ] **Step 1: Write failing tests** for `data-cycle-id`, embedded JSON, panel fields/links, selected class, Escape/close handlers, hash parsing, and absence of external URLs.
- [ ] **Step 2: Run the focused tests** and confirm failure because the panel and event handlers are not present.
- [ ] **Step 3: Implement one fixed responsive panel** adjacent to the canvas, with a close button and sections that render only when data exists. Add inline JS that swaps content, sets `location.hash`, restores `#node-...`, scrolls the selected node into view, and toggles a visible selected ring. Keep native `<title>` nodes.
- [ ] **Step 4: Run the focused tests** and confirm they pass.
- [ ] **Step 5: Commit** with `git add scripts/techtree_viewer.py tests/test_techtree_viewer.py && git commit -m "feat(lineage): add clickable cycle details panel"`.

### Task 4: Full verification and delivery

**Files:**
- Modify: only files already listed above unless a test fixture requires a small change

- [ ] **Step 1: Run the full suite** with `python -m pytest -q`; expected result is zero failures.
- [ ] **Step 2: Run Python 3.11 compile checks** for both scripts and compile the embedded remote reader.
- [ ] **Step 3: Inspect the diff and page-size test output**; verify the generated page remains below 1 MB and no external network request was introduced.
- [ ] **Step 4: Commit any final test-only corrections**, push `feat/issue-85-lineage-panel`, and create a PR fixing #85.
- [ ] **Step 5: Merge the PR, pull on `eeepc-lan`, run the existing installer, reset publish state if needed, and start the publish service.**
- [ ] **Step 6: Fetch the published `lineage.html` with a cache-buster and verify a non-zero count of detail records/nodes, panel field markers, deep links, hash handlers, and page size under 1 MB. Quote one real node's panel data from the published artifact.
- [ ] **Step 7: Post the delivery comment on issue #85 before cleanup**, including before/after no-title counts, tests, live URL evidence, non-zero detail volume, and one node's quoted panel content.
- [ ] **Step 8: Move the project item to Done, close the issue, and remove the worktree and feature branch only after the comment is confirmed.
