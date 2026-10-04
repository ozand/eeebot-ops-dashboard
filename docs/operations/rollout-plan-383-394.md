# Rollout preparation — dashboard host sink (#383 / #394)

**Status:** preparation plan only; not merge or rollout authorization.
**Scope:** `ozand/eeebot-ops-dashboard#383` / #394 only. Excludes #395 and D2/#316.

## Current evidence and uncertainty

- #383 is open; current reported candidate head `4f8747d51c5f97800873df78fc50d2c5ccd4a050` targets `master` at `b1fb1d1fccab591f16a7660e7f2bd63310aaed48`. Re-fetch and verify both immediately before any later decision; neither value is a future merge SHA.
- #394 is open and explicitly excludes deployment/host validation. This document does not amend that boundary.
- Repository docs say the publisher unit may have an `ExecStartPre` sync drop-in. If installed, the next publisher invocation can fetch the `master` manifest, pin a SHA, download/compile files, then replace the installed generator. This makes merge potentially operationally consequential.
- **Installed drop-in, installed generator SHA, live site-root state, and effective systemd `ReadWritePaths` are UNKNOWN.** No host access was performed to prepare this plan. Do not turn repository documentation into a live-state claim.

## Staged authorization gates

1. **Code gate:** #394 acceptance, exact-head review, and CI. Code merge is separate from host rollout approval. Because the installed sync drop-in is unknown, do not assume merge is operationally inert.
2. **Pre-merge read-only decision gate (requires separate explicit class-3 operator approval for host inspection):** before merging #383, gather a bounded read-only snapshot of the target host's relevant state. Until this permission is explicitly granted, all host values remain UNKNOWN. Host inspection approval is not merge approval.
   - Record current repository `master` SHA, PR #383 head SHA, and the prospective merge SHA only when GitHub exposes it. A future merge SHA cannot be predeclared.
   - Inspect only the named publisher unit and exact drop-in directory/entry for the sync `ExecStartPre`; record whether it is installed, loaded, and effective. Do not `systemctl cat` credential-bearing EnvironmentFiles or dump environment values.
   - Record installed generator file SHA-256/revision from manifest files and compare it with the planned target. Do not read credentials or environment-file contents.
   - Verify `/var/lib/eeebot-site` existence/type/owner/mode and effective `ReadWritePaths`; do not create the root, chmod, repair, or guess paths.
   - Identify the prior generator revision and the complete set of files named by the installed/approved manifest (including JS/CSS assets), plus backups or immutable retrieval for each; prove the whole set is available for rollback before any rollout decision. Do not invoke the publisher during this preflight.
3. **Merge decision:** if sync is installed/effective, treat merge as potentially triggering a host generator update on the next publisher run. Merge requires its own explicit operator decision after reviewing the pre-merge evidence, with the auto-sync effect and rollback readiness visible. If sync status, the complete prior manifest asset set, or rollback artifact is unknown, stop; do not infer approval or merge permission. No merge is authorized by the read-only preflight.
4. **Post-merge evidence gate (requires separate explicit class-3 approval for any host inspection):** pin the actual merge SHA and compare it with the pre-merge target. The dashboard repository has no dedicated `--verify-only` command for this generator. Its documented D4 procedure instead uses a staged sequence: sync/seed the host snapshot, verify the local and public snapshot-version markers, and inspect safe public canaries before the separately gated server cutover. Treat that as a host-changing rollout, not a verify-only check. Do not substitute runtime #2016's `deploy_release.sh --verify-only` (a different product). If the operator requires a no-host-mutation preactivation check, the current dashboard docs do not define one; record it as unavailable and request a separate design decision rather than inventing a command.
5. **Deployment decision and action (separate explicit class-3 authorization):** a further explicit operator decision must name whether the publisher's next run may perform the conditional auto-sync and/or whether a manual deployment is permitted. If authorized, follow only the approved route; verify the installed generator and manifest match the pinned merge SHA and inspect only safe public canaries/page status/snapshot version. Confirm private D2 cycle pages remain absent from public `gh-pages`; never publish or quote private cycle payloads.
6. **Rollback (separate explicit class-3 authorization):** before any deployment action, prove that the prior pinned generator and matching assets are available as verified backups or immutable revision. If rollback is authorized, restore those exact artifacts, verify their hashes against the pre-merge baseline, and use only a documented validation procedure. Never improvise file copying or permission changes. If prerequisites fail, stop and escalate.

## Bounded read-only preflight commands (execute only after separate class-3 approval)

The following are a command *plan*, not commands already run. Run from the approved operator session and redact host/path identifiers from any public record except the already documented fixed paths. Never print EnvironmentFile contents.

```sh
# Identify the publisher unit's loaded fragment/drop-in names and effective command/path properties only.
systemctl show eeebot-techtree-publish.service \
  -p LoadState -p FragmentPath -p DropInPaths -p ExecStartPre -p ExecStart \
  -p ReadWritePaths -p User -p Group -p StateDirectory

# Show only the exact known sync drop-in filename if present; do not enumerate unrelated unit files.
test -f /etc/systemd/system/eeebot-techtree-publish.service.d/20-repo-sync.conf \
  && printf 'repo-sync-dropin=present\n' || printf 'repo-sync-dropin=absent\n'

# Stat only the configured site root. Do not create/chmod it.
stat -c '%F %a %U:%G' /var/lib/eeebot-site

# Hash only the installed generator and manifest-listed generator files; never config/env files.
sha256sum /opt/eeebot-techtree/scripts/techtree_viewer.py \
  /opt/eeebot-techtree/scripts/techtree_autopublish.py \
  /opt/eeebot-techtree/scripts/two_sinks.py \
  /opt/eeebot-techtree/scripts/publish_scan.py
```

The above is intentionally narrow but does not itself prove that the installed sync script or manifest matches the canonical repository. Before any merge decision, obtain separate scoped approval to read the exact installed sync-script and manifest files as non-secret artifacts; record their SHA-256 and compare with the intended repository revision. If that file-level read approval is absent, or any manifest asset's prior version cannot be recovered, stop and keep rollout blocked. Do not read secrets or execute the sync script as part of preflight.

## Stop conditions

- No explicit class-3 approval for the specific host operation.
- Unknown installed drop-in/service wiring, generator revision, site-root state, or effective `ReadWritePaths`.
- Target SHA mismatch, failed approved verification/validation, missing backup/rollback identity, public/private boundary failure, or any request to expose credentials/private page content.
- Do not treat `gh-pages` freshness or code merge as proof that the host generator was updated.

## Not performed

No host probe, service/publisher invocation, merge of #383, deployment, restart, permission change, or public publication occurred while preparing this plan. The docs-only #396 issue and PR #397 were created to review this plan; those GitHub/documentation actions do not change the host authorization boundary.
