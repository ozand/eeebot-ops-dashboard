# Architecture Decision Record (ADR): Unified DAG + Visibility Projection (#218)

## 1. Context

The operator requires a continuous, unbroken visual representation of the agent's
evolution history (`techtree`/`lineage`), regardless of calendar-day boundaries.
Currently, `techtree_viewer.py` emits independent `day-group` SVG trees, and the
JavaScript renderer hides/shows them via CSS. This "cuts" cross-day parent
relationships and makes the "24h" filter granular to the calendar day rather
than the event timestamp.

The updated requirements state:
- The `parent_sha` basis must be immutable under projection; no "invented"
  direct parents if the real parent is hidden; no recalculation of ancestry
  based on remaining nodes.
- Unknown origin stays unknown; inferred relationships stay dashed.
- Calendar boundaries must not break SVG paths.
- "Contextual ancestors" (nodes out of the selected window but required to root
  an in-window node) must appear explicitly as collapsed pre-history.
- Existing features (a11y cards, gate violations, legends) must survive.

## 2. Decision and design elements

We will move to a **unified payload + client-side visibility projection** model,
reusing the existing custom `forest-layout` algorithm without introducing new UI
frameworks.

### 2.1 Payload contract (Generator)

`techtree_viewer.py` will emit exactly **one** JSON payload representing the
entire available history (up to `LINEAGE_DAYS`, truncated at `LINEAGE_DAY_CAP * DAYS`
overall nodes if needed, though a global limit `14 * 120 = 1680` nodes is well
within JS limits).

```javascript
window.lineageData = {
  // Ordered historically
  nodes: [
    { sha, cycle_id, ts, outcome, reason, /* ...details */ },
    // ...
  ],
  // All confirmed edges across the time window
  edges: [
    { source: "sha_A", target: "sha_B", basis: "recorded" | "inferred" },
    // ...
  ]
};
```
The generator will still resolve `(parent_sha, basis, parent_day)`, but `parent_day`
cross-day stubs are removed: all edges describe the unified DAG.

### 2.2 Date projection filter (Renderer)

`lineageDayFilter.select(mode, days)` becomes `lineageDateFilter.apply(mode, fromTs, toTs)`.
The filtering happens inside `renderGraph(window.lineageData)`:

1. **Window inclusion**: For each node, `inWindow = (ts >= fromTs && ts <= toTs)`.
2. **Ancestry trace**: For any in-window node, its ancestry path back to a root
   (or the edge of available history) is traced. Out-of-window nodes on this path
   are marked `isContext = true`.
3. **Graph reduction**: Nodes where `!inWindow && !isContext` are pruned.
4. **Collapsed path grouping (The "Ghost Path")**:
   - Sequential out-of-window context nodes are collapsed into a single `contextual stub`
     node in the data structure sent to the layout algorithm.
   - The edge connecting an in-window node to a collapsed stub receives a specific
     attribute (e.g., `data-collapsed-edges="3"`).
   - The renderer draws these context edges as a distinct style (e.g., dotted,
     faded, or explicit label "hidden history") — distinctly different from
     `inferred`, and explicitly NOT drawn as a direct real parent edge.

### 2.3 Multiple roots and multiple parents

- **Multiple roots**: Supported natively by the existing `layoutDay` algorithm.
  If the projection leaves multiple independent in-window subgraphs, they are
  laid out as parallel trees.
- **Multiple parents**: The source data (`cycle.parent_sha`) is inherently a
  string, yielding exactly 0 or 1 recorded parent per cycle. If Git merges
  (multiple parents) appear in `ledger`, the `techtree_viewer.py` currently
  resolves to the primary `parent_sha` or falls back to `inferred`. We will
  maintain the 1-parent limit structurally, as true DAG rendering (crisscrossing
  merge lines) exceeds the current simplistic loop-less SVG layout constraint,
  and the product spec restricts us from writing a brand new d3-DAG renderer from scratch.

### 2.4 Stable IDs and hash navigation (#213 compat)

- The `#node-<cycle_id>` URL hash contract remains mathematically unchanged.
- If a deep link points to an out-of-window node (or collapsed context node),
  the JS filter will automatically switch mode to `all` (or expand the window to
  include the requested timestamp) and `scrollIntoView()`.
- Deep links pointing to nodes outside the `LINEAGE_DAYS` server-side dataset
  will fail gracefully (card: "details unavailable").

### 2.5 Forest layout reuse

The current `layoutDay` performs a two-pass layout:
1. DFS depth assignment.
2. Sibling horizontal spacing (`x` coordinates) via column counters.

This algorithm works for any loopless planar tree, which the filtered DAG
remains. We will apply `layoutDay` to the **reduced projection** graph. A single
huge, tall SVG replaces the N separate day-SVG elements. The viewport scrolls
normally. `scrollPanelIntoView` bounds continue working exactly as built in #213.

## 3. Browser acceptance and test strategy

The following new browser/integration tests will be written alongside implementation:

| Test Case | Verification |
|---|---|
| **Cross-day chain without break** | A tree spanning Day 1 to Day 2 renders as one connected SVG; no "from Day 1" generic stubs. |
| **Immutable canonical edges** | Filtering to Day 2 alone collapses Day 1 parent into a distinct "context" stub; the parent identity/SHA does not change in the DOM dataset; no edge is drawn directly between Day 2 node and Day 0 node over missing history. |
| **24h strict TS filter** | `mode=24h` accurately includes only nodes within `[now - 24h, now]`. Nodes at `now - 25h` are pruned or collapsed to context, instead of drawing the whole day grid. |
| **Unknown stays unknown** | Missing roots remain rootless under filtering; no fallback edge to nearest time neighbor. |
| **Mobile responsive bounds** | SVG `viewBox` correctly rescales; legend wraps at 320px/390px/1280px. |
| **Cold card deep link** | `#node-X` on a hidden node auto-expands the range, fetches, and scrolls precisely. |

## 4. Consequences

- ✅ Resolves #214 accurately (true TS borders).
- ✅ Removes JS hide/show toggles of large DOM blocks; redrawing the 1-SVG graph
  dynamically is fast natively in the browser up to 1000 nodes.
- ⚠️ Generating a single massive HTML inline for all days increases the max initial
  DOM node count of that payload. The max (14 days × 120 nodes = 1680) is well
  within performance limits, but lazy pagination is lost.
