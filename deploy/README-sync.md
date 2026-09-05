# Tech-tree generator auto-sync

This is the repository-artifact portion of issue #136. It does not write to
the host. The operator applies it manually after the PR is merged.

## Behavior

`eeebot-techtree-sync.sh` first fetches `deploy/sync-manifest.txt` from
repository `master` (issue #210): the host's own copy of the manifest is not a
manifest entry and so could never update itself, which let one deleted entry
(#209 removed two vendored d3 files) deadlock every sync forever. The fetched
manifest is used when it is a recognisable manifest (at least one entry, only
`.py` / `.js` paths, no leading `/`, no `..`, CRLF tolerated); otherwise the
local copy `/opt/eeebot-techtree/sync-manifest.txt` is the fallback, and the
journal says which one was used and why. `SYNC_MANIFEST` overrides the fallback
path and, being an explicit operator choice, is read but never written back:

```
techtree sync: using master manifest (...)                       # normal
techtree sync: manifest fetch from master failed, falling back to local copy: ... (#210)
techtree sync: manifest fetched from master is empty or unrecognisable, falling back to local copy: ... (#210)
techtree sync: using local manifest (...)
techtree sync: local manifest copy updated from master (...)     # self-heal after a full success
```

After every named file has installed from a master manifest, that manifest is
written atomically (tmp + `mv`) over the local copy, so the fallback reflects
the last list proven to work. Every `curl` call is bounded (`--connect-timeout
10 --max-time 60`) and may not follow a redirect off https (`--proto-redir
=https`), so an unreachable GitHub degrades to the fallback instead of hanging
the unit. The residual case — GitHub unreachable *and* the local copy still
naming a deleted file — still fails the sync (nothing is installed, publish
proceeds on the existing generator), but with both lines above in the journal
rather than one. A 404 on an entry of master's own manifest (raw CDN behind a
push) also fails that one run; the next run, one publish interval later,
retries.

It then downloads every manifest entry over HTTPS, runs `python3 -m py_compile`
on each `.py` download, and only then touches any installed file. It creates one
UTC-timestamped `.bak` of each current file and atomically replaces each with
`mv`. Any download or compile failure occurs before replacement, leaves every
existing file untouched, and returns nonzero. The drop-in uses `ExecStartPre=-+...`:
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
