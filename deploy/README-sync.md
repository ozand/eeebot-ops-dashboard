# Tech-tree generator auto-sync

This is the repository-artifact portion of issue #136. It does not write to
the host. The operator applies it manually after the PR is merged.

## Behavior

`eeebot-techtree-sync.sh` downloads both standalone generators from repository
`master` over HTTPS, runs `python3 -m py_compile` on both temporary downloads,
and only then touches either installed file. It creates one UTC-timestamped
`.bak` of each current file and atomically replaces both with `mv`. Any
 download or compile failure occurs before replacement, leaves both existing
files untouched, and returns nonzero. The drop-in uses `ExecStartPre=-+...`:
`-` makes this pre-command failure non-fatal so publish proceeds with existing
generators; `+` runs the pre-command with full privileges. The service's
existing `User=eeebot-publish`, `ProtectSystem=strict`, credentials, and all
other sandbox settings are not changed.

The sync artifact does not clean old backups. During the manual host step,
retain only the newest `techtree_viewer.py.bak.*` and newest
`techtree_autopublish.py.bak.*` so `/opt/eeebot-techtree` has at most two
backups. If the sync fails after one file has been replaced, the other file's
preflight has already passed and the first replacement remains valid; the
next service invocation retries both. No source file is replaced after a
failed download or compile.

## Apply after merge

From a checkout containing the merged repository artifacts:

```bash
scp deploy/eeebot-techtree-sync.sh ozand@eeepc-lan:/tmp/eeebot-techtree-sync.sh
scp deploy/eeebot-techtree-publish.service.d-sync.conf ozand@eeepc-lan:/tmp/eeebot-techtree-publish.service.d-sync.conf
ssh ozand@eeepc-lan 'sudo install -o root -g root -m 0755 /tmp/eeebot-techtree-sync.sh /opt/eeebot-techtree/eeebot-techtree-sync.sh && sudo install -o root -g root -m 0644 /tmp/eeebot-techtree-publish.service.d-sync.conf /etc/systemd/system/eeebot-techtree-publish.service.d/20-repo-sync.conf && sudo systemctl daemon-reload && sudo systemctl cat eeebot-techtree-publish.service'
```

Verify `systemctl cat` contains the drop-in line:

```ini
ExecStartPre=-+/opt/eeebot-techtree/eeebot-techtree-sync.sh
```

Also verify that the base unit remains unchanged for `User=eeebot-publish`,
`ProtectSystem=strict`, credential mounts, and all other sandbox directives.
Do not print credential contents.

## End-to-end verification

After the operator applies the drop-in:

1. Record the merged commit and repository SHA-256 values for both raw `master`
   scripts.
2. Compare them with `sudo sha256sum /opt/eeebot-techtree/techtree_viewer.py /opt/eeebot-techtree/techtree_autopublish.py`.
3. Verify the `systemctl cat` drop-in and unchanged sandbox/credential lines.
4. Keep only the newest backup for each script; verify backup count `<= 2`.
5. Land a trivial merged comment-string change in this repo; do not manually
   copy generators to `/opt`.
6. Trigger `sudo systemctl start eeebot-techtree-publish.service`.
7. Inspect the journal for sync and publish results.
8. Verify the public GitHub Pages page contains the comment change and record
   the footer generated time/content check in UTC.
9. In a controlled operator-approved test, confirm a download/compile failure
   is logged, both existing `/opt` copies are unchanged, and publishing still
   proceeds because the `ExecStartPre` has the leading `-`.

Record actual host `sha256sum`, `systemctl cat`, backup count, journal, and
published-page evidence in issue #136. This repository change does not claim
that host application or end-to-end proof has happened.
