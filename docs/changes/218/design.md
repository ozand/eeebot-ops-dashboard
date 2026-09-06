# Design: Unified Lineage Graph with Date-Projection Filtering (#218)

- **Status**: Design Gate Approved with Invariant Constraints
- **Target Issue**: #218 (fixes #214 as verified consequence)
- **ADR**: `docs/adr/ADR-001-unified-lineage-date-projection.md`

---

## 1. Technical Invariants & Principles

1. **One Unified DAG**: The entire lineage payload is a single directed acyclic graph (`nodes[]`, `edges[]`) spanning up to `LINEAGE_MAX_NODES = 1500` nodes.
2. **Canonical Immutability**: Canonical nodes and edges are computed during generation and are strictly immutable under filtering. Filtering never mutates, adds, or recomputes parents.
3. **Parent Resolution Invariants**:
   - Resolved across the full read ledger history (`LEDGER_HISTORY_DAYS = 90`) **before** applying the emitted cap.
   - Recorded parent has absolute priority; if unresolvable, it stays `parent_known: false` (boundary). A recorded parent is **never replaced by a chronological guess**.
   - Missing source is explicitly rendered as a boundary, never as a "proven repository root".
   - Inferred edges link to latest prior trunk node **only** when no parent was recorded at all, carrying explicit `basis: 'inferred'`.
   - Rollback events are distinct forward cycles; history is not rewritten.
4. **Stable Node Identity**:
   - Git commits: `c:<sha>`.
   - Non-commit attempts: `a:<cycle_id>[:<disambiguator>]`.
   - `sha` is nullable (`str | null`); non-commits never receive fake commit SHAs.
   - DOM ID and URL hash use injective mapping: `nodeIdToDomId(id) = "node-" + encodeURIComponent(id).replace(/%/g, "_")`.
   - Legacy alias `#node-<cycle_id>` deterministically resolves to the node with the latest `ts` (tie-break by `node_id`). UI indicates `"Node 1 of N for cycle-X"`.
   - Unknown deep-link renders explicit notice: `"Node <id> not found in loaded history (coverage: <from> to <to>)"`.
5. **Branching Preservation & Basis Fidelity**:
   - Collapsing of out-of-window ancestors is strictly restricted to **maximal non-branching chains**.
   - If an out-of-window ancestor is a common ancestor for multiple visible nodes, it is drawn as a **context junction node** (`.lineage-context-node`).
   - Mixed recorded + inferred collapsed paths are marked `data-basis="mixed"`, never `recorded`.
6. **Strict Filter Bounds**:
   - `24h`: strict UTC rolling interval `[now - 24h, now]`, capturing `now` once per action.
   - `Today`: `[today 00:00:00.000Z, now]`.
   - `Yesterday+Today`: `[yesterday 00:00:00.000Z, now]` with label `Yesterday+Today (UTC calendar)`.
   - `Range`: `[from 00:00:00.000Z, to + 1 day 00:00:00.000Z)`. Invalid `from > to` shows inline error note without corrupting graph.
   - Deep-link outside active window automatically switches filter button to `All`.
   - Invalid or missing `ts` excluded from time windows; visible only under `All` with `data-ts-status="invalid"`.

---

## 2. Server-Side Generator Changes (`scripts/techtree_viewer.py`)

1. **Reader & Deduplication**:
   - `read_ledger_history()` reads up to `LEDGER_HISTORY_DAYS = 90` days.
   - Raw ledger rows are deduplicated to unique candidate nodes (`c:<sha>` or `a:<cycle_id>`).
2. **Canonical Resolution**:
   - Parent relationships are resolved across all unique candidate nodes before applying retention cap.
3. **Retention Budget**:
   - Retain the newest `LINEAGE_MAX_NODES = 1500` nodes in timestamp order.
   - Current HEAD / `current_sha` is unconditionally retained (swapping out oldest node if necessary).
   - Emit `coverage` dictionary in JSON:
     ```json
     "coverage": {
       "raw_read_rows": 4820,
       "unique_candidate_nodes": 1820,
       "emitted_nodes": 1500,
       "truncated": true,
       "truncated_before_ts": "2026-08-23T14:00:00Z",
       "from_ts": "2026-08-23T14:00:00Z",
       "to_ts": "2026-09-05T22:00:00Z"
     }
     ```
4. **Embedded Payload & Initial SVG**:
   - Emit single unified `<svg id="lineage-svg" class="lineage-unified-dag">` replacing the day-grouped sections.
   - Embed canonical graph in `<script id="lineage-data" type="application/json">`.
   - Render server-side default SVG (for `yesterday-today` or full default) to guarantee `<noscript>` accessibility.

---

## 3. Client-Side Projection Engine (`assets/vendor/lineage-renderer.js` / inline JS)

1. **Filter Evaluation**:
   - Computes active interval `[min_ts, max_ts]`.
   - Tags each node as visible (`true`) or out-of-window (`false`).
   - If deep-link target is out-of-window, sets active filter to `All` and updates UI buttons.
2. **Graph Projection**:
   - Identifies visible nodes.
   - Identifies context junction nodes (out-of-window nodes having ≥2 visible descendants with disjoint paths).
   - Collapses linear out-of-window chains between visible/junction endpoints.
   - Emits projected edges with attributes:
     - `data-edge-type`: `canonical`, `context`, or `collapsed`.
     - `data-basis`: `recorded`, `inferred`, or `mixed`.
     - `data-source`, `data-target`.
     - For collapsed: `data-collapsed-nodes`, `data-collapsed-edges`, `data-path`.
3. **Layout & Coordinate Calculation**:
   - Reuses layered DAG / forest slot algorithm:
     - Depth calculation with `visited` set (cycle protection).
     - Trunk nodes assigned to primary horizontal/vertical slots.
     - Leaf/attempt nodes placed in side lanes.
   - Sets dynamic SVG `viewBox` and dimensions.
4. **Interaction & State Preservation**:
   - Preserves selection highlight, ESC key handling, focus retention, and instant panel scrolling from #212/#213/#215.
   - Synchronizes URL hash via `replaceState`.

---

## 4. Comprehensive Test Plan

Tests in `tests/test_issue208_lineage.py` & `tests/test_techtree_viewer.py`:

1. **Exact 24h Boundaries**:
   - `now - 24h - 1ms`: node is excluded from `24h` filter.
   - `now - 24h + 1ms`: node is included in `24h` filter.
2. **Empty Window**:
   - Range filter matching 0 nodes renders empty-state message cleanly without JavaScript error.
3. **Cross-Day Edges & Junctions**:
   - Continuous edge crossing UTC midnight without stubs.
   - Out-of-window common ancestor branching to two visible descendants on different days renders as context junction node.
4. **Mixed Path Basis**:
   - Chain with 1 recorded and 1 inferred link collapses to `data-basis="mixed"`.
5. **Unknown Parent & Cycle Guard**:
   - Unknown recorded parent renders boundary stub; does not guess chronologically.
   - Graph with cyclic parent references handles gracefully via visited guard.
6. **Invalid Timestamps**:
   - Node with null/unparseable timestamp excluded from `Today`/`24h`, included in `All` with `data-ts-status="invalid"`.
7. **Identity & Duplicate Cycles**:
   - Cycle with 2 commit nodes and 1 non-commit leaf: unique DOM IDs for each; `#node-<cycle_id>` alias selects latest node; UI shows "Node 1 of 3".
8. **Deep Linking**:
   - Deep link to out-of-window node switches filter to `All`, highlights node, opens card.
   - Unknown deep link displays explicit non-modal missing node notice.
9. **Visual & Responsive Verification**:
   - Verified on mobile viewports (320px, 390px) and desktop.
   - Full test suite passes without regressions.
