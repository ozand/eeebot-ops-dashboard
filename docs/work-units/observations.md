# Observation contract (issue #423)

This module is a standalone opt-in pilot. It is not imported by the publisher,
not in the sync manifest, and does not change GitHub, host state or prompts.

```sh
python scripts/work_unit_observations.py docs/work-units/pilot.json \
  --collect --output /tmp/work-unit-observations.json
# On a later explicit invocation, preserve previous successful observations:
python scripts/work_unit_observations.py docs/work-units/pilot.json \
  --collect --previous /tmp/work-unit-observations.json \
  --output /tmp/work-unit-observations.json
```

No network runs without `--collect`. This client intentionally uses public,
unauthenticated GitHub GETs: no token setup is required or consumed. Rate limiting,
redirects, inaccessible resources and connection failures are reported as
unavailable, not empty. No automatic retry. Links are never recursively followed.
The public pilot currently has six unique dynamic targets. Files/commits remain
pinned references with `not_collected`, not assertions that they were fetched.

Each observation contains its canonical target ID, `checked_at`, `probe`, and an
optional `last_success` with its own `observed_at` and typed facts. Errors keep the
previous successful observation at its original timestamp. Probe failure and
freshness are independent: a recent historical observation does not hide a failed
latest check. A 404 is `not_found_or_inaccessible`, never proof of absence.
Catalog changes invalidate imported snapshots explicitly; omit `--previous` to
start a new snapshot, rather than silently treating an incompatible cache as proof.

For issues, store state and only the existing workflow status labels (preserve
multiple labels rather than inventing a resolution). For PRs, store state,
explicit `merged` and head SHA. Do not infer merging from `merge_commit_sha`.
For workflow attempts, validate run ID, attempt, repository URL and expected
head SHA. Store status and conclusion, not test totals; `checkout_sha` remains
null. A run's reported head is not proof of its actual checkout revision or that
all tests ran. No automatic acceptance, deployment or current-runtime verdict.
All dynamic object text, URLs and arbitrary labels are discarded before storage.

The snapshot is a local replayable observation, NOT a signed attestation or an
independent proof. Imported snapshots must come from trusted operator-controlled
storage. Schema validation cannot authenticate the author or truth of a fixture.

Freshness is evaluated at an explicit assessment timestamp: age < 900 seconds
is `recent_at_assessment`; equality and above are `stale`; negative age is
`clock_skew`; lack of evidence is `unknown`. The 900-second default is pilot
presentation policy only. It does not schedule publication or claim runtime health.

Bounds: default 32 requests (maximum 64), 60-second request-admission budget
(maximum 120), at most 10 seconds of socket timeout per request, 1 MiB read per
response/input/output. This is NOT a hard real-time bound on DNS resolution or a
slowly streaming OS-level read. Host operational acceptance must measure the
actual environment; a hard total deadline requires a separately supervised worker.
Output uses a same-directory temporary file and atomic replacement. Parent output
directory must already exist; no default host directory or service is created.
Exit 0: observed dynamic sources succeeded; 2: snapshot saved with source failures;
1: invalid/unreadable input/output. Invalid previous input does not replace output.

Tests use injected transport and deterministic clocks, not live requests or
machine-local paths. CLI network/credentials assumptions must be checked locally.

Primary API contracts consulted for the adapter:
- https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run-attempt
- https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request
