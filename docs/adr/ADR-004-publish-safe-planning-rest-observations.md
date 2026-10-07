# ADR-004: Publish planning/rest observations as a separate typed status

**Status**: Proposed
**Date**: 2026-10-04
**Authors**: Pi coding assistant
**Supersedes**: None
**Related**: ADR-002 (agent context reader contract), issue #395

## Context

The existing cycle feed is derived from allowlisted execution-cycle ledger phases. Planning sessions and planner rest-held observations are recorded in the same approved state root but are not actual execution cycles and must not be fabricated as such. A published page can therefore show an older cycle while a newer planning observation exists. Showing that observation requires a new public projection field, freshness semantics, and an explicit privacy boundary: rest state includes a raw wake-condition reference and snapshot value that must not be published.

The existing remote reader already reads `STATE_ROOT/ledger/cycles.jsonl`; the planner state is the adjacent `STATE_ROOT/planner/rest_state.json`, schema `planner-rest-v1`, owned by `nanobot/runtime/planner_rest.py` at the inspected runtime revision. This reuses the reader's existing `STATE_ROOT` authority and SSH execution identity but does add a new relative file access (`planner/rest_state.json`). Approval of the existing state root and SSH identity does NOT imply approval of this additional file read: operator approval of this ADR must explicitly include this path and its safe-field contract. The path remains confined beneath the already-approved state root; no new SSH identity, root, permission, or host probe is requested. Source fields are constrained to planning phases `planning_session`, `planner_rest`, `planner_rest_held`; outcomes `integrated`, `refused`, `malformed`, `no_plan`, `spawn_failed`, `commit_failed`, `timed_out`, `rest`, `rest_unchanged`; and rest fields `active_rest` presence, `wake_condition.kind` allowlisted by `VALID_WAKE_KINDS`, `deadline`, `consecutive_rests`, `held_ticks_since_last_session`, and `review_signal`. The reference, snapshot, free-text reason, and raw lines are excluded. The local reader accesses the same state-root-relative source under the caller-provided state root.

The evidence for the October 4 incident is a bounded read-only observation: planning outcome `rest_unchanged` and `planner_rest_held` entries existed while no new actual execution-cycle row was observed in the inspected window. This does not establish active work or why the candidate wake condition remains unchanged. Planning-event time and rest-file modification time are independent evidence sources; neither supersedes the other. If either source is stale, show its last recorded evidence and age, but current status for that source is unknown.

## Decision

### What this IS

The dashboard publishes one typed `planning_activity` observation separately from actual cycle rows. It contains the latest allowlisted planning outcome and timestamp, source status/mtime/age, plus validated rest-state fields: active flag, allowlisted wake-condition kind, deadline, bounded counters, and review-signal boolean. Validate the complete nested `active_rest` schema independently of the runtime's permissive loader: malformed shape or unknown fields/values make the observation unknown, never inactive by presence inference. `active_rest` may be a valid object or null; deadline must be a valid parsed timestamp or null; counters must be bounded non-negative strict integers (booleans rejected); review signal must be boolean or null; wake kind must match the exact allowlist. Each source has independent freshness; use a 900-second presentation freshness threshold as an explicit dashboard display policy. This is not proof of timer execution, runtime health, or an SLA. A stale rest file remains visible as last-recorded evidence, while current rest status is `unknown`.

### What this IS NOT

This is not a synthetic cycle, active-work indicator, runtime health/SLA, rest-policy judgment, wake-condition explanation, or proof that a candidate changed. It does not publish raw wake references, snapshot hashes, prompts, free-text reasons, or logs, and does not alter host reads/permissions or runtime behavior.

### Success criteria

Missing, malformed, unreadable, or stale inputs are explicitly absent/unavailable/unknown and never become inactive by default. Actual cycle rows retain their existing reader and presentation semantics. The typed public projection withholds unapproved fields and its version changes when the public schema changes.

## Consequences

### What gets easier

Operators can see recent planner/rest observations without confusing them with execution cycles.

### What gets harder

The remote and local readers, typed schema, privacy canaries, freshness behavior, and projection version must remain synchronized. Adding one file read expands the reader's source inventory and public projection surface; it requires explicit path-confinement and serialized-public-output tests. The 15-minute freshness boundary is presentation policy, not evidence of runtime health; it may not align with the actual bridge cadence.

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
| A valid active rest object exposes typed status; null means no active rest only when the enclosing schema/state is valid | `tests/test_planning_activity.py::test_rest_state_valid_object_and_null` | not yet written |
| Missing/stale/error inputs remain unknown, with independent source ages and exact 899/900/901-second boundary tests | `tests/test_planning_activity.py::test_source_freshness_boundary_and_unavailable_states` | not yet written |
| The new state-root-relative file read stays within `STATE_ROOT` and the serialized public allowlist excludes raw wake reference/snapshot/free text | `tests/test_planning_activity.py::test_planning_reader_confines_path_and_projection_withholds_private_fields` | not yet written |
| Local and remote readers agree on allowlisted fields and states | `tests/test_planning_activity.py::test_local_remote_reader_parity` | not yet written |
| Schema change invalidates publish digest | `tests/test_planning_activity.py::test_projection_version_changes_publish_digest` | not yet written |

## Rollback

Revert the dashboard projection, reader, and renderer change and bump/revert the projection version through the normal code review path. No runtime state is changed; previously published static snapshots remain until the ordinary publisher replaces them.

## References

- `scripts/techtree_viewer.py`: `REMOTE_READER_SCRIPT`, local mirror, `render_public_pages()`
- `ozand/eeebot` `nanobot/runtime/planner_rest.py` at inspected revision `6d476b715aa5989d5036e1385f6262d0f2a70f4b`: `_STATE_RELPATH`, `_SCHEMA`, `VALID_WAKE_KINDS`, `RestState`, `load_state()`.
- `ozand/eeebot` `nanobot/runtime/cycle_ledger.py` at the same inspected revision: `VALID_PLANNING_OUTCOMES`, `record_planning_session()`.
- `ozand/eeebot` `host/eeepc/systemd/eeepc-self-evolving-subagent-bridge.timer`: tracked `OnUnitActiveSec=15m`; this does not prove current installed timer behavior.
- `scripts/two_sinks.py`: `PUBLIC_DATA_KEYS`, `_PUBLIC_SCHEMA`, `PROJECTION_VERSION`
- Issue #395 research record: https://github.com/ozand/eeebot-ops-dashboard/issues/395#issuecomment-5981707778
- Issue #395 freshness decision update: https://github.com/ozand/eeebot-ops-dashboard/issues/395#issuecomment-5981740215
- Issue #395 and its linked sanitized research/freshness decision comments.
- Issue #395
