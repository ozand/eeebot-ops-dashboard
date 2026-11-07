---
version: alpha
name: Eeebot Evidence Terminal
description: Proposed evidence-first visual system for the public operations dashboard.
colors:
  primary: "#2fd3c4"
  canvas: "#080f0c"
  surface: "#0d1a15"
  text: "#dcebe1"
  secondary: "#b8d0c2"
  muted: "#8aa695"
  success: "#56d364"
  warning: "#e6b56c"
  error: "#ff8f8f"
typography:
  heading:
    fontFamily: "Consolas, Menlo, DejaVu Sans Mono, monospace"
    fontSize: 24px
    fontWeight: 700
    lineHeight: 1.25
  body:
    fontFamily: "Consolas, Menlo, DejaVu Sans Mono, monospace"
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.5
  metadata:
    fontFamily: "Consolas, Menlo, DejaVu Sans Mono, monospace"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
rounded:
  sm: 4px
  md: 8px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 32px
components:
  page:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.secondary}"
    rounded: "{rounded.md}"
    padding: "{spacing.md}"
  action:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.primary}"
    height: 44px
    rounded: "{rounded.sm}"
  success:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.success}"
  warning:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.warning}"
  error:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.error}"
  metadata:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.muted}"
    typography: "{typography.metadata}"
---

## Overview

**Proposed design target under #432 and Proposed ADR-006; not implemented or accepted.**
Tokens are normative only within this proposal. Current executable CSS remains
truth for the shipped site. This file does not authorize UI changes or deployment.

An operator checks observed work, confirmed results and reasons to intervene.
The interface feels like an evidence terminal, not a decorative control room.
Preserve the existing monospace/forest identity; improve hierarchy, not branding.
The signature is task → attempts → result → evidence, with unknowns left visible.

## Colors

Forest-black canvas and tonal green surfaces retain the existing terminal world.
Cyan identifies navigation/action; green means confirmed success, amber waiting or
incompleteness, red an observed error. Gray-green means secondary/unknown metadata.
Never use color alone: accompany it with a label or symbol. Four text roles are
primary text, supporting secondary, metadata muted, and explicitly disabled text.
Disabled controls retain a label; their opacity is not a status signal.

## Typography

Use the existing local monospace fallback stack, without remote font downloads.
The proposed 24/16/14 scale separates task/status focus from reading and metadata;
weight and space establish hierarchy before adding color. Numbers use tabular
alignment. Long prose uses a readable measure, approximately 72 characters.
Labels wrap; essential identifiers are copyable and may break at safe boundaries.

## Layout

Use a 4px base: 8px related-item gaps, 16px panel padding, 24–32px section gaps.
One focal question leads each page; diagnostics follow, repeated definitions fold.
The shared shell contains identity, navigation and snapshot age, not an overflowing
KPI rail. At 390px, badges wrap and tables become cards or named scroll regions.
Verify also at 1440px. Graphs/heatmaps may scroll locally, never hide key results.

## Elevation & Depth

Use quiet tonal surfaces and subtle separators, not decorative gradients or heavy
shadows. Dense controls sit together; evidence sections have more breathing room.
Keep focus rings conspicuous even when borders are intentionally restrained.

## Shapes

4px controls, 8px panels. Avoid oversized pill styling for long technical labels.

## Components

Status: label + observation + timestamp/coverage + evidence link when available.
Keep measured zero, no data, unknown, withheld and stale distinct. Staleness is an
additional age condition, not a replacement for measured/missing value semantics.
Do not change stored enum/schema contracts to obtain these presentation labels.

Controls use native links/buttons/inputs first. Provide persistent names, visible
focus, current-page/pressed semantics, and non-overlapping 44px touch targets.
Support loading, empty, unavailable and retry states where relevant. Selected
chart values must be readable without hover; avoid thousands of tab stops.

Cycle summary: outcome, task, time, key available metrics; expand for evidence.
Retain material missing-data warnings in the summary. Recorded and inferred graph
edges remain distinct. Budget fit and instruction truncation remain separate facts.
No new animation is required; respect reduced-motion preferences if motion is added.

## Do's and Don'ts

- Do separate feed freshness, observed execution, integration and useful outcome.
- Do state population, window, timezone and missingness beside affected metrics.
- Do retain old deep links and offer list alternatives for graph-heavy workflows.
- Don't invent evidence, health, money costs or a next selection from missing data.
- Don't reveal private text to make a public empty state more informative.
- Don't install a UI framework or change telemetry solely to implement this style.

Format reference: google-labs-code/design.md alpha spec at
`1ed38aa9dd4451b9039f9aeb9ed1d2579edafddf` (docs/spec.md).
The format/linter is tooling guidance, not a runtime dependency or WCAG certificate.
