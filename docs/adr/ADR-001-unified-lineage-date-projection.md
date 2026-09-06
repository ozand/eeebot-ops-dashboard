# ADR-001: Unified Lineage Graph with Date-Projection Filtering

- **Status**: Accepted (Design Gate Approved with Invariant Constraints, Issue #218)
- **Date**: 2026-09-05
- **Deciders**: @ozand, @reviewer (subagent-chat-01a071f4-ea48-77ea)
- **Consulted**: Issue #214, Issue #218, PR #222

---

## Context

`lineage.html` previously partitioned cycles into independent daily SVG trees (`LINEAGE_DAYS=14` calendar days, each day capped at `LINEAGE_DAY_CAP=120` trunk and 120 side nodes). Each day rendered as an isolated SVG with its own layout and coordinate system:
1. Cross-day edges were drawn as hanging stubs pointing at the boundary.
2. Filter buttons (`Today`, `Yesterday+Today`, `24h`, custom range) manipulated day-container visibility (`display: none`), so "24h" actually showed 1 or 2 calendar days rather than a true rolling 24-hour UTC window.
3. Edges across day boundaries were not connected, breaking lineage continuity.

Issue #218 mandates replacing this multi-tree architecture with a **single unified DAG** across the available history, with client-side interactive date filtering acting as a **visibility projection** over that graph.

---

## Decision Drivers

1. **Continuity**: The lineage graph must represent the true DAG of evolution cycles and commits across time without artificial day-boundary cuts.
2. **True Rolling 24h**: The `24h` filter must filter nodes strictly by `[now - 24h, now]`, resolving Issue #214 properly.
3. **Immutability of History**: Filtering must never invent, delete, recompute, or mutate recorded graph edges.
4. **Preservation of Branching**: Intermediate collapsed ancestors must preserve fork/junction structure and basis fidelity (`recorded` vs `inferred` vs `mixed`).
5. **Traceability**: Stable, reversible node identities and deterministic URL hash routing.
6. **Robustness**: Bounded DOM budget, defensive guards against cycles, malformed timestamps, and missing source data.

---

## Decision

### 1. Data Model & Node Identity

Every node in the graph has a globally unique, deterministic, injective identifier:
- **Git Commit Node**: `c:<sha>` (where `<sha>` is the exact commit SHA).
- **Non-Commit / Attempt Node**: `a:<cycle_id>[:<disambiguator>]` (e.g. `a:cycle-ecbe56903f35`).
- **SHA Nullability**: The `sha` attribute is `str | null`. A failed or non-committing cycle never receives a fabricated commit SHA (the legacy `leaf:<cid>` convention is retired).
- **Injective DOM & Hash Mapping**:
  - A single bidirectional function maps `node_id` to DOM ID and URL hash:
    ```javascript
    function nodeIdToDomId(nodeId) {
      return "node-" + encodeURIComponent(nodeId).replace(/%/g, "_");
    }
    function domIdToNodeId(domId) {
      if (!domId.startsWith("node-")) return null;
      return decodeURIComponent(domId.slice(5).replace(/_/g, "%"));
    }
    ```
  - This avoids collisions between hyphenated names and colon namespaces.
- **Deep-Linking & Legacy Alias**:
  - Direct exact link: `#node-<encoded_node_id>` selects that exact node.
  - Legacy alias `#node-<cycle_id>`: if multiple nodes share the same `cycle_id` (e.g. a failed proposer attempt and a subsequent commit node), the alias deterministically resolves to the node with the **latest `ts`** (tie-broken by `node_id` lexicographically). The details panel explicitly states: `"Node 1 of N for cycle-X"` with navigation links to the other attempts.
  - Unknown deep-link: if a hash points to a node not present in loaded history, the UI displays an explicit non-modal notice: `"Node <id> not found in loaded history (coverage: <from> to <to>)"`.

### 2. Canonical Resolution & Parent Invariants

Parent resolution occurs during server-side payload generation across the entire loaded ledger history **BEFORE** applying retention caps:
1. **Recorded Parent Priority**: If `parent_sha` is recorded and present in the loaded history, the edge is `basis: 'recorded'`.
2. **Missing Recorded Parent**: If `parent_sha` is recorded but absent from loaded history (e.g. before retention cutoff), the node retains `parent_sha: '<sha>'`, `parent_known: false`. **A recorded unknown parent is NEVER replaced by a chronological guess.**
3. **Missing Source != Proven Root**: An unresolvable parent indicates an out-of-coverage boundary, not that the node is a proven root of repository history. The UI displays an explicit boundary indicator.
4. **Inferred Edges**: Inferred parents are used **ONLY** when no parent was recorded at all (e.g. leaf attempts), linking to the latest trunk node at or before the node's timestamp with explicit `basis: 'inferred'`.
5. **Multiple Roots & Extra Parents**: Multiple roots are natural in a forest (initial commits, truncated boundaries). If merge commits with multiple parents occur, the primary parent is drawn as the structural edge and secondary parents are documented in `node.secondary_parents`.
6. **Rollback Events**: Rollbacks are recorded as distinct forward cycles/events in the timeline; history is never rewritten or mutated.

### 3. Emitted Graph Budget & Retention

Reader constants from `scripts/techtree_viewer.py`:
- `LEDGER_HISTORY_DAYS = 90` (history read from disk: `cycles-*.jsonl.gz` + live `cycles.jsonl`).
- Deduplication: raw ledger rows (which contain multiple phases per cycle) are deduplicated to unique candidate nodes.
- Emitted payload budget:
  - `LINEAGE_MAX_NODES = 1500` maximum nodes in the embedded payload.
  - Retention selects the newest 1500 unique nodes by timestamp order.
  - **Anchor invariant**: The current HEAD / `current_sha` node is unconditionally retained inside the budget (replacing the oldest node if necessary).
  - Truncation metadata is explicitly declared:
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
  - Graph projection and client filtering **NEVER recompute parent edges**.

### 4. Projection Engine & Immutability Contract

The canonical graph in the payload (`nodes[]`, `edges[]`) is **strictly immutable**. Client-side filtering computes a projected view for rendering:
1. **Node Visibility**: A node is visible if its timestamp falls within the active filter window.
2. **Invalid Timestamps**: Nodes with null or unparseable timestamps are excluded from time-bounded windows (`Today`, `24h`, custom range) and appear only under `All` in an undated section with `data-ts-status="invalid"`.
3. **Branching-Preserving Collapse**:
   - Only **maximal non-branching chains** of out-of-window ancestors are collapsed into a synthetic edge.
   - If an out-of-window ancestor is a **common ancestor / junction** for two or more visible subtrees, it **MUST NOT** be collapsed; it is rendered as a **context junction node** (`class="lineage-context-node"`).
   - If an out-of-window ancestor path contains both `recorded` and `inferred` segments, the resulting collapsed edge basis is `'mixed'`, rendered with distinct dash styling. It is **NEVER** labelled `recorded`.
4. **DOM Provenance Attributes**:
   - Every rendered SVG edge carries: `data-edge-type="canonical|context|collapsed"`, `data-source="<node_id>"`, `data-target="<node_id>"`, `data-basis="recorded|inferred|mixed"`.
   - Collapsed edges additionally carry: `data-collapsed-nodes="N"`, `data-collapsed-edges="M"`, `data-path="<id1>-><id2>..."`.

### 5. Filter Controls & Window Semantics

- **`24h`**: Strict UTC rolling window `[now - 24h, now]`. Value of `now` is captured exactly once per evaluation.
- **`Today`**: `[today 00:00:00.000Z, now]`.
- **`Yesterday+Today`**: Labelled `Yesterday+Today (UTC calendar)`, spanning `[yesterday 00:00:00.000Z, now]`.
- **`Range [from, to]`**: Half-open interval `[from 00:00:00.000Z, to + 1 day 00:00:00.000Z)`. If `from > to`, UI shows an inline error: `"Invalid range: 'from' must be before or equal to 'to'"` without corrupting the graph.
- **Deep-link Out-of-Window**: If a deep-link targets a node outside the current filter window, the UI automatically switches the filter to `All` (updating the active button state) so the target node is visible and focused.

---

## Consequences

### Positive
- Cross-day lineage continuity is fully restored; edges span seamlessly across time.
- Issue #214 is completely resolved by strict UTC 24-hour evaluation.
- Branching topology and edge basis fidelity are preserved under all filter settings.
- Memory and DOM consumption remain strictly bounded under `LINEAGE_MAX_NODES = 1500`.

### Negative / Trade-offs
- Client-side projection logic is required in vanilla JS (~250 lines), replacing simple CSS `display: none`.
- Layout must handle disconnected forest roots and context junction nodes gracefully.

---

## Verification & Test Plan

1. **Exact 24h Boundary**:
   - Node at `now - 24h - 1ms`: strictly excluded from `24h`.
   - Node at `now - 24h + 1ms`: strictly included in `24h`.
2. **Boundary & Empty Range**:
   - Filter range with 0 matching nodes renders clean empty state with note, no JS exception.
3. **Cross-Day Chain & Fork**:
   - Chain crossing UTC midnight renders continuous edge without stubs.
   - Hidden common ancestor branching to two visible descendants on different days renders as context junction node.
4. **Mixed & Inferred Collapse**:
   - Hidden path containing 1 recorded and 1 inferred step collapses to `data-basis="mixed"`.
5. **Unknown Parent & Cycle Guard**:
   - Recorded parent missing from history renders with `parent_known: false` boundary stub; does NOT fall back to chronological guess.
   - Deliberately cyclic parent links in test data break cleanly via `visited` guard without infinite recursion.
6. **Null / Malformed Timestamps**:
   - Node with `ts: "corrupt"` excluded from `24h`/`Today`, included in `All` with `data-ts-status="invalid"`.
7. **Node Identity & Legacy Alias**:
   - Cycle with 2 commit nodes and 1 non-commit leaf: unique DOM IDs for all 3; `#node-<cycle_id>` alias selects latest node; UI indicates "1 of 3 nodes for cycle".
8. **Deep Link & Responsive Cold Load**:
   - Deep link to node outside initial window switches filter to `All`, scrolls target into view, opens card.
   - Verified at 320px, 390px, and desktop viewports.
