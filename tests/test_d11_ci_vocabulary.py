"""#378 Codex round 3 (architect decision): the public projection's CI and
error-card enums come from their WRITERS.

(1) CI freshness: techtree_viewer exports the vocabulary its CI reader emits
    (CI_FRESHNESS_STATES, CI_ACTIONS_STATES, CI_LATEST_CONCLUSIONS); two_sinks
    imports it. Every value the writer can emit passes the projection
    unchanged and appears on the public page; an AST check keeps every
    literal the writer returns inside the exported constants.
(2) error_card_recording skip reasons: build_cycle_details is on the publish
    path (lineage-cycle-details.json), so each reason eeebot's bridge writes
    (bridge.py 7238/7260/7300/7302/7312) survives split + render.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts import techtree_viewer as tv
from scripts.two_sinks import split_render_inputs

ROOT = Path(__file__).resolve().parents[1]
_CI_WRITERS = ("_ci_cannot_ask", "_ci_actions_unanswerable", "_ci_actions_state", "_ci_freshness_state")


def _ci_source(state: str = "recent", conclusion: str = "success", actions_state: str = "known") -> dict:
    return {"ci_freshness": {"schema_version": 1, "observed_at_utc": "2026-09-29T10:00:00Z", "repositories": {
        "ozand/eeebot": {
            "actions_enabled": True, "freshness_state": state, "latest_conclusion": conclusion,
            "observed_at_utc": "2026-09-29T10:00:00Z",
            "actions": {"state": actions_state, "enabled": True, "observed_at_utc": "2026-09-29T10:00:00Z"},
            "freshness": {"state": state, "latest_conclusion": conclusion,
                          "observed_at_utc": "2026-09-29T10:00:00Z"},
        }}}}


def _ci_item(public: dict) -> str:
    return tv._build_ci_freshness_item(public["ci_freshness"])


def test_every_state_literal_the_ci_writer_returns_is_exported() -> None:
    tree = ast.parse((ROOT / "scripts/techtree_viewer.py").read_text(encoding="utf-8"))
    writers = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in _CI_WRITERS]
    assert len(writers) == len(_CI_WRITERS)
    allowed = {"state": set(tv.CI_FRESHNESS_STATES) | set(tv.CI_ACTIONS_STATES),
               "latest_conclusion": set(tv.CI_LATEST_CONCLUSIONS)}
    found = {"state": set(), "latest_conclusion": set()}
    for fn in writers:
        for node in ast.walk(fn):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value in found:
                        for const in ast.walk(value):
                            if isinstance(const, ast.Constant) and isinstance(const.value, str):
                                found[key.value].add(const.value)
    assert found["state"] >= {"recent", "runs_old", "runs_pending", "no_runs", "cannot_ask"}
    for key, values in found.items():
        assert values <= allowed[key], (key, sorted(values - allowed[key]))


@pytest.mark.parametrize("state", tv.CI_FRESHNESS_STATES)
def test_every_ci_freshness_state_passes_unchanged_and_is_shown(state: str) -> None:
    public, _ = split_render_inputs(_ci_source(state=state))
    repo = public["ci_freshness"]["repositories"]["ozand/eeebot"]
    assert repo["freshness_state"] == state and repo["freshness"]["state"] == state
    assert state in _ci_item(public)


@pytest.mark.parametrize("conclusion", tv.CI_LATEST_CONCLUSIONS)
def test_every_ci_conclusion_passes_unchanged_and_is_shown(conclusion: str) -> None:
    public, _ = split_render_inputs(_ci_source(conclusion=conclusion))
    repo = public["ci_freshness"]["repositories"]["ozand/eeebot"]
    assert repo["latest_conclusion"] == conclusion and repo["freshness"]["latest_conclusion"] == conclusion
    assert conclusion in _ci_item(public)


@pytest.mark.parametrize("actions_state", tv.CI_ACTIONS_STATES)
def test_every_ci_actions_state_passes_unchanged(actions_state: str) -> None:
    public, _ = split_render_inputs(_ci_source(actions_state=actions_state))
    assert public["ci_freshness"]["repositories"]["ozand/eeebot"]["actions"]["state"] == actions_state


@pytest.mark.parametrize("skip_reason", ["push_rejected", "worktree_add_failed", "write_failed",
                                         "diff_touched_more_than_errors_yaml", "exception:TimeoutError"])
def test_error_card_skip_reason_survives_split_and_render(skip_reason: str) -> None:
    row = {"phase": "error_card_recording", "cycle_id": "cycle-ecr", "status": "not_created",
           "skip_reason": skip_reason, "ts": "2026-09-29T10:00:00Z"}
    public, _ = split_render_inputs({"ledger_tail": [row], "ledger_history": [row]})
    pages = tv.render_public_pages(public, "eeepc", generated_at="2026-09-29 12:00:00")
    details = json.loads(pages["lineage-cycle-details.json"])
    assert details["cycle-ecr"]["error_card_recording"]["skip_reason"] == skip_reason
    assert "[withheld]" not in pages["lineage-cycle-details.json"]
