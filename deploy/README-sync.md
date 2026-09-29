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

## Host steps (D4, orchestrator)

D1 only provides the unit files, the sync drop-in and the tmpfiles entry; it
does not install anything. The orchestrator performs the host cutover after
D3 gate #1978. Every step below is class 3 (owner: ozand), run from a checkout
containing the merged repository artifacts. The publisher credential file
must already be provisioned through the approved host process; never print
its contents.

**No trigger is paused.** The publisher has two independent triggers: its
timer, and the bridge unit's `OnSuccess=eeebot-techtree-publish.service`
(drop-in `eeepc-self-evolving-subagent-bridge.service.d/20-techtree-publish.conf`),
which fires after every bridge run. Neither is touched. Instead, the
installation is made safe for ANY trigger at ANY moment:
- Until the single `daemon-reload` at the end of step 3, systemd keeps the
  previously LOADED publisher configuration. A trigger in that interval runs
  the legacy configuration exactly as it does today, even though the files on
  disk have already changed.
- After that reload, any trigger (the manual start in step 5, the timer, or
  the bridge's OnSuccess) runs the new configuration: sync, then the new
  generator. That first run IS the seed.
- A trigger that arrives while `daemon-reload` itself is running is queued
  by systemd until the reload completes, so the reload is atomic with respect
  to triggers.

The order matters:
- The site root exists BEFORE the reload (step 1). `/var/lib/eeebot-site`
  is created by systemd-tmpfiles (`deploy/eeebot-site.tmpfiles.conf`, `0755
  eeebot-publish`), not by the unit. The unit's `ReadWritePaths=` is set up
  with its namespace, before any `ExecStartPre`, and is deliberately not
  optional (`-`), so a missing site root fails the unit loudly. The site root
  is not a `StateDirectory=`: `StateDirectoryMode=` is one mode for all of a
  unit's state directories, and `/var/lib/eeebot-techtree` must stay `0700`.
  sync-manifest does not deliver the tmpfiles file (it installs generators
  only), so step 1 installs it explicitly. The directory is harmless to the
  legacy configuration, which never reads it.
- The unit, the sync drop-in and the sync script are all on disk BEFORE the
  one `daemon-reload` (step 3). Otherwise the first post-reload run could
  start without the sync and run the legacy generator, which never creates
  `current/index.html`.

Everything the publisher writes lives inside `/var/lib/eeebot-site`: snapshot
staging (`.staging/<version>`, renamed into place on the same filesystem) and
the publisher lock (`.publish.lock`). The server never serves a dot-named
path, a version directory, or `current` itself; it serves only files inside
the snapshot `current` points at. Never run both servers simultaneously; they
bind the same `:8080` port. The replacement server refuses to start unless
`current` points to a complete snapshot, so the seed must succeed before the
legacy server stops.

**Public effect of the cutover.** The seed (the first run after the step-3
reload) runs the NEW generator, and it publishes to gh-pages through the D1
public projection at the same time. This changes what is publicly visible.
Step 5 checks the served public page.

1. Create the site root with systemd-tmpfiles:

```bash
sudo install -o root -g root -m 0644 deploy/eeebot-site.tmpfiles.conf /etc/tmpfiles.d/eeebot-site.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/eeebot-site.conf
sudo stat -c "%a %U" /var/lib/eeebot-site
```

`stat` must print exactly `755 eeebot-publish`.

2. Take a dated backup of everything step 3 overwrites. On 2026-09-29 the host
   had `/opt/eeebot-techtree/eeebot-techtree-sync.sh` from #325 (sha256 prefix
   `afea3265`), and the drop-in directory already contained `20-timeout.conf`:

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
sudo cp -a /etc/systemd/system/eeebot-techtree-publish.service /etc/systemd/system/eeebot-techtree-publish.service.bak-$TS
sudo cp -a /opt/eeebot-techtree/eeebot-techtree-sync.sh /opt/eeebot-techtree/eeebot-techtree-sync.sh.bak-$TS
sudo cp -a /etc/systemd/system/eeebot-techtree-publish.service.d /etc/systemd/system/eeebot-techtree-publish.service.d.bak-$TS
sudo cp -a /etc/systemd/system/eeebot-dashboard.service /etc/systemd/system/eeebot-dashboard.service.bak-$TS
ls -la /etc/systemd/system/*.bak-$TS /etc/systemd/system/eeebot-techtree-publish.service.d.bak-$TS /opt/eeebot-techtree/*.bak-$TS
echo "$TS"   # keep: the rollback below uses it
```

systemd ignores `*.bak-$TS` files and the `*.service.d.bak-$TS` directory:
neither name matches `<unit>` or `<unit>.d`.

3. Install the publisher unit, the sync drop-in and the sync script (last), and
   only THEN reload, exactly once:

```bash
sudo install -o root -g root -m 0644 systemd/eeebot-techtree-publish.service /etc/systemd/system/eeebot-techtree-publish.service
sudo install -d -o root -g root -m 0755 /etc/systemd/system/eeebot-techtree-publish.service.d
sudo install -o root -g root -m 0644 deploy/eeebot-techtree-publish.service.d-sync.conf /etc/systemd/system/eeebot-techtree-publish.service.d/20-repo-sync.conf
sudo install -o root -g root -m 0755 deploy/eeebot-techtree-sync.sh /opt/eeebot-techtree/eeebot-techtree-sync.sh
sudo systemctl daemon-reload
```

4. Verify the effective unit:

```bash
sudo systemctl cat eeebot-techtree-publish.service
sudo systemctl show eeebot-techtree-publish.service -p User -p ProtectSystem -p StateDirectory -p StateDirectoryMode -p ReadWritePaths -p ExecStart -p LoadState -p FragmentPath -p DropInPaths
```

`systemctl cat` must contain the drop-in lines
`ExecStartPre=-+/opt/eeebot-techtree/eeebot-techtree-sync.sh` and
`ExecStart=/usr/bin/python3 /opt/eeebot-techtree/scripts/techtree_autopublish.py`.
`systemctl show` must report `User=eeebot-publish`, `ProtectSystem=strict`,
`StateDirectory=eeebot-techtree`, `StateDirectoryMode=0700` and
`ReadWritePaths=/var/lib/eeebot-site`.

5. Seed the host snapshot. A timer or bridge trigger may already have run the
   seed since step 3; the manual start below is then just one more run. Start
   it, wait until no run is active, and check the result:

```bash
sudo systemctl start eeebot-techtree-publish.service
while systemctl is-active --quiet eeebot-techtree-publish.service; do sleep 10; done
sudo journalctl -u eeebot-techtree-publish.service -n 50 --no-pager
sudo test -s /var/lib/eeebot-site/current/index.html
```

Verify that the NEW generator produced the snapshot. `add_snapshot_version`
(`scripts/two_sinks.py`) writes exactly one
`<meta name="snapshot-version" content="<version>">` into every public HTML
page, and `<version>` is the directory `current` points at:

```bash
sudo grep -o '<meta name="snapshot-version" content="[^"]*">' /var/lib/eeebot-site/current/index.html
V=$(sudo basename "$(sudo readlink /var/lib/eeebot-site/current)"); echo "$V"
```

The `grep` must print exactly one line, and its `content` must equal `$V`. No
line or a different value means the snapshot is not from the new generator:
stop here and do not cut over.

The seed also published gh-pages through the D1 projection. `publish_to_pages`
updates the `gh-pages` ref and returns without waiting for the Pages
deployment, and `publish_ordered` hands the publisher the versioned public
pages, so the served page carries the same meta. Wait until the served page
reports a snapshot version at least as new as the seed (at most 10 minutes).
A later trigger may legitimately publish a newer snapshot meanwhile, so accept
the seed's version or any NEWER one. A version is `<epoch>-<digest12>`
(`techtree_autopublish.py`), so compare the epoch parts numerically:

```bash
for i in $(seq 1 40); do
  S=$(curl -fsS "https://ozand.github.io/eeebot-ops-dashboard/?v=$(date +%s)" | grep -o '<meta name="snapshot-version" content="[^"]*">' | sed -E 's/.*content="([^"]*)".*/\1/')
  if echo "$S" | grep -qE '^[0-9]+-[0-9a-f]{12}$' && [ "${S%%-*}" -ge "${V%%-*}" ]; then echo "served: $S (seed $V)"; break; fi
  sleep 15
done
```

If it never prints `served: ...`, stop: the new projection is not yet what the
public sees. Once it does, open the public GitHub Pages site and check that it
renders and shows no private detail (no bridge error text, no file paths, no
prompt text) before going on.

6. Only now, cut the server over:

```bash
sudo install -o root -g root -m 0644 deploy/eeebot-dashboard-server.service /etc/systemd/system/eeebot-dashboard-server.service
sudo systemctl daemon-reload
sudo systemctl stop eeebot-dashboard.service
sudo systemctl enable --now eeebot-dashboard-server.service
sudo systemctl status eeebot-dashboard-server.service
```

### Rollback

Use the `$TS` printed in step 2.

- **Steps 1-5** (the publisher side): put the backups back and reload. The
  site root `/var/lib/eeebot-site` and the tmpfiles entry can stay; the legacy
  configuration never reads them, and nothing serves them until step 6.

```bash
while systemctl is-active --quiet eeebot-techtree-publish.service; do sleep 10; done   # never swap files under a running publisher
sudo cp -a /etc/systemd/system/eeebot-techtree-publish.service.bak-$TS /etc/systemd/system/eeebot-techtree-publish.service
sudo cp -a /opt/eeebot-techtree/eeebot-techtree-sync.sh.bak-$TS /opt/eeebot-techtree/eeebot-techtree-sync.sh
sudo rm -rf /etc/systemd/system/eeebot-techtree-publish.service.d
sudo cp -a /etc/systemd/system/eeebot-techtree-publish.service.d.bak-$TS /etc/systemd/system/eeebot-techtree-publish.service.d
sudo systemctl daemon-reload
sudo systemctl cat eeebot-techtree-publish.service   # must match the pre-cutover unit
```

  This rollback does not undo the gh-pages publication of the seed. The next
  run of the legacy generator republishes the old view, as expected.

- **Step 6** (the server side): stop the new server and start the legacy one
  again.

```bash
sudo systemctl disable --now eeebot-dashboard-server.service
sudo systemctl start eeebot-dashboard.service
sudo systemctl status eeebot-dashboard.service
```

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
