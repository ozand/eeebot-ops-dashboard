# ADR-007: Publish bounded token-data incompleteness

- **Status**: Proposed
- **Date**: 2026-11-07
- **Target issue**: #433
- **Decision owner**: Repository owner
- **Implementation status**: Not implemented; contract review required before source changes

## Context
Issue #433 requires a malformed token row to be distinguishable from a known zero and an absent day, without treating invalid input as a confident zero. The local and embedded readers feed the same public token heatmap. The current strict `_TOKEN_HEATMAP` summary allowlist discards an extra `invalid_days` field, so a reader-only implementation cannot reliably expose an incomplete-day indicator through the public projection. This ADR proposes the smallest typed metadata extension needed to satisfy that criterion; it does not authorize host reads, publishing, or deployment.

## Decision proposed
Add optional `token_heatmap.summary.invalid_days`, a sorted, unique list of MSK calendar-date strings (`YYYY-MM-DD`) for dates with one or more timestamp-valid rows whose token fields cannot be interpreted under the accepted input rules. Each date must be a real Gregorian date, must occur in that heatmap's retained `dates` list, and the list must be no longer than the retained window (at most 62 dates). Both local and embedded readers produce the same field under equivalent input. The public typed projection validates the list and rejects the field value as a whole if its type, dates, uniqueness/order, or retained-window membership is invalid; it does not coerce, truncate, or publish malformed values. Older payloads without the optional field remain compatible; absence means **completeness unknown**, not proven complete. The UI may show an incomplete notice only for a date explicitly present in validated `invalid_days`.

## Input semantics proposed
- A nonnegative integer `total_tokens`, including `0`, is the measured row token count.
- Only when `total_tokens` is absent may the reader derive a count, and only if both `prompt_tokens` and `completion_tokens` are nonnegative integers (booleans excluded); the derived count is their sum.
- A present but malformed/negative/unsupported `total_tokens` is invalid; do not fall back to component counts.
- When `total_tokens` is absent and either component count is absent or malformed, the row is invalid.
- Invalid rows contribute neither token category nor total nor call count and are not converted to zeros. Their valid timestamp's MSK date is recorded in `invalid_days` so the day summary is marked incomplete.
- Rows with invalid timestamps remain excluded from date attribution; the malformed date itself is never emitted.

## Typed projection and disclosure boundary
The new field is fixed-form aggregate metadata only. Do not publish raw rows, model/component values from invalid records, parser error text, source paths, or private payloads. Existing summary numeric/category fields retain their contracts. Treat the field as untrusted input at the typed allowlist, validating exact list/string shape and Gregorian date validity plus sorted/unique/window membership. Rejecting malformed metadata must not create a confident complete-day signal; old payloads with no field retain the existing display contract and must not be reinterpreted as explicitly complete. This is a data-minimization boundary, not a general privacy certification.

## Alternatives considered
1. **Keep malformed-row state only inside each reader:** rejected because it is lost at the strict public projection; the UI cannot distinguish malformed rows from ordinary zero calls.
2. **Overload `days_missing`, `total_calls`, or a category number:** rejected because it corrupts established meanings and makes the figure misleading.
3. **Publish raw malformed rows or error reasons:** rejected as unnecessary exposure and new schema surface; one fixed-format date marker suffices for the required summary.
4. **Leave the original AC partial/unmet:** safe fallback if this proposal is not accepted, but does not meet #433's malformed-row distinction.

## Costs and compatibility
The optional field changes the public typed projection and therefore its version/digest contract may require an explicit version bump according to the existing projection policy. Old payloads remain parseable, but cannot assert completeness from field absence. New readers and the projection must agree on the field semantics; local/embedded divergence can otherwise recreate the defect. Validation and UI tests add maintenance cost, bounded to the token heatmap contract. No new telemetry collection, inference, cost estimation, model-routing logic, retention expansion, or raw-data publication is proposed.

## Test contracts (proposed, not implemented)
- `test_token_heatmap_invalid_days_projection_accepts_retained_sorted_dates`: fixed valid date list accepted only when sorted, unique, Gregorian-valid, and contained in retained `dates`.
- `test_token_heatmap_invalid_days_projection_rejects_bad_shape_and_out_of_window_values`: absent optional field stays absent; `null`, scalar, malformed date, duplicate/unsorted date, outside-window date, and over-bound list are rejected without coercion or leakage.
- `test_token_heatmap_local_and_embedded_reader_parity`: run the embedded standalone reader against a temporary synthetic state root and compare with local reader; valid nonzero, known zero, derivable pair, incomplete fallback, malformed total, and negative values yield same categories, total, calls, and invalid dates.
- `test_token_heatmap_invalid_rows_excluded_and_day_marked_incomplete`: invalid rows contribute no tokens or calls, but mark the valid timestamp's MSK date; malformed timestamps do not create a date.
- `test_tokens_day_summary_renders_incomplete_only_for_validated_invalid_date`: actual rendered browser detail shows category totals and actual calls, labels explicitly invalid dates incomplete, and does not invent completeness for old payloads missing the field.
- Existing boundary contract remains: `20:59 UTC` maps to `23:59 MSK` on that date; `22:00 UTC` maps to `01:00 MSK` the next date.

## Acceptance and authority
This is a proposal only. Independent exact-revision review and explicit acceptance/release of this contract are prerequisites to modifying the public schema or completing #433 AC2. No public verification, host access, deploy, or publication is authorized by this ADR.
