# Local-agent handoff: work-unit pilot

## Scope and ownership

Repository work is staged in three reviewable PRs, not deployed:

1. `feat/421-work-unit-contract`: contract, three descriptors, validator.
2. `feat/423-work-unit-observations`: stacked observations and cache contract.
3. `feat/425-work-unit-preview`: stacked offline cards and this handoff.

Resolve the final head from the PR delivery receipt and verify it before testing.
Do not merge the stack into an intermediate feature branch merely to run it: the
last branch already contains the earlier slices. After each approved merge,
retarget/rebase the remaining PRs carefully, then re-run CI for the exact new head.
Never reuse an earlier SHA's acceptance after a code change.

The local agent owns independent review, network/environment verification,
approved-only installation inventory and operator usability evidence. This file
is a handoff, not a report that a local agent has started or completed those tasks.
It authorizes no new private file access, permission change, service start,
publication, merge, rollout or prompt modification.

## A. Workstation: clean checkout and tests

Use a disposable workstation environment, not the constrained production host.
Preserve unrelated work. From a clean clone:

```sh
git status --short --branch
git fetch origin
git worktree add --detach /tmp/eeebot-work-unit-review origin/feat/425-work-unit-preview
cd /tmp/eeebot-work-unit-review
git rev-parse HEAD
git diff --stat 2cd2928229cd370613c83855f949f71e123dd447...HEAD
```

Expected changes: only the new `work_unit_*.py` modules/tests and work-unit design
files. No existing publisher, viewer, workflow, sync manifest, runtime, systemd
unit, credentials or private-state file changes. Stop if the reviewed diff differs.

Use the dependencies and pinned versions from `.github/workflows/test.yml` in a
clean environment. Install the matching Playwright browser on the workstation,
not eeepc. Then:

```sh
python scripts/work_unit_catalog.py docs/work-units/pilot.json
PYTHONPATH=. python -m pytest tests/test_work_unit_catalog.py tests/test_work_unit_observations.py tests/test_work_unit_preview.py -q -rs
PYTHONPATH=. python -m pytest tests -q -rs
```

Record exact head, environment, commands, totals, skips and warnings from actual
logs. A missing browser must not become accepted browser coverage. GitHub CI fails
on missing Playwright/Chromium; a non-CI workstation reports explicit skips.
A workflow success is not an independent review or proof of installed behavior.

## B. Optional public GitHub capture and offline replay

Only these public repositories are approved by the pilot catalog. This client
uses no token and makes GET requests only. Review the descriptors before enabling
network access. Never put credentials in commands or a snapshot.

```sh
OUT=$(mktemp -d)
python scripts/work_unit_observations.py docs/work-units/pilot.json \
  --collect --output "$OUT/observations.json"
# Exit 2 means saved with source failures; inspect it, do not call it all-success.
python scripts/work_unit_preview.py docs/work-units/pilot.json \
  --snapshot "$OUT/observations.json" --out "$OUT/preview"
# No-snapshot mode must also remain useful and honest:
python scripts/work_unit_preview.py docs/work-units/pilot.json \
  --out "$OUT/unobserved"
```

Open the local `preview/index.html` and `unobserved/index.html`. Do not copy them
into any production site root. The HTML and JSON are preview outputs, not approved
public artifacts. All timestamps are as-of; there is no background refresh.

Check all three cards at narrow and desktop widths, keyboard index links, pinned
source versions, separately shown latest probe and last successful observation.
Check #418 as finite work, #395 as a decision-dependent unit, and publishing
reliability as an ongoing area. API metadata alone must not claim actual checkout,
full test coverage, acceptance, deployment or live runtime state.

Check rate-limit/network failures, a failed latest check with preserved history,
and incompatible input snapshots. Do not replace failures with fabricated data.
Any supplied snapshot is operator-trusted input, not a signed proof. Keep private
samples local; public reports may contain only approved identifiers and metadata.

## C. Installation inventory (separate authorization boundary)

Only perform already-authorized read-only checks. Record installed generator
revision, the actual serving program, installation layout, update mechanism,
and public/private audience boundaries. Do not read `planner/rest_state.json`,
credentials, prompts or new private paths for this pilot. Do not dump service
environments or private payloads into a public issue.

The README describes master auto-sync. Verify whether it is active; do not assume
that avoiding a manual deployment makes a merge operationally harmless. This
pilot is deliberately absent from the sync manifest and live rendering path.
If necessary information cannot be read under existing authorization, record the
gap rather than widening access or inferring host state.

## D. Independent acceptance and usability pilot

Assess five questions using the same frozen material set with the current
navigation and with the cards. Counterbalance order; keep operator and agent
results separate:

- Find the purpose and baseline code version for the digest work.
- Identify what a pinned workflow attempt actually proves and what remains unknown.
- Determine whether there is evidence of deployment, without treating a PR as proof.
- Identify the unapproved rest-source boundary in the planning unit.
- Explain why an ongoing publishing area is not complete when a task closes.

Record time, correct answers, missing constraints, wrong version selections,
unnecessary copies and time spent maintaining the index. Do not invent achieved
speedups. First acceptance requires zero false acceptance/deployment/permission
claims in these probes and no forced duplicate source documents. Set a measured
retrieval target against the observed baseline before claiming efficiency gains.
Keep linked-knowledge/context-assembly experiments out of this comparison.

## E. Production integration: NOT performed by this stack

Only after independent acceptance, installation inventory and an explicit owner
rollout decision, prepare a separate integration PR. That PR must decide the
approved observation source/capture cadence, public/private schema, page allowlist,
scan rules, installed files/import layout, and publication invalidation behavior.
Test semantic changes, unchanged data, missing/error transitions and stale-page
handling through the real publisher with external writes stubbed. The standalone
`semantic_digest` helper is not a production freshness or publish policy.

Name the exact artifact/commit to install and record the pre-change serving state.
Use the existing approved deployment mechanism. Verify serving and rollback
separately from code CI. No blanket restart, sudo, directory permission widening,
new host reader or runtime state mutation is authorized by this document.
Rollback must restore the prior approved generator/serving configuration, preserve
previous artifacts and remove only the new integration. For the standalone pilot,
rollback is simply not running it; no service was installed.

## Return receipt

Return: issue/PR; tested head and actual checkout revision; environment; commands
and outcomes; skipped/not-executed checks; approved source scope; public-safe
findings; installed revision only if actually observed; acceptance decision only
if issued by its authority; remaining blocker and proposed next action. Link full
logs privately when necessary; never publish their secret-bearing content.
