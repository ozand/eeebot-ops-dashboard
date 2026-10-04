# ADR-004: Publish planning/rest observations as a separate typed status

**Status**: Proposed
**Date**: 2026-10-04
**Authors**: Pi coding assistant
**Supersedes**: None
**Related**: ADR-002 (agent context reader contract), issue #395

## Context

The existing cycle feed is derived from allowlisted execution-cycle ledger phases. Planning sessions and planner rest-held observations are recorded in the same approved state root but are not actual execution cycles and must not be fabricated as such. A published page can therefore show an older cycle while a newer planning observation exists. Showing that observation requires a new public projection field, freshness semantics, and an explicit privacy boundary: rest state includes a raw wake-condition reference and snapshot value that must not be published.

The existing remote reader already reads `STATE_ROOT/ledger/cycles.jsonl`; the planner state is the adjacent `STATE_ROOT/planner/rest_state.json`, schema `planner-rest-v1`. This reuses the same documented SSH reader identity/root and adds no host permission or path. Source fields are constrained to planning phases `planning_session`, `planner_rest`, `planner_rest_held`; outcomes `integrated`, `refused`, `malformed`, `no_plan`, `spawn_failed`, `commit_failed`, `timed_out`, `rest`, `rest_unchanged`; and rest fields `active_rest` presence, `wake_condition.kind` allowlisted, `deadline`, `consecutive_rests`, `held_ticks_since_last_session`, and `review_signal`. The reference, snapshot, free-text reason, and raw lines are excluded.

The evidence for the October 4 incident is a bounded read-only observation: planning outcome `rest_unchanged` and `planner_rest_held` entries existed while no new actual execution-cycle row was observed in the inspected window. This does not establish active work or why the candidate wake condition remains unchanged.

## Decision

### What this IS

The dashboard publishes one typed `planning_activity` observation separately from actual cycle rows. It contains the latest allowlisted planning outcome and timestamp, source status/mtime/age, plus validated rest-state fields: active flag, allowlisted wake-condition kind, deadline, bounded counters, and review-signal boolean. Each source has independent freshness; use a 900-second presentation freshness threshold as an explicit dashboard display policy. This is not proof of timer execution, runtime health, or an SLA. A stale rest file remains visible as last-recorded evidence, while current rest status is `unknown`.

### What this IS NOT

This is not a synthetic cycle, active-work indicator, runtime health/SLA, rest-policy judgment, wake-condition explanation, or proof that a candidate changed. It does not publish raw wake references, snapshot hashes, prompts, free-text reasons, or logs, and does not alter host reads/permissions or runtime behavior.

### Success criteria

Missing, malformed, unreadable, or stale inputs are explicitly absent/unavailable/unknown and never become inactive by default. Actual cycle rows retain their existing reader and presentation semantics. The typed public projection withholds unapproved fields and its version changes when the public schema changes.

## Consequences

### What gets easier

Operators can see recent planner/rest observations without confusing them with execution cycles.

### What gets harder

The remote and local readers, typed schema, privacy canaries, freshness behavior, and projection version must remain synchronized. The 15-minute freshness boundary is presentation policy, not evidence of runtime health; it may not align with the actual bridge cadence.

### What does not change

The existing approved state root and reader identity are reused. The cycle feed remains execution-only. No host deployment, permission change, or runtime mutation is authorized.

## Alternatives Considered

### Add planning rows to the cycle feed

Rejected: it would fabricate cycles from a different event type and alter existing cycle semantics.

### Show only the newest cycle and add no planning status

Rejected: it leaves known planning/rest observations invisible and cannot distinguish absent data from current rest-held data.

### Do nothing

Rejected: the observed mismatch remains unexplained to dashboard users, although the system must still avoid claiming activity or cause without evidence.

## Test Contract

| Claim in Decision | Test | Currently |
|---|---|---|
| Rest-held status is distinct from actual cycle rows | `tests/test_planning_activity.py::test_rest_observation_renders_separately_from_cycles` | not yet written |
| Missing/stale/error inputs remain unknown | `tests/test_planning_activity.py::test_source_freshness_boundary_and_unavailable_states` | not yet written |
| Raw wake reference/snapshot/free text is withheld | `tests/test_planning_activity.py::test_rest_projection_withholds_private_fields` | not yet written |
| Local and remote readers agree | `tests/test_planning_activity.py::test_local_remote_reader_parity` | not yet written |
| Schema change invalidates publish digest | `tests/test_planning_activity.py::test_projection_version_changes_publish_digest` | not yet written |

## Rollback

Revert the dashboard projection, reader, and renderer change and bump/revert the projection version through the normal code review path. No runtime state is changed; previously published static snapshots remain until the ordinary publisher replaces them.

## References

- `scripts/techtree_viewer.py`: `REMOTE_READER_SCRIPT`, local mirror, `render_public_pages()`
- `scripts/two_sinks.py`: `PUBLIC_DATA_KEYS`, `_PUBLIC_SCHEMA`, `PROJECTION_VERSION`
- Issue #395 research record: https://github.com/ozand/eeebot-ops-dashboard/issues/395#issuecomment-5981707778
- Issue #395 freshness decision update: https://github.com/ozand/eeebot-ops-dashboard/issues/395#issuecomment-5981740215
- Issue #395 and its linked sanitized research/freshness decision comments.
- Issue #395
