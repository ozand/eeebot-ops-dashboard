# ADR-006: Keep dashboard presentation evidence-first and visual guidance in DESIGN.md

**Status**: Proposed
**Date**: 2026-11-07
**Authors**: Pi coding assistant (AI-generated proposal for @ozand)
**Deciders**: @ozand
**Supersedes**: None
**Related**: ADR-001, ADR-002, ADR-004, ADR-005; #432

## Context

The owner requested issue/ADR/design documentation following a public interface
review. Direct rendered observation at desktop and mobile sizes found an
overflowing shared KPI header, clipped mobile reference tables and mixed-status
badges, repeated feed caveats and an inconsistent token day summary (#433).
This is presentation evidence from a changing public snapshot, not a diagnosis
of runtime health, reader permissions, arithmetic cause or a WCAG certification.

The existing retro-terminal identity is intentional (#38). Replacing it with a
new framework/theme would add cost without addressing interpretation. Existing
ADRs 001/002/004/005 are Proposed; this record does not accept or rewrite them.
#386 owns current-source versus historical-observation authority; #406/#408 own
cycle timing semantics. Work-unit pilot #421/#423/#425/#427 is separate.

A persistent design source is needed to prevent each page inventing different
status colors, controls and hierarchy. The owner named google-labs-code/design.md;
its alpha specification at 1ed38aa9dd4451b9039f9aeb9ed1d2579edafddf supports
machine-readable tokens plus rationale. This is a documentation format, not a
reason to install a frontend framework or publish data.

## Decision

### What this IS

This proposed decision would establish root DESIGN.md as the single visual-guidance
source for this dashboard. It contains proposed tokens and rationale, clearly
separate from shipped CSS until governed implementation lands. Future changes to
the design source and affected UI belong to the same issue/reviewed revision.
Do not create a competing .interface-design/system.md; if a tool needs that path,
use a reference to DESIGN.md rather than a second normative token set.

Presentation leads with observed work, confirmed result and actionable evidence.
Retain the terminal identity, local fonts and current rendering stack. Compact
summaries precede expandable diagnostics without hiding material unknowns.

Existing source/schema owners retain authority. Display measured zero, no data,
unknown and withheld distinctly, with staleness as an additional age condition.
Feed freshness does not imply execution success; execution success does not imply
integration. Static configuration never proves a historical request payload.
Expose population/window/timezone where needed, not new guessed computations.

Shared navigation and controls have accessible names, focus/current/selected
states and touch targets. Graph/chart information has a keyboard/touch path and
readable alternatives. Responsive layouts must not silently clip important content.
Proposed visual defaults are checked at 390px and 1440px with rendered evidence.

### What this IS NOT

No runtime, collector, persistence schema, model, cost, trust/access or deployment
change. No automatic migration of current styles, new universal health verdict,
new source of truth for metrics, or private text publication. This ADR neither
implements its test contract nor clears any existing pilot/release acceptance.
Routine arithmetic fixes such as #433 do not wait for a visual ADR acceptance.

## Alternatives Considered

- Per-page fixes without shared guidance: fastest first edit, but duplicates
  token/state choices and preserves conflicting visual priorities.
- Adopt a new component framework/theme: potentially rich primitives, but adds
  dependency/build migration to a static Python-rendered tool; not necessary for
  the demonstrated gaps. Reuse native controls and existing behavior first.
- Maintain both DESIGN.md and system.md: suits individual tools but creates two
  authorities that can drift. One source plus references is sufficient.
- Force a new global data-state enum: tempting uniformity, but would cross reader
  and schema boundaries; preserve current typed contracts and map presentation.
- Do nothing: retains the identity but leaves misleading hierarchy and mobile
  clipping. Keep the identity, not the defects.

## Consequences

### What gets easier

Page-specific work can share controls/status styling and a clear result-to-evidence
path. Design changes can be reviewed as explicit token/rationale differences.

### What gets harder

Maintainers must keep guidance and changed UI aligned. Accessible graph/list
alternatives and responsive evidence captures require real implementation effort.
The alpha format may evolve; upgrades need a deliberate compatibility check.
Folding diagnostics can conceal important facts unless summary invariants are tested.

### What does not change

All source, privacy, redaction, retention, recorded/inferred and runtime/dashboard
boundaries. Existing ADR statuses and operator-reserved acceptance remain intact.
No assertion that today's public snapshot is complete or the host is healthy.

## Test Contract

Names below are planned tests, not existing executions or passing coverage.
Acceptance of this ADR requires independent review and an explicit decider action;
implementation acceptance additionally requires each applicable owning issue's checks.

| Decision claim | Proposed runnable test | Current state / owner |
|---|---|---|
| Canonical token references/section structure | `npx -p @google/design.md@0.4.0 designmd lint DESIGN.md` | Run/result in #432 PR; tooling only |
| Fresh feeds do not imply useful execution | `tests/test_now_presentation.py::test_fresh_feeds_unknown_execution` | Not yet written; #435 |
| Zero/absent/withheld remain distinct | `tests/test_presentation_states.py::test_value_state_matrix` | Not yet written; #434, with page fixtures in #433/#440 |
| Accessible responsive shared controls | `tests/test_interface_browser.py::test_shell_focus_and_mobile_targets` | Not yet written; #434 |
| No silent mobile table/badge clipping | `tests/test_interface_browser.py::test_agent_about_mobile_content_reachable` | Not yet written; #440/#442 |
| Graph selection and list agree | `tests/test_interface_browser.py::test_lineage_list_matches_selected_cycle` | Not yet written; #436 |
| Charts work without hover | `tests/test_interface_browser.py::test_token_keyboard_touch_detail` | Not yet written; #438 |
| Public/private boundary remains intact | Existing projection/redaction suite selected by changed paths plus hostile public fixtures | Required per implementation; no new pass claimed |

## Rollback

A docs-only proposal is withdrawn/superseded through normal ADR lifecycle; do not
rewrite accepted history. Future UI changes retain reversible release artifacts
and are rolled back by their own authorized release procedure. Publishing private
text is not reversible and is never an acceptable visual-design experiment.

## References

- [Planning and issue map](../plans/432-interface-audit.md)
- [Proposed DESIGN.md](../../DESIGN.md)
- [Governing issue #432](https://github.com/ozand/eeebot-ops-dashboard/issues/432)
- [Pinned upstream format](https://github.com/google-labs-code/design.md/blob/1ed38aa9dd4451b9039f9aeb9ed1d2579edafddf/docs/spec.md)
- Interface-design public rendered audit, 2026-11-07; route-specific sanitized
  observations are retained in #433–#443. They are snapshots, not current host proof.
