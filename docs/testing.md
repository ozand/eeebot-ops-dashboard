# Testing Guide & Operational Baselines

## Token heatmap day-summary regression (#433)
The public typed projection contract added optional, bounded `summary.invalid_days`; projection `PROJECTION_VERSION` is bumped from `d1.1-typed-2` to `d1.1-typed-3` so publication digest changes with the output domain. Local and embedded reader parity and invalid-only observed-day behavior use synthetic state only; they do not access hosts.
`tests/test_techtree_viewer.py::test_tokens_day_summary_renders_incomplete_only_for_validated_invalid_date` runs synthetic producer output through `split_render_inputs` and local Playwright Chromium, verifies the visible category total/call count/incomplete marker, rejects a private canary, and saves the rendered detail at `C:/Temp/433-evidence/tokens-day-summary.png`. `test_token_heatmap_local_and_embedded_reader_parity` executes the embedded reader script on synthetic local state without SSH; `test_token_heatmap_invalid_rows_excluded_and_day_marked_incomplete` verifies an invalid-only day is observed, incomplete, and does not expose a canary. `tests/test_two_sinks.py::test_token_heatmap_invalid_days_projection_accepts_retained_sorted_dates` and `...::test_token_heatmap_invalid_days_projection_rejects_bad_shape_and_out_of_window_values` cover the typed projection's Gregorian/ordered/contiguous/62-date bounds and invalid metadata. Projection version is now `d1.1-typed-3` so the output-domain change changes the publish digest. Existing tests cover known zero vs missing days and the 20:59 UTC / 22:00 UTC MSK transition. All fixtures are synthetic and local; this is not host/publication evidence.

## Continuous Integration (GitHub Actions)
- **Workflow**: `.github/workflows/test.yml`
- **Environment**: Ubuntu (Python 3.12)
- **CI Baseline**: **424 passed, 8 skipped, 0 failed** (GREEN)
- The 8 skipped tests in `tests/test_canonical_import.py` are explicitly marked with `pytest.mark.skipif` when running outside the original monorepo layout.

## Local Test Suite (Windows Developer Environment)
- **Environment**: Windows (Python 3.13)
- **Local Baseline**: **419 passed, 8 skipped, 5 failed** (out of 432 collected tests).

### Windows-Specific Test Failures (5 tests)
All 5 failures on Windows are path formatting / path separation assertions in string matching (posix `/` vs windows `\`):

1. `tests/test_app.py::test_app_experiments_renders_current_experiment_and_budget`
   - Expects literal posix path string `workspace/state/experiments/current.json` in HTML output; on Windows the template renders `workspace\state\experiments\current.json`.
2. `tests/test_collector.py::test_normalize_repo_state_loads_hypothesis_backlog_snapshot`
   - Checks `backlog['path'].endswith('workspace/state/hypotheses/backlog.json')` with posix separators against Windows backslashed path.
3. `tests/test_collector.py::test_collect_once_persists_subagent_telemetry`
   - Checks `detail['source_path'].endswith('workspace/state/subagents/sub-2.json')` against Windows backslashed path.
4. `tests/test_dashboard_truth_audit_gaps.py::test_hypotheses_api_exposes_local_vs_live_diagnostics_and_prefers_live_canonical_backlog`
   - Checks `payload['local_path'].endswith('/workspace/state/hypotheses/backlog.json')` against Windows backslashed path.
5. `tests/test_selfevo_pr_system.py::test_system_api_exposes_bounded_selfevo_current_proof_summary`
   - Checks `any(path.endswith('workspace/state/self_evolution/current_state.json') for path in proof['evidence_paths'])` against Windows backslashed path.

When evaluating changes locally on Windows, any test failure outside these 5 path-separator assertions represents a genuine regression.
