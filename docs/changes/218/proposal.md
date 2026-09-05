# #218 Proposal: Unified DAG with Date-Projection Filter

## Problem statement

The lineage page renders one independent SVG per calendar day. The day filter
hides/shows entire `<section class="lineage-day-group">` blocks. This means:

- A node whose `parent_sha` was committed on a different day loses its root
  when that day's section is hidden — the lineage is **cut**, not filtered.
- The `24h` button keeps every UTC calendar day that overlaps the window (up to
  two days), so "24h" may show nodes from 36–47 hours ago.
- The operator cannot see continuous version history across a date boundary.

## Proposed solution (bounded)

Replace the per-day-section architecture with a single unified payload covering
all available history (up to `LINEAGE_DAYS = 14` days). The date filter becomes
a **client-side visibility projection**: nodes outside the selected window are
either hidden (if they have no in-window descendants) or shown as compact
contextual stubs (if an in-window node's ancestry chain passes through them).

This is a targeted architectural change. It reuses the existing forest-layout
algorithm (`layoutDay`) with a generalised input and does not introduce a new
DAG engine or external dependency.

## What is NOT in scope

- A new DAG layout engine (d3-dag, etc.)
- Changes to #213 (a11y/cards), #215 (violations), #212 (legend/outcomes)
- Mobile framework rewrite
- Closing #214 before this issue's AC are verified (see dependency comment on #214)

## Governing issue

GitHub Issue: [ozand/eeebot-ops-dashboard#218](https://github.com/ozand/eeebot-ops-dashboard/issues/218)

## Status

**Pending operator GO on design.** This document is the design-gate artifact;
no implementation code is written until GO is received.
