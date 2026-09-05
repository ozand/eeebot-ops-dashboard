# #218 Design Gate: Unified Lineage Graph with Date Projection

Status: **Proposed — awaiting operator/reviewer GO before implementation**

Canonical ADR: [`docs/adr/ADR-001-unified-lineage-date-projection.md`](../../adr/ADR-001-unified-lineage-date-projection.md)

## Rules and source material read

- `README.md` — confirms `eeebot-ops-dashboard` is the canonical dashboard repo and `scripts/techtree_viewer.py` is the standalone, stdlib-only, self-contained static lineage/tech-tree page with lazy details JSON.
- `docs/testing.md` — confirms CI/local baselines and known Windows-only failures.
- `T:/Code/eeebot/REPO_GITHUB_WORKFLOW_RULES.md` — confirms issue/branch/PR isolation rules and that dashboard work belongs in this repo.
- `docs/superpowers/plans/2026-08-26-lineage-cycle-panel.md` — prior lineage details-panel plan; #213 supersedes/preserves its hash/card/focus contract.
- `scripts/techtree_viewer.py` and `assets/vendor/lineage-renderer.js` — current implementation evidence.

## Current implementation facts

Current lineage rendering is day-bucketed:

1. `scripts/techtree_viewer.py::_build_vertical_day_lineage()` groups `evolution_tree` rows by `_lineage_day(ts)`.
2. It emits one `<section class="lineage-day-group" data-day="YYYY-MM-DD">` per day.
3. Each section contains an independent `<svg data-lineage-renderer>` and a per-day JSON script.
4. `assets/vendor/lineage-renderer.js::attachFilter()` hides/shows whole `.lineage-day-group` sections.
5. `_lineage_parent()` intentionally returns `(None, None, parent_day)` when a recorded parent exists in `all_nodes` but has a different day. The renderer then displays `↩ from <day>` instead of a real graph edge.

Therefore, calendar days are currently graph boundaries. This violates the #218 requirement.

## Target contract

### 1. Canonical Graph Data Model & Stable Node Identities

The generator emits one unified lineage payload covering the full available source history (bounded by retention caps):

```jsonc
{
  "coverage": {
    "from_ts": "2026-08-23T00:00:00Z",
    "to_ts": "2026-09-05T23:00:00Z",
    "raw_node_count": 1420,
    "emitted_node_count": 1250,
    "truncated": false,
    "truncated_before_ts": null,
    "truncated_count": 0
  },
  "nodes": [
    {
      "node_id": "c:abcdef1234567890",
      "sha": "abcdef1234567890",
      "cycle_id": "cycle-20260905-01",
      "ts": "2026-09-05T20:15:00Z",
      "outcome": "integrated",
      "title": "Fix memory leak in collector"
    },
    {
      "node_id": "a:cycle-20260905-02",
      "sha": null,
      "cycle_id": "cycle-20260905-02",
      "ts": "2026-09-05T20:30:00Z",
      "outcome": "failed",
      "title": "Attempt refactor without commit"
    }
  ],
  "edges": [
    {
      "source": "c:abcdef1234567890",
      "target": "a:cycle-20260905-02",
      "basis": "recorded",
      "source_available": true
    }
  ]
}
```

Rules:
- **`node_id` uniqueness**: globally unique string across the entire payload.
  - Commit-bearing node: `c:<sha>`
  - Attempt/leaf without commit: `a:<cycle_id>[:<disambiguator>]`
- **Nullable `sha`**: `sha` is `str | None`. Non-commit attempts have `sha: null`. We never invent synthetic commit SHAs (e.g. `leaf:cid`).
- **Canonical edges**: reference `source` and `target` by `node_id`.
- **Payload immutability**: client-side filtering never alters the canonical `nodes[]` or `edges[]`.
- **DOM element ID**: strictly unique `id="node-" + safe_id(node_id)` on each SVG node element (e.g. `id="node-c-abcdef12"`).
- **Deep links & Legacy Alias**:
  - Precise hash: `#node-<node_id>` selects that exact node.
  - Legacy alias `#node-<cycle_id>`: if multiple nodes share `cycle_id` (e.g. 2 commits or 1 commit + 1 failed attempt), deterministically selects the one with the latest `ts` (tie-break by `node_id`). The UI card/details explicitly indicates `"Node 1 of N for <cycle_id>"` with links to toggle between sibling nodes of that cycle.

### 2. Projection Semantics & Test Immutability

Projection is a dynamic SVG view over the canonical graph:

- Filter controls:
  - `All`: full emitted graph.
  - `Today`: UTC calendar day matching viewer clock `[today 00:00:00Z, tomorrow 00:00:00Z)`.
  - `24h`: strict timestamp window `[now - 24h, now]` with `now` captured once at click time.
  - `Yesterday+Today (UTC calendar)`: calendar-day mode `[yesterday 00:00:00Z, tomorrow 00:00:00Z)`.
  - Custom date range: `[from_date 00:00:00Z, to_date + 1 day 00:00:00Z)` (half-open, preventing sub-millisecond loss).
  - Validation: `from > to` displays inline error `"Invalid date range: 'from' must be before or equal to 'to'"` without clearing graph.
- Deep link outside active window: automatically switches the UI filter controls to `All` (visually marking `All` active), renders full graph, scrolls target into view, and opens details card.
- Empty range: renders explicit empty-state message `"No cycles recorded in selected interval"`, never falling back to old graph.
- **Test Contract Distinction**:
  - `payload.edges` is strictly immutable under any filter change.
  - Projected SVG edges are derived dynamically: each displayed path maps to an underlying canonical subpath, carrying `data-source`, `data-target`, `data-edge-type`, and provenance attributes.

### 3. Branching Preservation & Contextual Ancestor Collapsing

- **Maximal Non-branching Chains**: out-of-window ancestor nodes are collapsed if and only if they form a strictly linear path (each intermediate node has in-degree == 1 and out-degree == 1, with no other visible descendants).
- **Junction Preservation**: if a hidden ancestor `J` is a common ancestor of two or more distinct visible branches, `J` MUST NOT be collapsed into an edge. `J` is rendered as a distinct **context junction node** (`.lineage-context-node`, `data-context="junction"`).
- **Collapsed Path Marker**: linear collapsed chains are rendered as a single context edge with:
  - Visible label counting hidden **NODES**: `"N hidden nodes"` / `"N ancestors"`.
  - DOM provenance attributes: `data-edge-type="collapsed"`, `data-collapsed-nodes="N"`, `data-collapsed-edges="M"`, `data-path="id1->id2->..."`.
  - Mixed basis: if the collapsed chain contains both `recorded` and `inferred` links, its basis is composite `data-basis="mixed"` and is NEVER labelled `recorded`.
  - Interactive expansion: clicking the collapsed path marker expands that linear segment or switches filter to `All`.

### 4. Coverage, Retention Budget & Caps

- **Tier 1 (Source read window)**: `LEDGER_HISTORY_DAYS` (default 30 days) read by generator.
- **Tier 2 (Emitted graph cap)**:
  - Target budget: `LINEAGE_MAX_NODES = 1500` nodes (or newest 14 calendar days).
  - Deterministic retention: newest events up to cap; trunk HEAD / current SHA is always anchored and retained.
  - The payload includes an explicit `coverage` block:
    `{ "from_ts": "...", "to_ts": "...", "raw_node_count": N, "emitted_node_count": M, "truncated": bool, "truncated_before_ts": "...", "truncated_count": K }`
  - Missing ancestors outside emitted cap have `source_available: false, source_boundary: "truncated_history"`. They are explicitly labelled as history truncation, NOT as proven root commits.
- **Tier 3 (UI filter projection)**: client-side filter over the emitted canonical graph.

### 5. Defensive Graph Traversal

- `visited: set[str]` guard on all ancestor/descendant graph walks to prevent infinite recursion on malformed/cyclic parent references. Cycles are flagged with `cycle_detected: true`.
- Timestamps: malformed or unparseable timestamps are parsed to `ts: null`, tagged with `data-ts-status="invalid"`, and rendered safely at boundary slots rather than throwing unhandled exceptions.

### 6. Preservation of Prior Increments

- **#212**: outcome classes (`integrated`, `skipped`, `partial`, `failed`), edge dash patterns, mobile wrap legend (320px/390px).
- **#213**: cycle details card, keyboard navigation (Tab/Enter/Space), close button (`×`), Escape key, focus return to node, `history.replaceState` hash updates.
- **#215**: `gate_violations` array preservation from ledger to JSON to card.

## Layout approach

First attempt: **reuse/extend** the existing dependency-free forest layout.

Rationale:
- Standalone, zero-dependency, static HTML page.
- Each node has one canonical parent link in the forest model; branches/junctions and leaf attempts are positioned in horizontal lanes by timestamp.
- Context junction nodes and collapsed edges fit naturally into the lane/column layout without a third-party DAG engine.

## Test plan & Fixtures

1. `test_issue218_canonical_edges_immutable_under_filter`:
   Verify that switching between `All`, `24h`, `Today`, and custom range leaves `payload.edges` identical in length, source, target, and basis.
2. `test_issue218_unique_dom_ids_multiple_shas_one_cycle`:
   Fixture with 2 commits and 1 leaf sharing `cycle-X`. Asserts all 3 SVG node elements have unique DOM ids, exact edges connect to the right nodes, and `#node-cycle-X` selects the latest commit while showing `"Node 1 of 3"`.
3. `test_issue218_junction_ancestor_preserved_not_collapsed`:
   Fixture with hidden common ancestor `A` having two visible children `B` and `C` on different days. Asserts `A` is rendered as a `.lineage-context-node` junction, NOT collapsed into a false direct edge between `B` and `C`.
4. `test_issue218_mixed_basis_collapsed_chain`:
   Fixture with a hidden chain `D -> E -> F` containing one `recorded` and one `inferred` edge. Asserts the collapsed edge has `data-basis="mixed"`, `data-collapsed-nodes="1"`, `data-collapsed-edges="2"`, and is not marked `recorded`.
5. `test_issue218_strict_24h_timestamp_filtering`:
   Fixture with node at `now - 23h 59m` (visible) and node at `now - 24h 01m` (hidden or context only).
6. `test_issue218_deep_link_auto_all`:
   Playwright test: opening `#node-<id>` where the node is outside the default window automatically switches filter UI to `All`, centers node, and opens card.
7. `test_issue218_invalid_date_range_shows_warning`:
   Setting `from > to` renders inline warning without crashing or emptying graph.
8. `test_issue218_retains_212_213_215_behaviors`:
   Full suite of legend wrapping, details card opening/closing, focus return, and `gate_violations` assertions continue to pass.
