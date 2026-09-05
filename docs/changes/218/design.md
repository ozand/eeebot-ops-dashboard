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

Therefore, calendar days are currently graph boundaries. This violates the new #218 requirement.

## Target contract

### Data model

The generator emits one canonical lineage payload for the available coverage window:

```jsonc
{
  "coverage": {
    "from_ts": "...",
    "to_ts": "...",
    "raw_node_count": 0,
    "emitted_node_count": 0,
    "truncated": false,
    "truncated_before_ts": null,
    "truncated_count": 0
  },
  "nodes": [
    {
      "sha": "...",
      "cycle_id": "cycle-...",
      "ts": "2026-09-05T...Z",
      "outcome": "integrated|failed|partial|skipped|unknown",
      "title": "..."
    }
  ],
  "edges": [
    {
      "source_sha": "parent-sha",
      "target_sha": "child-sha",
      "basis": "recorded|inferred",
      "source_available": true
    }
  ]
}
```

Rules:
- `parent_sha`/`basis` are canonical and immutable under filtering.
- Filtering never recomputes parentage and never rewrites `basis`.
- A missing/out-of-coverage parent is represented as unavailable/unknown context, not inferred as recorded ancestry.
- `cycle_id` remains the stable public hash target: `#node-<cycle_id>`.
- `sha` remains the canonical graph-edge identity and should be present as `data-sha` on DOM nodes where possible.

### Projection semantics

Projection is a view over the canonical payload:

1. Compute `in_window` by UTC timestamp.
2. For each `in_window` node, trace canonical ancestors within available coverage.
3. Hide nodes that are outside the window and irrelevant to visible nodes.
4. Preserve required out-of-window ancestors as contextual ancestry.
5. Collapse a contiguous chain of hidden/out-of-window ancestors into a labelled collapsed-path marker.

A context/collapsed path is **not** a direct recorded edge. It must have separate CSS/DOM markers, for example:

- `.lineage-context-node`
- `.lineage-context-edge`
- `data-context="ancestor"`
- `data-collapsed-edges="N"`
- visible label: `N hidden ancestors` / `outside selected range`

### Filter controls

- `All`: show full emitted coverage.
- `Today`: UTC calendar day matching the viewer clock.
- `24h`: exact `[now - 24h, now]` timestamp range.
- `Yesterday+Today`: explicit calendar-day mode; may remain if useful but must be labelled as calendar-based.
- Custom date range: inclusive UTC day bounds, e.g. from `YYYY-MM-DDT00:00:00Z` to `YYYY-MM-DDT23:59:59.999Z` unless the UI later adds time inputs.

### Empty and boundary states

- Empty range: render explicit empty-state message, not an older fallback graph.
- Range with visible node but missing parent in source coverage: show unavailable context/root marker.
- Deep link to node inside coverage but outside current range: make target and context visible and open the card; implementation may switch to `All` or temporarily extend the projection for that target.
- Deep link outside coverage: show explicit unavailable/coverage message and do not invent a node.

### Multiple roots / multiple parents

- Multiple roots are valid and rendered as independent root trees in the same SVG.
- Current data exposes one canonical `parent_sha` per node. If future raw data contains multiple parents, #218 must not silently pretend all were rendered. Minimum acceptable behaviour: label additional parents as unsupported/ambiguous metadata. True multi-parent routing is only in scope if implementation analysis proves the existing layout can show it without false edges.

## Layout approach

First attempt: **reuse/extend** the existing dependency-free forest layout.

Rationale:
- The repo intentionally ships a stdlib/static page and previously removed d3/d3-dag.
- Current source effectively provides one canonical parent per node, so a forest renderer is likely sufficient for #218.
- Reuse must be validated, not assumed. If implementation analysis shows the existing layout cannot represent required context/collapsed paths honestly, stop and report before bringing in a new engine.

## Implementation sequence after GO

1. Add failing fixtures/tests for unified payload and projection.
2. Introduce a pure helper to build canonical lineage graph data from current rows.
3. Keep existing per-day renderer untouched until new tests demonstrate the new graph contract.
4. Update renderer to draw one SVG from the unified payload.
5. Port #212 legend/outcome/edge styling to context/collapsed edge classes.
6. Preserve #213 click/keyboard/hash/focus behaviour against new node elements.
7. Preserve #215 gate-violations details-card rendering.
8. Publish and browser-verify after merge.

## Acceptance fixtures to add

| Fixture | Assertion |
|---|---|
| Cross-day chain `A(day1) -> B(day2) -> C(day3)` | One SVG; canonical edges A→B and B→C exist; no per-day root break. |
| Fork across dates `A(day1) -> B(day2)` and `A(day1) -> C(day3)` | Both children keep source A, even when filtering to day2/day3 separately. |
| 24h strict boundary | Node at `now - 24h` included; node at `now - 24h - 1ms` excluded/context only if ancestor. |
| Hidden ancestor chain | Filter to descendant only shows contextual/collapsed path with count; no direct recorded edge over hidden nodes. |
| Unknown parent | Missing parent remains unknown/unavailable; no chronological fallback promoted to recorded. |
| Multiple roots | Two root subtrees render in same SVG. |
| Deep link outside current projection | Target/context visible and card open. |
| Empty range | Explicit empty-state message. |
| Responsive | 320/390/1280 viewport checks for legend, SVG, details card. |
| Regression | #212, #213, #215 tests still pass. |

## Browser acceptance plan

- Use Playwright against generated HTML served over local HTTP when lazy JSON is needed.
- Verify real rendered DOM, not only static strings:
  - edge `data-source`, `data-target`, `data-basis`
  - context/collapsed path classes and labels
  - visible node/card bounds at 320/390/1280
  - `#node-<cycle_id>` opens card after cold page load
  - details body text visible after JSON resolves
- Published-page gate after merge must quote footer SHA, visible filter behaviour, and one live node/card result.

## Open questions before implementation GO

1. Should a deep link outside the selected range switch the visible filter to `All`, or should it render a temporary `deep-link context` projection while leaving controls unchanged?
2. Should collapsed context labels count hidden **nodes** or hidden **edges**? ADR uses edges; UX label may say ancestors.
3. Should `Yesterday+Today` remain after strict `24h` exists, or should it move to a clearly-labelled calendar section?
4. What is the maximum acceptable SVG node count before truncation messaging must appear? Proposed initial bound: existing `LINEAGE_DAYS * LINEAGE_DAY_CAP`.
