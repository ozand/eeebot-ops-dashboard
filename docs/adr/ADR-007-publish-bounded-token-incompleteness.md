# ADR-007: Publish bounded token-data incompleteness

- **Status**: Proposed
- **Date**: 2026-11-07
- **Target issue**: #433
- **Authors**: Pi coding assistant (AI-generated draft under delegated implementation assignment)
- **Supersedes**: None
- **Related ADRs/contracts**: [ADR-006: Keep dashboard presentation evidence-first and visual guidance in DESIGN.md](ADR-006-evidence-first-interface-and-design-source.md); `scripts/two_sinks.py` typed public projection contract
- **Decision owner**: Repository owner
- **Implementation status**: Partial local-reader implementation exists in pre-ADR commit `a3a7b2e30b9647fa8d0f540da49edc5c6fc17c77`; embedded remote-reader work remains uncommitted in the worktree; public-schema field is not implemented. ADR review is blocked pending this correction; no proposed contract has been accepted. This proposal is not retrospective approval of the earlier implementation or gate chronology.

## Context
Issue #433 requires a malformed token row to be distinguishable from a known zero and an absent day, without treating invalid input as a confident zero. The local and embedded readers feed the same public token heatmap. The current strict `_TOKEN_HEATMAP` summary allowlist discards an extra `invalid_days` field, so a reader-only implementation cannot reliably expose an incomplete-day indicator through the public projection. A partial local-reader implementation in `a3a7b2e30b9647fa8d0f540da49edc5c6fc17c77` predates this ADR; an embedded remote-reader mirror remains uncommitted and the public-schema field has not been implemented. That earlier work is disclosed chronology, not retroactive gate approval. The current contract review is blocked pending this draft correction; no public-schema implementation is authorized until the proposal is independently reviewed and explicitly accepted. This ADR does not authorize host reads, publishing, or deployment.

## Decision
**Proposed; not accepted.** Add optional `token_heatmap.summary.invalid_days`, a sorted, unique list of MSK calendar-date strings (`YYYY-MM-DD`) for dates with one or more timestamp-valid rows whose token fields cannot be interpreted under the accepted input rules. Each date must be a real Gregorian date, must occur in that heatmap's retained `dates` list, and the list must be no longer than the retained window (at most 62 dates). The input `dates` list itself must be an ordered contiguous calendar window of at most 62 valid Gregorian dates; validate against that bounded list rather than trusting an arbitrary producer-provided span. Both local and embedded readers produce the same field under equivalent input. The public typed projection validates the list and rejects invalid metadata without coercion or truncation. Older payloads without the optional field, and rejected/absent invalid metadata, mean **completeness unknown**, not proven complete. A day with only invalid token rows remains an observed day with zero accepted tokens/calls plus an explicit incomplete marker; a known zero-token row remains a valid zero-token call and does not mark the day invalid. The UI may show an incomplete notice only for a date explicitly present in validated `invalid_days`.

This proposal does **not** authorize raw-row publication, new source access, retention changes, model-routing changes, host operations, publication, or deployment.

## Consequences
- **Easier:** the day-detail UI can distinguish known zero, unobserved day, and an observed day whose token accounting is incomplete, using bounded public metadata; producer parity is testable.
- **Harder:** local and embedded readers plus the typed projection must maintain one shared semantic contract; projection validation and versioned publication behavior require focused tests.
- **Unchanged:** numeric category/total/call definitions, UTC input to MSK day bucketing, existing retained history window, and exclusions on telemetry/model routing/raw data.
## Input semantics proposed
- A nonnegative integer `total_tokens`, including `0`, is the measured row token count.
- Only when `total_tokens` is absent may the reader derive a count, and only if both `prompt_tokens` and `completion_tokens` are nonnegative integers (booleans excluded); the derived count is their sum.
- A present but malformed/negative/unsupported `total_tokens` is invalid; do not fall back to component counts.
- When `total_tokens` is absent and either component count is absent or malformed, the row is invalid.
- Invalid rows contribute neither token category nor total nor call count and are not converted to zeros. Their valid timestamp's MSK date is recorded in `invalid_days` so the day summary is marked incomplete.
- Rows with invalid timestamps remain excluded from date attribution; the malformed date itself is never emitted.

## Typed projection and disclosure boundary
The new field is fixed-form aggregate metadata only. Do not publish raw rows, model/component values from invalid records, parser error text, source paths, or private payloads. Existing summary numeric/category fields retain their contracts. Treat `invalid_days` as untrusted at the typed allowlist: validate exact list/string shape, real Gregorian dates, canonical order, uniqueness, retained-window membership, and the `dates` list's own maximum length/contiguity. If the field is absent or fails validation, omit it; consumers must treat incompleteness as unknown, not as complete. Do not substitute another field or publish an error detail. This is a data-minimization boundary, not a general privacy certification.

## Alternatives considered
1. **Keep malformed-row state only inside each reader:** rejected because it is lost at the strict public projection; the UI cannot distinguish malformed rows from ordinary zero calls.
2. **Overload `days_missing`, `total_calls`, or a category number:** rejected because it corrupts established meanings and makes the figure misleading.
3. **Publish raw malformed rows or error reasons:** rejected as unnecessary exposure and new schema surface; one fixed-format date marker suffices for the required summary.
4. **Leave the original AC partial/unmet:** safe fallback if this proposal is not accepted, but does not meet #433's malformed-row distinction.

## Costs and compatibility
The field is an additive change to the public projection output domain. `scripts/two_sinks.py` documents `PROJECTION_VERSION` as part of the publish digest and requires a bump whenever that output domain changes (`PROJECTION_VERSION = "d1.1-typed-2"` at the base). Therefore implementation **must bump** the projection version to the next project-consistent value when adding the field; `tests/test_d11_typed_projection.py::test_projection_version_change_changes_the_publish_digest` verifies that a version change alters the publisher digest. Do not change the digest algorithm or add a separate digest input. Old payloads with the field absent remain parseable and completeness unknown. No telemetry collection, inference, cost estimation, model-routing logic, retention expansion, raw-data publication, or host operations are proposed.

## Test contracts (proposed, not implemented)
- `tests/test_two_sinks.py::test_token_heatmap_invalid_days_projection_accepts_retained_sorted_dates`: fixed valid date list accepted only when sorted, unique, Gregorian-valid, and contained in a valid retained `dates` list of at most 62 contiguous dates.
- `tests/test_two_sinks.py::test_token_heatmap_invalid_days_projection_rejects_bad_shape_and_out_of_window_values`: optional field absent remains absent/unknown; `null`, scalar, malformed date, duplicate/unsorted date, outside-window date, over-bound list, and invalid `dates` window are rejected without coercion or private-string leakage.
- `tests/test_techtree_viewer.py::test_token_heatmap_local_and_embedded_reader_parity`: run the embedded standalone reader against temporary synthetic state (no SSH) and compare with local reader; valid nonzero, known zero, derivable pair, incomplete fallback, malformed total, and negative values yield the same categories, total, calls, and invalid dates.
- `tests/test_techtree_viewer.py::test_token_heatmap_invalid_rows_excluded_and_day_marked_incomplete`: invalid rows contribute neither tokens nor calls, but mark the valid timestamp's MSK date; malformed timestamps do not create a date; invalid-only day remains observed and incomplete, not confidently complete.
- `tests/test_techtree_viewer.py::test_tokens_day_summary_renders_incomplete_only_for_validated_invalid_date`: actual rendered browser detail shows category totals and actual calls, labels explicitly invalid dates incomplete, preserves known zero vs missing, and does not assert completeness for old payloads lacking the field.
- Existing `tests/test_d11_typed_projection.py::test_projection_version_change_changes_the_publish_digest` applies to the required version bump; no test implementation is claimed by this proposal.
- Existing boundary contract remains: `20:59 UTC` maps to `23:59 MSK` on that date; `22:00 UTC` maps to `01:00 MSK` the next date.

## Acceptance and authority
This is a proposal only. Independent exact-revision review and explicit acceptance/release of this contract are prerequisites to modifying the public schema or completing #433 AC2. No public verification, host access, deploy, or publication is authorized by this ADR.
