# D2 private cycle pages — rollout decision and preflight

**Status:** preparation only; not merge, host-inspection, or rollout authorization.
**Scope:** dashboard #316 / PR #318 D2 private cycle-detail delivery. This is separate from #383/#394 and #396/#397, whose plan explicitly excludes D2.

## Evidence checkpoint — 2026-10-12

- PR [#318](https://github.com/ozand/eeebot-ops-dashboard/pull/318) merged on 2026-10-06 as `bd2ea5b0e57757403d002992469e5e3e0c236fcc`, from final reviewed head `cddeea28bafdc2aed05d4170093270b82d1cff23`. Issue #316 remains open in `status:roll-out`.
- [Exact-head CI 37411764660](https://github.com/ozand/eeebot-ops-dashboard/actions/runs/37411764660) passed. The PR records independent final-head review with no findings, 1470 passed / 9 skipped in the full local suite, and 237 passed / 1 skipped in focused modules. These are delivery receipts, not tests rerun for this documentation update.
- The ambiguous prompt-ownership follow-up is integrated: a prompt matching multiple attempts is retained once as unassigned/incomplete; all affected attempts and the cycle remain history-incomplete.
- The D2 implementation places private cycle pages and its query router in the host snapshot; public publication receives a separate public-page map. The cycle-detail reader is in the sync manifest. The existing public `cycle.html` renderer is not evidence that private D2 pages are active.
- Supporting PRs #383, #398, #401 and #403 are merged. Issue #402 records a naturally scheduled successful publisher invocation at generator `ea035fb`, with all eight manifest hashes matching and the absent-root host sink unconfigured. This proves the recorded flat-import/public-only case, not private activation.
- Repository checkpoint: master `2be4e2bbc488aa0072421be31ceda67d83eaa2b6`, [CI 37776470448](https://github.com/ozand/eeebot-ops-dashboard/actions/runs/37776470448) passed (1457 passed, 57 skipped). That CI does not establish host audience, authentication, or private serving.

## Facts still UNKNOWN

No private-D2 host inspection or activation evidence is recorded in this checkpoint. Therefore installed generator/revision, effective sync drop-in and publisher wiring, manifest contents on host, service/site-root state, access controls and actual served pages are all **UNKNOWN**. Do not infer them from repository files or #397's #383/#394 evidence.

## Decision gates

1. **Code gate (delivered):** #318 and the supporting fixes are merged with recorded CI evidence. Before a rollout, select and verify the actual target revision, its manifest, and successful checks; do not reuse the former open-PR checkpoint. Issue #316 still requires acceptance/rollout evidence. The documented auto-sync can select a changed master on a later publisher run; current installed host state must not be inferred from a merge.
2. **One bounded read-only preflight approval:** before any host contact, obtain an explicit operator decision naming the host, identity, commands/read scope, and permitted read-only evidence across the interdependent #383/#394/#398 host-sink work and D2 #316/#318 privacy delivery. #397 supplies a staged-gate pattern for #383/#394 only; it does not itself authorize this combined D2 preflight. No PR merge or operational action is included in this read permission.
3. **Read-only preflight (only after that approval):** inspect only approved generator/source SHA and exact manifest-listed files, effective sync drop-in and publisher wiring, site-root state, service bind/listener and access-control boundary, and the minimum snapshot metadata needed to assess whether `cycles/<id>.html` and the `cycle.html?id=…` route are confined to the intended audience. Distinguish network/listener reachability from proxy policy and application authentication/authorization; HTTP status or a reachable route alone does not prove audience restriction. Do not fetch/render or copy private page payloads. Never read credentials, env contents, prompts, cycle payloads, or unrelated host files. Record sanitized hashes/statuses only. Any unapproved or unavailable fact remains UNKNOWN; stop if host-only access cannot be established.
4. **Rollout decision:** present preflight evidence, remaining unknowns, exact target revision, conditional auto-sync consequence, privacy boundary checks, and rollback artifact identity to the operator. Require a new explicit authorization naming each permitted operational action. No publisher invocation, restart, deployment, permission change, or public publication is implied by preflight approval.
5. **Post-action verification/rollback:** only if separately authorized, verify host-only route/page availability without exposing/capturing private payloads and confirm public `gh-pages` excludes private pages/data. Stop on any public leakage, audience/access mismatch, revision mismatch, or missing rollback artifact. Rollback also requires explicit authorization; restore only the identified prior generator and every matching manifest asset, then verify hashes.

## Stop conditions

No explicit scope-specific approval; unknown or inconsistent source/revision/access evidence; inability to establish host-only access; any private content in public output; missing verified rollback artifacts; or request to inspect secret-bearing configuration. Treat an effective auto-sync hook as a possible merge-triggered generator change, not a harmless background detail. Report unavailable evidence rather than attempting broader access or inventing a check.

## Not performed

This documentation refresh performed no host contact, publisher invocation, restart, permission/service change, private activation, or deployment. Historical code merges are recorded above; they are not private-rollout evidence. Live host state remains UNKNOWN. #397 is referenced only as a staged-gate process pattern; its evidence and authorization do not cover D2.
