# D2 private cycle pages — rollout decision and preflight

**Status:** preparation only; not merge, host-inspection, or rollout authorization.
**Scope:** dashboard #316 / PR #318 D2 private cycle-detail delivery. This is separate from #383/#394 and #396/#397, whose plan explicitly excludes D2.

## Evidence available (repository/PR only)

- #316 is open in `status:test`; PR #318 is open at reviewed head `2ee05286ac9ee2cd2be8ee70ad3246b93d22ea19`, base `d8b0ea4dac6234def35dc9e7917b9a5b4dd3cd45`.
- Exact-head CI run `37244588236` passed. The full D2 review found no blocker; focused tests passed 247, 1 skipped. Historical Codex review comments remain; thread disposition is not verified.
- The reviewed implementation places cycle pages and the query router in the host snapshot; the public publisher receives the separate public-page map. The cycle-detail reader is present in the deployment sync manifest.
- These facts establish code/CI evidence only. They do not establish merged code, installed files, current host state, or successful private-page serving.

## Facts still UNKNOWN

No D2 host inspection has been authorized or performed. Therefore installed generator/revision, effective sync drop-in and publisher wiring, manifest contents on host, service/site-root state, access controls and actual served pages are all **UNKNOWN**. Do not infer them from repository files or #397's #383/#394 evidence.

## Decision gates

1. **Code gate:** resolve #318 review-thread dispositions, verify current exact PR head/base and CI, and complete #316 acceptance. Merge is a separate operator decision; a green check or this plan is not merge permission. If the sync hook described in repository docs is installed/effective, a merge may become consequential on the next publisher run; its live status is UNKNOWN and must not be inferred.
2. **One bounded read-only preflight approval:** before any host contact, obtain an explicit operator decision naming the host, identity, commands/read scope, and permitted read-only evidence across the interdependent #383/#394/#398 host-sink work and D2 #316/#318 privacy delivery. #397 supplies a staged-gate pattern for #383/#394 only; it does not itself authorize this combined D2 preflight. No PR merge or operational action is included in this read permission.
3. **Read-only preflight (only after that approval):** inspect only approved generator/source SHA and exact manifest-listed files, effective sync drop-in and publisher wiring, site-root state, service bind/listener and access-control boundary, and the minimum snapshot metadata needed to establish whether `cycles/<id>.html` and the `cycle.html?id=…` route are reachable only by the intended LAN audience. Do not fetch/render or copy private page payloads. Never read credentials, env contents, prompts, cycle payloads, or unrelated host files. Record sanitized hashes/statuses only. Any unapproved or unavailable fact remains UNKNOWN; stop if host-only access cannot be established.
4. **Merge/rollout decision:** present preflight evidence, remaining unknowns, exact target revision, conditional auto-sync consequence, privacy boundary checks, and rollback artifact identity to the operator. Require a new explicit authorization naming each permitted merge or rollout action. No publisher invocation, restart, deployment, permission change, or public publication is implied by preflight approval.
5. **Post-action verification/rollback:** only if separately authorized, verify host-only route/page availability without exposing/capturing private payloads and confirm public `gh-pages` excludes private pages/data. Stop on any public leakage, audience/access mismatch, revision mismatch, or missing rollback artifact. Rollback also requires explicit authorization; restore only the identified prior generator and every matching manifest asset, then verify hashes.

## Stop conditions

No explicit scope-specific approval; unknown or inconsistent source/revision/access evidence; inability to establish host-only access; any private content in public output; missing verified rollback artifacts; or request to inspect secret-bearing configuration. Treat an effective auto-sync hook as a possible merge-triggered generator change, not a harmless background detail. Report unavailable evidence rather than attempting broader access or inventing a check.

## Not performed

No host probe/access, merge, publisher invocation, deployment, restart, permission/service change, or publication was performed to prepare this document. Live host state remains UNKNOWN. #397 is referenced only as a staged-gate process pattern; its evidence and authorization do not cover D2.
