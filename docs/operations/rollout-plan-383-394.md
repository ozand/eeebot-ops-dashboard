# Rollout preparation — dashboard host sink (#383 / #394)

**Status:** preparation plan only; not merge or rollout authorization.
**Scope:** `ozand/eeebot-ops-dashboard#383` / #394 only. Excludes #395 and D2/#316.

## Current evidence and uncertainty

- #383 is open; current reported candidate head `4f8747d51c5f97800873df78fc50d2c5ccd4a050` targets `master` at `b1fb1d1fccab591f16a7660e7f2bd63310aaed48`. Re-fetch and verify both immediately before any later decision; neither value is a future merge SHA.
- #394 is open and explicitly excludes deployment/host validation. This document does not amend that boundary.
- Repository docs say the publisher unit may have an `ExecStartPre` sync drop-in. If installed, the next publisher invocation can fetch the `master` manifest, pin a SHA, download/compile files, then replace the installed generator. This makes merge potentially operationally consequential.
- **Installed drop-in, installed generator SHA, live site-root state, and effective systemd `ReadWritePaths` are UNKNOWN.** No host access was performed to prepare this plan. Do not turn repository documentation into a live-state claim.

## Staged authorization gates

1. **Code gate:** #394 acceptance, exact-head review, and CI. Code merge is separate from host rollout approval.
2. **Explicit class-3 approval:** before any host inspection, deployment, publisher invocation, restart, permission/path change, or rollout, obtain a separate operator authorization naming target and allowed read/write operations. An issue, PR merge, this plan, or a successful verify-only run is not that authorization.
3. **Read-only preflight (only after approval):**
   - Record current `master` SHA and candidate merge SHA; require the intended SHA to be merged and reviewed.
   - Inspect the specific installed publisher service/drop-in and sync script wiring; establish whether sync `ExecStartPre` is installed and active. If its status cannot be proven, stop and preserve `UNKNOWN`.
   - Record installed generator file revision/hash and compare it with the approved target. Verify manifest entries and compilation evidence without reading credentials or environment-file contents.
   - Verify site-root existence/type/ownership/mode and effective `ReadWritePaths`; do not create the root, chmod, repair, or guess paths during preflight.
   - Capture the current publisher invocation/result and the existing `current` snapshot identity as rollback baseline. Do not invoke the publisher unless a later explicit action authorization names it.
4. **Rollout decision:** if any preflight evidence is absent, inconsistent, the installed source is not the intended pin, or the sync mechanism is uncertain, stop. Request a fresh scoped operator decision; do not infer permission to fix the host.
5. **Post-action verification (only if separately authorized):** use the documented verify-only path before activation where applicable; after any approved rollout, check only safe public canaries/page status and snapshot version. Confirm private D2 cycle pages are never present on public `gh-pages`. Do not publish or quote private cycle payloads.
6. **Rollback (only if separately authorized):** restore the previous pinned generator files/assets from verified backups or immutable revision, verify their hashes against the preflight baseline, and run the documented validation. Never improvise file copying or permission changes. If rollback's preconditions are not met, stop and escalate.

## Stop conditions

- No explicit class-3 approval for the specific host operation.
- Unknown installed drop-in/service wiring, generator revision, site-root state, or effective `ReadWritePaths`.
- Target SHA mismatch, failed verify-only, missing backup/rollback identity, public/private boundary failure, or any request to expose credentials/private page content.
- Do not treat `gh-pages` freshness or code merge as proof that the host generator was updated.

## Not performed

No host probe, service/publisher invocation, merge, deployment, restart, permission change, issue/PR mutation, or public publication occurred while preparing this plan.
