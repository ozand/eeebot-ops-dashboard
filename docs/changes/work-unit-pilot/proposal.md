# Work-unit pilot: Context / Work / Result

## Decision boundary

This is the operator-requested read-only pilot, not approval to add host readers,
change prompts, merge a branch, publish new pages or deploy. The three roles are
an interface to independent work, not lifecycle stages or three physical folders.
This product synthesis follows the operator-supplied organizational research;
its usefulness remains a hypothesis, not a scientifically established rule of three.

## First slice (issue #421)

`docs/work-units/pilot.json` describes exactly three units: digest work (#418),
planning observations (#395), and publishing reliability (an ongoing area).
The canonical validator is `scripts/work_unit_catalog.py`. No new dependency.

A unit has a stable ID, kind (`project`, `area`, `resource`), title, goal,
boundaries, acceptance criteria, explicit visibility, material links and related
units. Descriptions/criteria are authored configuration, not observed runtime facts.
Each link has a role (`context`, `work`, `result`), reviewed label, visibility and
structured target. A reference may serve different roles in different units;
source content has one canonical location. Relations do not grant permissions.

GitHub issue state and existing status labels remain the sole task/status store.
There is deliberately no `status`, `progress`, `accepted` or `deployed` field in
unit definitions. The area is not automatically complete when a child closes.
Future hierarchy must not rename IDs or force every file into a recursive unit.

## Reference contract

Only the explicitly approved repositories `ozand/eeebot-ops-dashboard` and
`ozand/eeebot` are accepted. Adding a repository is an explicit code-review change.
Supported targets: issue, PR, commit, file at a full commit SHA, and workflow run
at an explicit attempt and expected head SHA. Dynamic objects are observed as of
a timestamp; static references pin a revision. Links are not proof of existence,
acceptance, deployment, current state or successful tests. Run head identity is
not necessarily the checkout/merge commit; without job-log verification the
actual tested checkout remains unknown. Never derive test counts from PR prose.

URLs are constructed from validated targets, never fetched from arbitrary text.
File paths are repository paths, not local filesystem paths. No linked content is
executed or treated as an instruction. There is no implicit recursive traversal.

## Bounds and privacy

Limits: 1 MiB per JSON input, 32 units, 64 links per unit, 16 criteria/boundaries,
512 characters per text (120 for link labels), 40 lowercase hex characters per
commit SHA. IDs are strict positive integers, not booleans. Reject unknown fields,
duplicate JSON keys, nonfinite numbers, malformed versions, unsafe paths,
duplicate IDs/role-target links and unresolved unit relations. Diagnostic errors
contain fixed codes, not source data.

Only `public` units and links survive public projection; links between units
are filtered to visible IDs. Never commit private descriptors or tokens to this
public repository. Visibility is an explicit editorial decision, not an automatic
secret detector. Externally obtained descriptions/titles/bodies are not trusted
public labels and must not enter generated output.

## Verification and later slices

```sh
python scripts/work_unit_catalog.py docs/work-units/pilot.json
PYTHONPATH=. python -m pytest tests/test_work_unit_catalog.py -q
```

Next isolated slices: observations from bounded GitHub GETs, then offline cards
and a local acceptance handoff. Production publication integration is gated on
independent acceptance and the local installed-layout/serving inventory. No file
moves, `deploy/sync-manifest.txt` edits, `two_sinks.py` edits, runtime mutation or
new rest-source reads belong to this slice.

A useful pilot lets another person identify the purpose, source versions,
produced objects, verification scope and unknowns without copying the backlog.
Measure retrieval accuracy/time and maintenance costs before changing context
assembly. Context packets and linked-knowledge behavior remain a second phase.
