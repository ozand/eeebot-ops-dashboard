# Testing Guide & Operational Baselines

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
