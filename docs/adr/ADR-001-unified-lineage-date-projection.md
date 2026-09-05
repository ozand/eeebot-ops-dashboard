# ADR-001: Unified Lineage Graph with Date-Projection Filtering

- Status: Proposed
- Date: 2026-09-05
- Deciders: operator, autonomous dev session
- Consulted: subagent-chat-01a071f4-ea48-77ea (independent reviewer)
- Informed: issue #218, #214, #213, #215, #212

## Context and Problem Statement

`lineage.html` previously partitioned lineage history into separate `<section class="lineage-day-group">` elements, each with its own independent SVG tree. When a cycle's parent commit lived on an earlier day, the cross-day edge was broken into a textual `↩ from <day>` stub instead of drawing a continuous edge in the graph. Furthermore, date filtering worked by hiding whole day sections, tearing graph topology apart at arbitrary 00:00 UTC boundaries.

The operator and user requirement is unambiguous: **lineage must be rendered as one continuous graph over available history**. Selecting a date range or "24h" must act as a visibility projection over this single canonical graph, reducing visible clutter without destroying branch continuity, recomputing parentage, or inventing false direct edges across hidden nodes.

## Decision

1. **One Canonical Graph Payload**
   The generator emits one unified payload containing `nodes[]` and `edges[]` covering the full available source history (up to bounded caps).
   - Every node has a globally unique `node_id`:
     - Commit-bearing node: `c:<sha>`
     - Attempt/leaf without commit: `a:<cycle_id>[:<disambiguator>]`
   - `sha` is nullable (`str | None`); failed/skipped attempts without a commit have `sha: null` and are NEVER assigned synthetic commit SHAs.
   - Canonical edges connect `source_node_id` -> `target_node_id` with immutable `basis` (`recorded` | `inferred`).
   - The canonical graph payload is strictly immutable: client-side date filtering never adds, removes, or rewrites canonical edges or node parentage.

2. **DOM Element IDs and Deep Links**
   - Each node element in the SVG has a strictly unique DOM id: `id="node-" + safe_id(node_id)` (e.g. `id="node-c-ecbe56903f35"` or `id="node-a-cycle-123"`).
   - Direct deep link: `#node-<node_id>` selects that exact node.
   - Legacy alias: `#node-<cycle_id>` is preserved as an alias. If a cycle produced multiple nodes (e.g. 2 commits or 1 commit + 1 leaf attempt), the alias deterministically selects the node with the latest `ts` (tie-break by `node_id` lexicographically). The details panel/card explicitly notes: `"Node 1 of N for <cycle_id>"` with links/buttons to toggle between sibling nodes of that cycle.
   - Deep link outside active filter window: immediately switches the UI filter controls to `All` (visually marking `All` as active) so the user sees the active filter state honestly, scrolls the target node into view, and opens the details card.

3. **Visibility Projection Semantics**
   - Date filtering is a client-side visibility projection over the single canonical graph.
   - Strict UTC boundaries:
     - `24h`: exact timestamp range `[now - 24h, now]` where `now` is captured once at filter invocation time.
     - `Today`: `[today_midnight_utc, tomorrow_midnight_utc)`.
     - `Yesterday+Today (UTC calendar)`: explicitly labelled as calendar days `[yesterday_midnight_utc, tomorrow_midnight_utc)`.
     - Custom date range: `[from_utc_midnight, to_utc_midnight + 24h)` (half-open, inclusive of the entire 'to' date up to the next midnight, avoiding sub-millisecond truncation).
     - Range validation: if `from > to`, the UI renders a clear warning `"Invalid date range: 'from' must be before or equal to 'to'"` and does not clear or corrupt the graph.
   - Nodes inside the window are visible primary nodes.
   - Empty range: when no nodes match the active window, the UI renders an explicit empty-state message `"No cycles recorded in selected interval"`, never falling back to an older graph.

4. **Branching Preservation & Contextual Ancestor Collapsing**
   - Ancestors outside the active window required to connect visible nodes are rendered as contextual ancestry.
   - **Maximal Non-branching Chains**: out-of-window nodes are collapsed if and only if they form a linear path without junctions (every intermediate node has in-degree == 1 and out-degree == 1 in the ancestor forest, and no other visible descendants).
   - **Junction Preservation**: if a hidden ancestor `J` is a common ancestor of two or more distinct visible branches, `J` MUST NOT be collapsed into an edge. `J` is rendered as a distinct **context junction node** (`.lineage-context-node`, `data-context="junction"`).
   - **Collapsed Path Marker**: linear collapsed chains are rendered as a single context edge with:
     - Visible label counting hidden **NODES**: `"N hidden nodes"` / `"N ancestors"`.
     - DOM provenance attributes: `data-edge-type="collapsed"`, `data-collapsed-nodes="N"`, `data-collapsed-edges="M"`, `data-path="id1->id2->..."`.
     - Mixed basis: if the collapsed chain contains both `recorded` and `inferred` steps, its basis is composite `data-basis="mixed"` and must NEVER be styled or labelled as `recorded`.
     - Interactive expansion: clicking the collapsed path marker expands that linear segment or switches filter to `All`.

5. **Coverage, Retention Budget & Caps**
   - Tier 1: Source read window (`LEDGER_HISTORY_DAYS` in generator).
   - Tier 2: Emitted graph cap:
     - Target budget: `LINEAGE_MAX_NODES = 1500` nodes (or newest 14 calendar days).
     - Deterministic retention: newest events up to cap; trunk HEAD / current SHA is always anchored and retained.
     - The payload includes an explicit `coverage` block:
       `{ "from_ts": "...", "to_ts": "...", "raw_node_count": N, "emitted_node_count": M, "truncated": bool, "truncated_before_ts": "...", "truncated_count": K }`
     - Edges pointing to ancestors before `truncated_before_ts` have `source_available: false, source_boundary: "truncated_history"`.
     - Truncated boundaries are explicitly labelled as history truncation, NOT as proven root commits.
   - Tier 3: UI filter projection over the emitted graph.

6. **Defensive Graph Traversal**
   - All graph traversals (depth, ancestor walk, descendant collection) maintain a `visited: set[str]` guard to prevent infinite loops on corrupted or cyclic parent references. If a cycle is detected, traversal terminates and flags `cycle_detected: true`.
   - Timestamps: malformed or unparseable timestamps are parsed to `ts: null`, tagged with `data-ts-status="invalid"`, and rendered safely at boundary slots rather than throwing unhandled exceptions.

7. **Preservation of Prior Verified Increments**
   - #212: outcome styles (`integrated`, `skipped`, `partial`, `failed`), dash styles, and mobile flex-wrap legend (320px/390px) remain fully preserved.
   - #213: cycle details panel, keyboard navigation (Tab, Enter, Space), close button (`×`), Escape key handling, focus return, and `history.replaceState` hash updates remain preserved.
   - #215: `gate_violations` array preservation from ledger to JSON to details card remains preserved.

## Consequences

### Positive
- Cross-day lineages render continuously without artificial date cuts.
- Date filters reduce cognitive clutter without mangling graph topology.
- Canonical edges are immutable and verifiable.
- Ambiguity regarding multiple SHAs per cycle is resolved with unique `node_id`s and a deterministic legacy fallback.
- No third-party heavyweight DAG library (e.g. d3-dag) is required; existing forest layout is reused and extended.

### Negative / Trade-offs
- Rendering a single SVG across 14 days requires careful coordinate scaling and viewport containment.
- Collapsed-chain calculation introduces client-side graph traversal logic that must be unit-tested.

## Test Contract

The test suite must verify:
1. **Canonical Immutability**: `payload.edges` remains identical in count, source, target, and basis before and after any client projection call.
2. **Displayed Edge Provenance**: every displayed SVG edge maps either to a canonical edge or a valid collapsed path (`data-path` matches a real path in canonical edges).
3. **Unique DOM IDs**: every rendered node has a unique `id="node-..."` even when 2 SHAs and 1 leaf share the same `cycle_id`.
4. **Junction Preservation**: hidden common ancestor of two visible branches renders as a context node, not a merged edge.
5. **Mixed Basis**: a collapsed chain with both recorded and inferred links has `data-basis="mixed"`, not `recorded`.
6. **Strict 24h Boundary**: node at `now - 24h` is in-window; node at `now - 24h - 1s` is out-of-window (context only if ancestor).
7. **Invalid Range**: `from > to` displays error text and does not crash.
8. **Deep Link Auto-All**: opening `#node-<out_of_window_id>` sets filter to `All` and opens card.
9. **Regression Gate**: #212, #213, #215 assertions continue to pass.
