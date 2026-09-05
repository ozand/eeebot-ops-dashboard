# ADR-001: Render lineage as one canonical graph and apply dates as a visibility projection

**Status**: Proposed
**Date**: 2026-09-05
**Authors**: Pi coding agent, with operator/reviewer requirements from #218
**Supersedes**: None
**Related**: #212 (edge/outcome legend), #213 (details panel and `#node-<id>`), #214 (24h semantics), #215 (`gate_violations`), #218 (governing issue)

## Context

### Problem statement

The current lineage page renders independent SVG trees per UTC day. The filter then hides or shows whole day sections. This makes dates act as graph boundaries: a node whose recorded parent is on a different day appears rootless or receives only a `↩ from <day>` marker instead of remaining connected to the same lineage. The `24h` button is also day-granular: it keeps calendar days that overlap the window rather than filtering by event timestamps.

Independent user/reviewer requirement for #218: dates are labels and filters, not ancestry boundaries. The graph must be built first from the full available history with canonical parent/basis relationships, and only then projected to the selected date/time range.

### Evidence

- **Direct code evidence**: `scripts/techtree_viewer.py::_build_vertical_day_lineage()` groups nodes by `_lineage_day(ts)`, emits one `<section class="lineage-day-group" data-day="...">` per day, and computes parents inside each day's `trunk`.
- **Direct code evidence**: `_lineage_parent()` returns `(None, None, parent_day)` when a recorded parent exists in `all_nodes` but has a different day, so cross-day ancestry is not drawn as a canonical edge.
- **Direct code evidence**: `assets/vendor/lineage-renderer.js::attachFilter()` hides whole `.lineage-day-group` sections; `selectDays('24h')` chooses days by calendar-day overlap.
- **Operator requirement**: filtering must not recompute parents, must not invent direct links over hidden paths, and must mark hidden/context ancestry explicitly.

### Prior art

The existing custom renderer already performs a small dependency-free forest layout. This ADR does **not** require d3/d3-dag or a new front-end framework. The implementation must first attempt to reuse/extend the existing layout, because the dashboard intentionally avoids external assets and heavy dependencies.

## Decision

The lineage page uses one canonical lineage graph over the available source coverage, then applies UI filters as a visibility projection over that graph.

### What this IS

1. **Unified payload**
   - The generator emits one lineage payload for the available coverage window, not one JSON payload per day.
   - Payload contains canonical nodes and canonical edges:
     - `nodes[]`: one entry per displayed cycle/tree node, sorted by timestamp.
     - `edges[]`: one canonical parent edge per child where known, with `source_sha`, `target_sha`, and `basis`.
   - `basis` is immutable under filtering: `recorded`, `inferred`, or absent/unknown. Filtering never changes an edge basis.
   - Payload includes coverage metadata:
     - `coverage.from_ts`, `coverage.to_ts`
     - `coverage.raw_node_count`, `coverage.emitted_node_count`
     - `coverage.truncated`, `coverage.truncated_before_ts`, and/or `coverage.truncated_count` when bounded history omits older nodes.

2. **Canonical parent semantics**
   - A recorded `parent_sha` edge is used only when the source node is available in the emitted graph.
   - If a node records a parent that is outside source coverage, the parent is shown as unavailable/coverage-boundary context; no edge to another in-range node is invented.
   - If source data lacks a parent and the existing code infers chronological adjacency, that edge remains explicitly `basis: "inferred"` and visually dashed. It does not become recorded ancestry.
   - Filtering does not recompute or replace `parent_sha`.

3. **Visibility projection**
   - Date/range controls compute `in_window` from real timestamps (`ts`) using UTC boundaries.
   - `24h` means `[now - 24h, now]` by timestamp, not calendar-day overlap.
   - Out-of-window nodes with no visible descendants are hidden.
   - Out-of-window nodes that are required to connect visible descendants are represented as contextual ancestry.

4. **Context and collapsed paths**
   - A single out-of-window ancestor may be drawn as a context node/stub with a distinct class (for example `lineage-context-node`) and `data-context="ancestor"`.
   - A chain of multiple hidden ancestors is collapsed into a distinct context path marker with an explicit count, for example `data-collapsed-edges="3"` and visible text such as `3 hidden ancestors`.
   - Collapsed/context paths are styled differently from both recorded and inferred edges. They must never be styled or labelled as direct recorded parent edges.

5. **Roots and parent multiplicity**
   - Multiple roots are valid. The layout renders them as separate root subtrees inside the same SVG.
   - The current source contract has at most one canonical parent per node (`parent_sha` string). If future source data contains multiple real parents, this issue does not silently drop them; it labels the additional-parent state as unsupported/ambiguous or records it in metadata. Full multi-parent DAG routing is out of scope unless implementation analysis proves the existing forest layout can represent it without false ancestry.

6. **Stable IDs and details panel compatibility**
   - The DOM keeps the #213 fragment contract: `#node-<cycle_id>` opens/selects that cycle.
   - Node elements keep `data-cycle-id`; when available, they also carry `data-sha` so canonical edges can be audited by SHA.
   - If a cycle has no `cycle_id`, implementation may use a deterministic fallback DOM id derived from SHA, but it must not break existing `#node-cycle-*` links.
   - Details-card behaviour from #213 is preserved: click, keyboard activation, selected style, hash update, deep-link open, focus management, and post-render panel scroll.
   - Gate-violation rendering from #215 is preserved.

7. **Empty range and deep links**
   - Empty filter ranges show an explicit empty-state message, not a misleading older graph.
   - If a deep link targets a node outside the current UI range but inside source coverage, the page makes that node and immediate context visible (for example by switching to `all` or expanding the projection just for the selected node) and opens the details card.
   - If a deep link targets a node outside source coverage, the page shows an explicit unavailable/coverage message; it does not invent the node.

### What this IS NOT

- Not a new UI framework.
- Not a mandatory d3/d3-dag reintroduction.
- Not a rewrite of #213 details-card behavior.
- Not a new claim that the dashboard has history outside the emitted source coverage.
- Not permission to close #214 before #218 acceptance proves strict timestamp semantics.
- Not permission to infer unknown ancestry as recorded ancestry.

### Success criteria

- One SVG/graph contains cross-day chains without per-day graph breaks.
- Canonical edge count/basis does not change when filters change.
- Filter controls change visibility/projection only.
- Contextual ancestors and collapsed paths are visually and semantically distinct.
- 24h uses exact UTC timestamp math.
- #212 legend/outcome styles, #213 card interactions, and #215 violations all still pass.

## Consequences

### What gets easier

- Operators can follow lineage across UTC day boundaries without mentally joining separate sections.
- #214 can be resolved honestly: `24h` becomes timestamp-based rather than label-based.
- Tests can assert a stable canonical edge set independent of UI filters.
- Deep links no longer need to reason about day-section expansion as a public contract.

### What gets harder

- The renderer must handle projection state, context nodes, collapsed paths, and redraws.
- The payload contract becomes more explicit and therefore needs regression tests.
- The current `lineageDayFilter` API likely changes or becomes a compatibility wrapper.
- Rendering one graph may create a larger SVG than each current day-section, so coverage/truncation metadata and responsive checks are required.
- Multiple-parent data must be surfaced honestly rather than accidentally hidden by a tree-only renderer.

### What does not change

- Static, dependency-light page delivery remains the goal.
- The published `lineage.html` remains self-contained except for the lazy details JSON already introduced by #208/#213.
- Existing node outcome classes and legend semantics from #212 remain.
- Existing details card, keyboard, hash, and focus contracts from #213 remain.
- Existing `gate_violations` JSON/card rendering from #215 remains.

## Alternatives Considered

### Alternative 1 — Rename `24h` to `Yesterday+Today`

This would make the current label less misleading but would not satisfy the user requirement. Dates would still split ancestry into independent day trees.

### Alternative 2 — Keep per-day SVGs and add cross-day text stubs

This is close to current behaviour. It can acknowledge missing parents (`↩ from Sep 04`) but cannot show one continuous graph or preserve canonical edges under projection. It leaves the operator to manually reconstruct lineage.

### Alternative 3 — Reintroduce d3/d3-dag immediately

A full DAG layout library may eventually be useful, especially for true multi-parent graphs, but it conflicts with the current dependency-light static page design and is not yet proven necessary. The implementation must first assess reuse/extension of the existing forest layout.

### Alternative 4 — Server-render all possible filter states

The generator could emit one precomputed graph per filter/window. This multiplies page size, reintroduces chances for inconsistent parent calculations per filter, and makes arbitrary date-range controls awkward.

### Alternative 5 — Do nothing

The operator continues seeing broken cross-day lineage and misleading `24h` behaviour. This directly violates #218 and leaves #214 unresolved.

## Test Contract

| Claim in Decision | Test | Currently |
|---|---|---|
| Cross-day parent/child chain renders inside one SVG without day split | `tests/test_issue208_lineage.py::test_issue218_cross_day_chain_single_svg` | not yet written |
| Canonical `data-source`/`data-target`/`data-basis` edge set is unchanged by filter changes | `tests/test_issue208_lineage.py::test_issue218_filter_preserves_canonical_edges` | not yet written |
| `24h` uses `[now - 24h, now]` timestamps, not day overlap | `tests/test_issue208_lineage.py::test_issue218_24h_strict_timestamp_boundary` | not yet written |
| Out-of-window ancestors needed for visible nodes are shown as context, not normal in-window nodes | `tests/test_issue208_lineage.py::test_issue218_context_ancestors_are_distinct` | not yet written |
| Multiple hidden ancestors are collapsed and labelled with a count, not drawn as direct recorded parent | `tests/test_issue208_lineage.py::test_issue218_collapsed_path_marks_hidden_edge_count` | not yet written |
| Unknown/out-of-coverage parent remains unavailable/unknown and is not inferred as recorded | `tests/test_issue208_lineage.py::test_issue218_unknown_parent_not_invented` | not yet written |
| Empty range displays explicit empty-state message | `tests/test_issue208_lineage.py::test_issue218_empty_range_explicit_empty_state` | not yet written |
| Deep link outside current projection but inside coverage makes target/context visible and opens card | `tests/test_issue208_lineage.py::test_issue218_deep_link_expands_projection_for_target` | not yet written |
| Deep link outside source coverage reports unavailable rather than inventing a node | `tests/test_issue208_lineage.py::test_issue218_deep_link_outside_coverage_reports_unavailable` | not yet written |
| #212 legend/outcome/edge-fill behaviour remains intact at 320/390/1280 | existing #212 tests plus `test_issue218_mobile_responsive_legend_and_svg` | partly passing; new 1280 projection test not yet written |
| #213 click/keyboard/hash/card/focus behaviour remains intact after projection | existing #213 tests | passing before #218 implementation |
| #215 `gate_violations` survive JSON and render in card | existing #215 tests | passing before #218 implementation |

## Rollback

Rollback is straightforward before publication: revert the implementation PR and restore the current per-day renderer. After publication, rollback means republishing the previous generator/page commit. No source data is migrated or destroyed by this decision.

## References

- Issue #218 — unified DAG governing issue
- Issue #214 — 24h boundary semantics, intentionally left open until #218 acceptance
- PR #220 / commit `74b0e131` — #213 details-card/hash/focus behaviour to preserve
- PR #221 / commit `d177b7f0` — #215 `gate_violations` behaviour to preserve
- `scripts/techtree_viewer.py::_build_vertical_day_lineage`
- `scripts/techtree_viewer.py::_lineage_parent`
- `assets/vendor/lineage-renderer.js::layoutDay`
- `assets/vendor/lineage-renderer.js::attachFilter`
