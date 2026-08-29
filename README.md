# eeebot Ops Dashboard

Local observability dashboard for eeebot.

Canonical source:
- **this repository (`ozand/eeebot-ops-dashboard`) is the canonical home** since
  2026-07-05 (decision: `ozand/eeebot#617`). The code was extracted from
  `ozand/eeebot` `ops/dashboard/` via subtree split with full history and this
  copy supersedes anything that previously lived there.
- status: dormant / not deployed. The dashboard actually running on the eeepc
  host is a different program (`scripts/eeebot_dashboard.py` in `ozand/eeebot`,
  port 8080). This app (port 8787, WSGI + SQLite + collectors) is the more
  capable candidate replacement; deploying it is a separate decision.

Compatibility note:
- the imported dashboard still exposes the `eeebot-ops-dashboard` project identity where that is user-facing
- many local paths, services, package names, and collected control artifacts still carry `nanobot` names for compatibility with the existing runtime and deployed host
- those internal names should be migrated only in a separate controlled compatibility tranche

Purpose:
- run on this host, not on eeepc
- ingest live eeepc self-evolving state over SSH
- ingest local repo-side eeebot bounded-runtime state
- keep historical snapshots in SQLite
- present a local web UI for current and past cycles, goals, promotions, approvals, deployments, and any available subagent telemetry

Current scope of v1:
- overview page with latest-source summaries, blocker analysis, quick links, and compact status-emphasized timelines
- hypotheses/backlog page with HADI + explicit WSJF and execution spec visibility
- cycle history page with visible filter form and PASS/BLOCK/unknown badges
- promotions page with visible filter form and promotion-status badges
- approvals/capability page
- deployments/verification page with repo-vs-eeepc divergence visibility
- experiments page with reward, budget, credits summary, used-call visibility, and task linkage
- credits ledger page
- system files page for eeepc goal/system files and local repo docs
- analytics page with status-emphasized counters, recent snapshots, recent cycles, failure-class breakdown, streaks, top-goal frequency, top BLOCK reasons, artifact history, and recent goal transitions
- subagents page with durable task/goal/cycle correlation visibility
- compatibility service units for both names:
  - `nanobot-ops-dashboard-*.service`
  - `eeebot-ops-dashboard-*.service`
- `/api/summary` machine-readable endpoint
- `/api/cycles` machine-readable history endpoint
- `/api/promotions` machine-readable history endpoint
- `/api/approvals` machine-readable operational endpoint
- `/api/deployments` machine-readable deployment/proof endpoint
- `/api/system` machine-readable system/current-proof endpoint
- `/api/analytics` machine-readable analytics endpoint
- `/api/hypotheses` machine-readable HADI/WSJF backlog endpoint
- `/api/plan` machine-readable task-plan/reward endpoint
- `/api/experiments` machine-readable experiments/budget/credits endpoint
- `/api/credits` machine-readable credits ledger endpoint
- autonomy control artifacts for project ownership, status-heartbeat transparency, escalation thresholds, and execution roles

The dashboard is intentionally dependency-light:
- Python stdlib
- SQLite
- Jinja2
- system ssh/scp

Quick start:

```bash
cd /home/ozand/herkoot/Projects/nanobot/ops/dashboard
PYTHONPATH=src python3 -m nanobot_ops_dashboard init-db
PYTHONPATH=src NANOBOT_EEEPC_SUDO_PASSWORD='<set-in-env-file>' python3 -m nanobot_ops_dashboard collect-once
PYTHONPATH=src NANOBOT_EEEPC_SUDO_PASSWORD='<set-in-env-file>' python3 -m nanobot_ops_dashboard serve --host 127.0.0.1 --port 8787
```

Then open:
- `http://127.0.0.1:8787/`

Tech-tree viewer:
- `scripts/techtree_viewer.py` is a standalone, stdlib-only operator page, separate from the collector/SQLite/web-app path above. It does one SSH round-trip to `eeepc`, reads the self-evolving loop's direction portfolio, trust ladder, hypothesis verdicts, and evolution-tree history, and renders a self-contained, Civilization-style static HTML page (no external assets, opens as `file://`). Every source is read fail-soft: a missing/corrupt state file renders an "unavailable" panel instead of crashing the page.
- run it with:
  ```bash
  python scripts/techtree_viewer.py --host eeepc --open
  ```
  flags: `--host` (default `eeepc`), `--out` (default `techtree.html`), `--open` (launch the default browser), `--publish` (also push the snapshot to GitHub Pages — https://ozand.github.io/eeebot-ops-dashboard/ — via the authenticated `gh` CLI; the repo and therefore the page are public)

More details:
- `docs/SHOWING_THE_DASHBOARD.md`
- `docs/operations/2026-04-24-eeebot-ops-dashboard-baseline.md`

Tech-tree publish pipeline (issue #101):

The published GitHub Pages site (https://ozand.github.io/eeebot-ops-dashboard/)
runs from a host copy of the generator at `/opt/eeebot-techtree/` on the `eeepc`
host, not directly from this repo. The pipeline from source to published site is:

```
repo master (scripts/techtree_viewer.py + techtree_autopublish.py)
    │
    │  scripts/deploy_generator.sh --host eeepc-lan
    ▼
/opt/eeebot-techtree/{techtree_viewer.py,techtree_autopublish.py}
    │
    │  eeepc-self-evolving-subagent-bridge.service OnSuccess= drop-in
    │  (digest-gated: only publishes when tree sources change or staleness
    │   floor exceeded — see scripts/techtree_autopublish.py)
    ▼
https://ozand.github.io/eeebot-ops-dashboard/  (gh-pages branch)
```

Deploying the generator after a repo change:

```bash
# From the operator's workstation (key-based SSH to eeepc-lan required):
scripts/deploy_generator.sh --host eeepc-lan

# Dry-run first to preview what will happen:
scripts/deploy_generator.sh --host eeepc-lan --dry-run

# Running directly on the eeepc host (no --host needed):
scripts/deploy_generator.sh
```

The script:
1. Captures and prints the short git sha of the version being deployed.
2. Backs up the prior `/opt` copy with a UTC timestamp.
3. Copies both scripts via `scp` (or `install` when running locally).
4. Runs `python3 -m py_compile` on the target copies — aborts on syntax error.
5. Triggers one `eeebot-techtree-publish.service` run so the new generator
   is exercised immediately.

The published page footer displays the generator git sha. Verify deployment
by comparing the footer sha with the source commit printed by this script.

First-time setup (before running `deploy_generator.sh`):
1. Run `scripts/install_techtree_publish.sh` (as root on the host) to create
   `/opt/eeebot-techtree/`, the `eeebot-publish` system user, and the systemd unit.
2. Create `/etc/eeepc-agent/techtree-publish.env` (root-owned, 0600) with
   `GH_TOKEN=<your fine-grained PAT>`.

Canonical runtime assets included:
- `scripts/run_web.sh`
- `scripts/run_collector.sh`
- `scripts/install_user_units.sh`
- `scripts/deploy_generator.sh`
- `scripts/eeepc_reachability_watchdog.py`
- `systemd/nanobot-ops-dashboard-web.service`
- `systemd/nanobot-ops-dashboard-collector.service`

Project links:
- Main repo: `https://github.com/ozand/eeebot`
- Dashboard repo: `https://github.com/ozand/eeebot-ops-dashboard`
