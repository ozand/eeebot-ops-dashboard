# ADR-005: Versioned work-unit catalog and observation contracts

- **Status:** Proposed
- **Date:** 2026-10-13
- **Decision owner:** operator only; no acceptance is recorded by this document.
- **Author:** browser dashboard writer lane.
- **Registration issue:** #428; governing implementation scopes: #421, #423, #425.
- **Independent acceptance:** #427, with its own claims, exact-SHA checks and coordinated runs.
- **Implementation reference:** `45e36eb925f601446328cbc4052f6bd6be0e5410`.
- **Documentation base:** `2cd2928229cd370613c83855f949f71e123dd447` (`master`).
- **Supersedes:** no numbered ADR. This formal proposal carries forward the unnumbered discussion draft in #421, comment 6134245389; that comment remains historical evidence, not a previously accepted decision.

## Context and reason for this record

The work-unit pilot persists authored definitions as `work-units/v1` and collected observations as `work-unit-observations/v1`. Persisted data format/schema decisions require an ADR under the operator-confirmed governance rule recorded in #428. Being standalone, offline or reversible is not an exemption. The change proposal and `observations.md` describe implementation intent, but do not supply architectural acceptance.

The existing dashboard ADRs cover different subjects: ADR-001 covers lineage projection, ADR-002 covers rendering recorded agent-context figures, and ADR-004 covers planning/rest observations. They do not decide this catalog's ownership, independent format versions, compatibility/rejection rules or cache binding. This record does not amend their decisions or statuses.

The operator relayed pG's source-only review of the exact implementation SHA: privacy/observation negative cases CLEAR, without local tests, and a remaining documentation/governance gap. That report is recorded in #428 and #427. It is not represented here as an independently repeated review, a test pass, a vulnerability allegation or operator acceptance.

### Actual chronology, not a pre-code ADR

| Existing implementation | Immutable head | PR opened (UTC) |
|---|---|---|
| #421 / PR #422: catalog, descriptors, validator | `e43fba4315d24b7eb87c2012a090c4f177161272` | 2026-10-12 17:31:04 |
| #423 / PR #424: observations and replay | `405eb57d7c5959cd20ecf2246e5895a9169c3053` | 2026-10-12 17:35:37 |
| #425 / PR #426: offline consumer and handoff | `45e36eb925f601446328cbc4052f6bd6be0e5410` | 2026-10-12 17:40:12 |

An unnumbered ADR draft was subsequently posted in #421 on 2026-10-12. This numbered proposal is written on 2026-10-13, **after the pilot implementation already existed on feature branches**. The request to implement a pilot, PR wording about operator approval, source review and CI are not evidence of a pre-code accepted ADR. This record neither backdates approval nor erases that process gap.

### Scope and allocation

#428 records the duplicate/scope check and reservation of **dashboard ADR-005**. At the checked master, the local ADR directory/index contained 001, 002 and 004; no conflicting 005 reservation was found in issues or PRs. This is the dashboard repository's namespace, not the runtime namespace. The 003 gap is not reused.

#421 and #423 retain their implementation tasks; #425 retains its consumer task; #427 remains independent acceptance. Registration uses a separate two-file docs branch from master so it does not move or supersede the exact implementation stack being reviewed. It is not a second implementation PR for any of those issues and not a shadow backlog.

## Proposed decision: record the existing contracts without changing them

The following captures the current v1 field sets and documented semantics for operator disposition. It adds no field, enum, source, permission, migration or executable behavior. A normative claim below is an obligation to verify, not a claim that every negative case is already covered; the claim-to-test contract records the limits.

### 1. Ownership and sources of truth

| Object | Writer / owner | Reader and authority boundary |
|---|---|---|
| Authored `work-units/v1` catalog | Reviewed repository configuration in #421; schema validator `work_unit_catalog.py` | Observation collector and offline preview read it. It describes purpose, criteria and references, not current task state or acceptance. |
| `work-unit-observations/v1` snapshot | Explicit invocation of the #423 collector, or a deliberately supplied operator-controlled replay | `validate_snapshot()` checks structure and binding; preview consumes it. It is timestamped local data, not a signed attestation or a new status authority. |
| Existing derived `work-unit-index/v1` output | #425 `build_index()` and preview output | Human/offline consumer only in this pilot. It is not an input cache, a new collector source or independent proof. |
| Task state and workflow labels | Canonical GitHub issues and existing labels | Observations preserve what was read and when. Authored descriptors do not override it. |
| PR state / workflow-attempt metadata | The corresponding GitHub object | A link, reported run head or successful status does not establish actual checkout, test totals, acceptance or deployment. |
| Architectural acceptance | Operator | Separate explicit decision; neither writer lane nor schema validator can supply it. |

The organizational model stays Context / Work / Result as **roles of references**, not lifecycle stages or three physical directories. Source material retains one canonical location and a reference can serve different roles in different units. A relation confers neither read permission nor instruction authority. An ongoing area is not complete merely because a linked issue closes.

### 2. Catalog v1

The top-level object has exactly `schema` and `units`, with schema `work-units/v1`. A unit has exactly `id`, `kind`, `title`, `goal`, `boundaries`, `acceptance`, `visibility`, `links`, `related_units`. Here **`acceptance` is a list of criteria, never an acceptance verdict**. Kinds are `project`, `area`, `resource`; visibility is `public` or `private`.

A link has exactly `role`, `label`, `visibility`, `target`. Roles are `context`, `work`, `result`. Targets are structured:

| Kind | Required target fields besides `repo`, `kind` |
|---|---|
| `issue`, `pr` | `number` |
| `commit` | `sha` |
| `file` | `sha`, `path` |
| `run` | `number`, `attempt`, `expected_sha` |

Only `ozand/eeebot-ops-dashboard` and `ozand/eeebot` are in the existing repository allowlist. File/commit versions are full lowercase 40-hex SHAs; run identity includes its explicit attempt and expected head SHA. GitHub-backed unit IDs resolve to a linked issue identity; local IDs use the existing `eeebot:<kind>:<slug>` pattern. Unsupported repositories/kinds, unsafe repository paths, duplicate IDs or role/target pairs, dangling/self relations, unknown fields and unsupported schemas are rejected, not repaired by inference.

Existing bounds remain: 1 MiB JSON input, 1..32 units, at most 64 links per unit, 1..16 criteria/boundaries, at most 32 related IDs, 512-character text, 120-character link labels and 180-character unit IDs. Object identifiers are strict integers in 1..10^15, not booleans. Duplicate JSON keys, nonfinite constants, malformed encoding/shape and unsafe text must not become valid input. File paths remain repository references, not local filesystem paths.

No operational `status`, `progress`, `accepted`, `deployed` or test-count fields are introduced into definitions. URLs are constructed from validated targets; arbitrary linked content is not recursively fetched or executed.

### 3. Observations v1 and typed facts

The top-level object has exactly `schema`, `catalog_sha256`, `records`, with schema `work-unit-observations/v1`. Records are keyed by canonical target identity. A record has exactly `checked_at`, `probe`, `last_success`; a non-null last success has exactly `observed_at`, `facts`.

The existing probe vocabulary is `present`, `unavailable`, `not_found_or_inaccessible`, `invalid`, `mismatch`, `budget_exhausted`, `not_collected`. A partial record map is permitted, but no key outside the current public target set is permitted. A missing record is shown as uncollected/unknown, not proof of absence. File/commit references are not fetched by this collector.

| Target | Exact persisted facts |
|---|---|
| Issue | `state`, `workflow_labels` |
| PR | `state`, `merged`, `head_sha` |
| Workflow attempt | `run_id`, `attempt`, `head_sha`, `status`, `conclusion`, `checkout_sha` |

The issue-label allowlist stays `status:discovery`, `status:in-progress`, `status:test`, `status:roll-out`. Multiple allowed labels are retained without inventing a resolution. PR merging requires the explicit `merged` value; `merge_commit_sha` is not a substitute. Run ID, attempt, repository/object URL and expected SHA are validated against the target; `checkout_sha` remains null because actual checkout is not observed by this adapter. Existing status/conclusion enums remain owned by `work_unit_observations.py` at the implementation reference, not widened by documentation.

Raw titles, bodies, arbitrary labels/URLs and exception text are discarded rather than becoming persisted facts or public labels. No test totals or acceptance are extracted from prose. A schema-valid replay does not authenticate its author, provenance or truth: input snapshots must come from trusted operator-controlled storage. No new trust/ingestion path is authorized.

### 4. Version compatibility, rejection and cache semantics

The catalog and snapshot have independent, exact schema identifiers. The current reader accepts its known v1, rejects unsupported versions/fields and performs **no implicit migration, best-effort downcast or unknown-field preservation**. This is strict matching, not a forward-compatibility promise.

`catalog_sha256` binds a snapshot to the deterministic public catalog produced by `public_digest()`; it is not a digest of the raw file or an authenticity signature. Object keys are sorted with compact JSON serialization; list order is retained. Changes to included public definitions/references invalidate the binding. Excluded private-only content does not participate. The separate catalog CLI digest must not be substituted for the observation binding.

An incompatible or invalid previous snapshot fails visibly before collection; the CLI does not silently reset it, fetch against changed definitions or replace an existing output. Starting an explicitly requested fresh capture without `--previous` is a new capture, not a migration or acceptance of the rejected cache.

For a valid previous snapshot, a source failure preserves `last_success` with its original `observed_at`, while recording the new `checked_at` and failure probe. The failure does not become hidden because the historical evidence is recent. HTTP 404 means not found **or inaccessible**, not proven absence. Identity mismatch, invalid input and exhausted budget remain explicit.

Snapshot output is size-bounded and written via a same-directory temporary file, file flush/fsync and atomic replacement. Its parent directory must already exist. There is no default host-state location or service. This does not claim directory-fsync/power-loss durability. CLI outcomes remain 0 for successful dynamic checks, 2 for a saved snapshot with source failures, and 1 for controlled invalid/unreadable input/output. Known exception-normalization review findings are not claimed fixed by this ADR.

### 5. Time and transport boundaries

Timestamps require the existing timezone-aware parser and are normalized for comparison. At the explicit assessment instant, an observation age below 900 seconds is `recent_at_assessment`, age at or above 900 is `stale`, absent evidence is `unknown`, and a future last-success or future latest-check timestamp gives `clock_skew`. Last check, last success and assessment are distinct. These are static assessment semantics, not a live health verdict, timer SLA or automatic publication schedule.

Collection is explicitly enabled by `--collect`, using unauthenticated public GitHub GETs at constructed endpoints, without redirect following, recursive traversal or automatic retries. Rendering performs no network requests. Existing limits remain: default 32/max 64 requests, default 60/max 120 seconds of request-admission budget, at most 10 seconds socket timeout per request, 1 MiB per response/snapshot. The admission budget is not a hard wall-clock bound on DNS or slowly streaming reads; no stronger timeout mechanism is introduced here.

### 6. Public projection and the existing derived index

Only explicitly public units/links and relations to visible units survive the projection. Private definitions must not be committed to this public repository. Visibility is reviewed configuration, not an automatic secret detector; even names, paths, IDs and edges can carry private information. Copies/projections must not mutate source definitions.

`build_index()` already emits `work-unit-index/v1` with `schema`, `assessment_at`, `units`. It enriches public links with constructed `reference_id`, `url` and the assessed `observation` (including `checked_at`), and units with `acceptance_evidence` and `deployment_evidence`, both `not_connected`. This records the existing #425 output; it creates no third input authority, importer or new schema. Offline HTML is rendered from that built index, not arbitrary replayed index JSON.

The preview does not infer acceptance, deployment or current runtime state. Its semantic digest removes the three time fields (`assessment_at`, `checked_at`, `observed_at`) while retaining assessed semantic facts/states, so a freshness transition can change it. It is an offline comparison helper, **not** a production publication gate. No production publisher import, sync-manifest entry, host read, permission or runtime/prompt behavior changes.

## Claim-to-test contract

This is a static mapping to test definitions at the exact implementation SHA, **not executed results, independent review or proof of complete coverage**. `C`, `O`, `P` denote the pinned test files linked under Sources. Parameterized tests are named by function, not expanded into invented case counts. p16 coordinates independent acceptance; pG owns privacy review; pF runs only a coordinated unique test scope recorded in #427. Reading these definitions to write this ADR does not take over their work.

| Claim | Required observable contract | Existing named test definition(s) | Coverage limit / required evidence |
|---|---|---|---|
| C01 | Roles are reference roles; definitions cannot store operational verdicts | C: `test_pilot_has_three_units_and_reuses_canonical_references`; `test_catalog_cannot_become_a_second_status_store` | Defined, not rerun. Human acceptance is not inferred from either. |
| C02 | Canonical IDs, pinned files/run attempt, safe paths and valid relations | C: `test_file_versions_are_pinned`; `test_identifiers_are_strict_integers`; `test_run_reference_is_one_expected_revision_and_attempt`; `test_repository_paths_never_become_local_or_arbitrary_urls`; `test_identity_and_relation_invariants` | Existing cases are not a claim of every possible invalid shape. |
| C03 | Exact versions/fields and bounded parsing reject incompatible input with safe diagnostics | C: `test_unknown_repositories_and_missing_visibility_fail_closed`; `test_json_parser_rejects_ambiguous_or_invalid_input`; `test_bounds_and_safe_error_messages`; `test_cli_returns_error_for_bad_catalog`; O: `test_invalid_cache_is_rejected_not_promoted_to_evidence` | No specifically named unsupported-version or full per-limit boundary test is present in these files. Those obligations still need exact evidence; oversized-integer normalization is an existing external review item. |
| C04 | Private units, IDs, labels and edges never enter public outputs; projection is isolated | C: `test_private_units_links_and_relationships_do_not_escape`; O: `test_private_sources_never_requested`; P: `test_private_labels_ids_relations_and_content_absent_in_both_outputs` | Synthetic definitions; source-only pG CLEAR is not a local execution receipt. |
| C05 | Collection is opt-in, deduplicated, fixed-endpoint, bounded and unauthenticated | O: `test_reads_only_six_unique_dynamic_public_references`; `test_no_redirects_and_endpoint_is_constructed`; `test_transport_get_is_bounded_unauthenticated_and_does_not_echo_payload`; `test_atomic_snapshot_roundtrip_and_no_network_by_default` | Injected transport is not a live-network or hard-deadline test. |
| C06 | A run is bound to the expected identity/attempt/head; no checkout/merge/test-total inference | O: `test_mismatched_run_never_becomes_success`; `test_pr_merge_commit_is_not_a_merge_or_checkout_proof`; `test_reads_only_six_unique_dynamic_public_references` | Named mismatch parameters cover ID, attempt and SHA; do not label all repository/URL mismatch combinations tested. |
| C07 | Invalid schema/cache/facts/time cannot be promoted into evidence or overwrite saved output | O: `test_invalid_cache_is_rejected_not_promoted_to_evidence`; `test_invalid_previous_file_does_not_replace_good_output`; `test_raw_wrong_shapes_are_unknown_not_crashes` | Unsupported schema identifiers need separately identified coverage, not inferred from generic invalid-cache cases. |
| C08 | Public catalog binding is deterministic; excluded private edits do not invalidate it | C: `test_digest_does_not_depend_on_dict_key_order`; O: `test_static_private_changes_do_not_invalidate_public_snapshot`; `test_invalid_cache_is_rejected_not_promoted_to_evidence` | Catalog digest and public binding are distinct. A changed-public-definition roundtrip is not separately named here. |
| C09 | A failed latest read retains original-time success and visible failure | O: `test_error_preserves_historical_success_without_fresh_claim`; P: `test_failed_latest_probe_does_not_hide_old_observation` | Existing parameters cover OSError, HTTP 403/404, not the external HTTPException review finding. |
| C10 | 899/900/901 and future evidence produce exact assessed states | O: `test_exact_freshness_boundary`; P: `test_semantic_change_not_clock_only_change_invalidates_comparison_digest` | The O test's future case ages both times together; independent future-check/old-success evidence is not separately claimed. |
| C11 | Budget exhaustion is not absence; admission stops when its deadline expires | O: `test_budget_does_not_fabricate_absence`; `test_deadline_stops_admitting_network_requests` | No claim about hard OS DNS/read deadlines. |
| C12 | Conflicting allowed workflow labels remain visible | O: `test_multiple_workflow_labels_are_preserved_without_resolving_conflict` | Do not infer a resolved task state or broaden label policy. |
| C13 | Snapshot writing roundtrips; rendering stays offline and reports unconnected evidence | O: `test_atomic_snapshot_roundtrip_and_no_network_by_default`; P: `test_roles_and_uncertainty_appear_without_network`; `test_cli_does_not_overwrite_and_invalid_snapshot_fails_closed` | Roundtrip does not prove crash/power-loss durability. Derived index is not a snapshot import format. |
| C14 | Safe HTML, working anchors and keyboard/narrow layout | P: `test_markup_is_escaped_and_anchor_targets_exist`; `test_browser_layout_roles_and_keyboard_index` | Browser definitions are not a run receipt; page-language review finding remains separately owned by the writer. |
| C15 | Facts/freshness changes affect the comparison digest; clock-only changes within a state do not | P: `test_semantic_change_not_clock_only_change_invalidates_comparison_digest` | Offline helper only; no production invalidation or deployment claim. |

Missing evidence in this table is not waived, silently converted to a pass or patched by this docs change. Existing automated findings are [JSON integer handling in #422](https://github.com/ozand/eeebot-ops-dashboard/pull/422#discussion_r4250735676), [HTTP protocol failures in #424](https://github.com/ozand/eeebot-ops-dashboard/pull/424#discussion_r4250772421), and [page language in #426](https://github.com/ozand/eeebot-ops-dashboard/pull/426#discussion_r4250807133). This record neither reproduces nor adjudicates/resolves them. Any coordinated fixes remain with the existing writer and PRs; no test/code changes are bundled here.

## Alternatives and consequences

Keeping only the proposal was insufficient: it left the persisted-format decision unregistered. Putting mutable status or approval into authored definitions would duplicate GitHub truth. Treating a digest/schema-valid snapshot as authenticated evidence would confuse compatibility with provenance. An arbitrary-URL collector, new graph store or root-routing redesign is outside the already bounded pilot.

The recorded design keeps a small offline consumer and an opt-in collector, but requires explicit version matching, conservative cache rejection, trusted snapshot storage and visible uncertainty. It does not establish usability improvements. Registration addresses the missing formal proposal/index; it does not make the implementation accepted, repair review findings or authorize production use.

## Acceptance, supersession and exact-SHA rules

1. **Operator-only acceptance.** Status remains Proposed until an explicit operator disposition identifies this document revision, scope and unresolved qualifications. Neither author comments, CI nor pG's scoped CLEAR change it to Accepted. The post-implementation chronology remains visible even after a later decision.
2. **No current numbered supersession.** ADR-001/002/004 retain their existing text/status. This document is the formal successor to the unnumbered #421 discussion draft only; the historical draft is not deleted or portrayed as accepted.
3. **Future changes are explicit.** Unsupported fields/enums/versions remain rejected by v1. An incompatible future format or a change to ownership, trust, cache binding/rejection or read scope needs a separately scoped decision and producer/consumer compatibility or migration plan. Do not silently reinterpret old snapshots or claim a new version works with old readers. If a future ADR supersedes this one, name the exact replaced sections and operator decision, update the index then, and preserve historical decisions. No migration is introduced now.
4. **Bug fixes do not erase evidence boundaries.** Restoring an existing v1 obligation is not automatically a schema expansion, but a code change still has a new SHA and requires coordinated evidence for that change. Reviewers/operator decide disposition; old-SHA results must not be relabelled as new-SHA acceptance.
5. **Docs SHA is not implementation SHA.** This proposal's commit is based on master and adds only the ADR and index row; it is not a descendant build of the pilot stack and must not replace #427's `45e36eb925f601446328cbc4052f6bd6be0e5410` test target. A documentation revision supersedes the previous text revision, not the tested code. Before any later stack edit/rebase, coordinate with p16 and publish the new exact heads.
6. **No implied rollout or broadened scope.** No merge, deploy, issue closure, schema/code/host-read changes or root-policy edits are authorized. The separate stops for dashboard #375/#369, #386 with runtime #2058/ADR038 trust prerequisite, #395 with ADR-004 rest-source approval, and #381 owner decision remain unaffected. Their readiness is not reassessed here.

There is no operational rollback from this two-file proposal because no running state is changed. A future integration/rollback decision remains separate. #428 is a documentation gate; #427 and operator acceptance remain independent gates. GitHub remains the sole task/status/claim source, not this ADR table.

## Sources at the recorded implementation revision

Implementation/test links are pinned because the docs branch does not contain the unmerged pilot. No live source reads or test executions were performed to register this record.

- [S1: Catalog code](https://github.com/ozand/eeebot-ops-dashboard/blob/45e36eb925f601446328cbc4052f6bd6be0e5410/scripts/work_unit_catalog.py).
- [S2: Observation code](https://github.com/ozand/eeebot-ops-dashboard/blob/45e36eb925f601446328cbc4052f6bd6be0e5410/scripts/work_unit_observations.py).
- [S3: Preview/index code](https://github.com/ozand/eeebot-ops-dashboard/blob/45e36eb925f601446328cbc4052f6bd6be0e5410/scripts/work_unit_preview.py).
- [S4: Original pilot proposal](https://github.com/ozand/eeebot-ops-dashboard/blob/45e36eb925f601446328cbc4052f6bd6be0e5410/docs/changes/work-unit-pilot/proposal.md) and [observation contract](https://github.com/ozand/eeebot-ops-dashboard/blob/45e36eb925f601446328cbc4052f6bd6be0e5410/docs/work-units/observations.md).
- [C: Catalog test definitions](https://github.com/ozand/eeebot-ops-dashboard/blob/45e36eb925f601446328cbc4052f6bd6be0e5410/tests/test_work_unit_catalog.py).
- [O: Observation test definitions](https://github.com/ozand/eeebot-ops-dashboard/blob/45e36eb925f601446328cbc4052f6bd6be0e5410/tests/test_work_unit_observations.py).
- [P: Preview test definitions](https://github.com/ozand/eeebot-ops-dashboard/blob/45e36eb925f601446328cbc4052f6bd6be0e5410/tests/test_work_unit_preview.py).
- [Original ADR index](https://github.com/ozand/eeebot-ops-dashboard/blob/2cd2928229cd370613c83855f949f71e123dd447/docs/adr/INDEX.md), [unnumbered draft](https://github.com/ozand/eeebot-ops-dashboard/issues/421#issuecomment-6134245389), [registration scope/reservation](https://github.com/ozand/eeebot-ops-dashboard/issues/428), [independent acceptance and routing](https://github.com/ozand/eeebot-ops-dashboard/issues/427).
