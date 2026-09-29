"""#378 (architect decision, class "a vocabulary has two owners"): every
``_enum`` in the public projection is checked against its WRITER.

- Dashboard writers (scripts/techtree_viewer.py): the reader EXPORTS its
  vocabulary as a constant, two_sinks imports it, and an AST check keeps
  every literal the reader returns -- in the local ``read_*_local``
  functions AND their REMOTE_READER_SCRIPT twins -- inside that constant.
- eeebot writers (another repository, cannot be imported): EEEBOT_SNAPSHOT
  below is the writer's vocabulary as a list with its file:line, a SNAPSHOT
  of eeebot at commit 6d476b71. Every value must pass the real
  split_render_inputs unchanged, so a projection that drops one turns this
  red. When eeebot adds a value, update the snapshot and its citation.

run_end classifications (Codex 4135973058) are also rendered through the
real split + render: each appears in the cycle status line.
"""
from __future__ import annotations

import ast

import pytest

from scripts import agent_context as ac
from scripts import techtree_viewer as tv
from scripts import two_sinks as sinks
from scripts.two_sinks import split_render_inputs

EEEBOT_COMMIT = "6d476b71"

#: {name: (eeebot file:line at EEEBOT_COMMIT, writer values)}
EEEBOT_SNAPSHOT: dict[str, tuple[str, tuple[str, ...]]] = {
    "run_end.classification": (
        "nanobot/crash_record.py:262-263; nanobot/runtime/bridge.py:5306-5310, 7012-7014",
        ("completion", "failed", "unit_timeout", "loop_breaker_abort", "wall_clock_abort",
         "progress_watchdog_abort")),
    "run.outcome": ("nanobot/crash_record.py:323, 443-445", ("success", "failure", "interrupted")),
    "exit_status.signal": ("nanobot/crash_record.py:73-80, 434-445 (systemd $EXIT_STATUS)",
                           ("TERM", "SIGTERM", "INT", "SIGINT", "KILL")),
    "ledger.outcome": (
        "nanobot/runtime/cycle_ledger.py:90-93 VALID_OUTCOMES, 355-357 VALID_DIARY_OPEN_OUTCOMES, "
        "404-407 VALID_PLANNING_OUTCOMES",
        ("success", "partial", "failed", "skipped-duplicate", "promotion_candidate", "push_pending",
         "pushed_late", "superseded", "abandoned", "paused-supplier", "integrated", "refused", "malformed",
         "push_failed", "commit_failed", "no_plan", "spawn_failed", "timed_out", "rest", "rest_unchanged",
         "rejected_duplicate")),
    "ledger.decision": ("nanobot/runtime/cycle_ledger.py:94 VALID_DEDUP_DECISIONS",
                        ("proceeded", "skipped_duplicate", "skipped_recent_failure")),
    "error_card.skip_reason": ("nanobot/runtime/bridge.py:7238, 7260, 7300, 7302",
                               ("worktree_add_failed", "write_failed", "push_rejected",
                                "diff_touched_more_than_errors_yaml")),
    "priority.provenance": ("nanobot/runtime/demand.py:305-306", ("operator", "self-derived")),
    "derived_view.derived_status": ("nanobot/runtime/demand.py:849-851", ("present", "absent", "probe_unavailable")),
    "derived_view.sort": ("nanobot/runtime/demand.py:1007",
                          ("provenance(operator<self-derived), then vector(V1<V2), as demand._priority_items",)),
    "local_ci.state": ("nanobot/runtime/local_ci.py:41, 109", ("ran", "targets_missing")),
    "scorecard.unavailable": ("nanobot/runtime/scorecard.py:852-867, 890-891", ("unavailable",)),
}


def _eeebot_cases():
    for name, (_where, values) in EEEBOT_SNAPSHOT.items():
        for value in values:
            yield pytest.param(name, value, id=f"{name}={value}")


def _project(name: str, value: str) -> object:
    """Put ``value`` where the writer puts it; return what the public side has there."""
    if name in ("run_end.classification", "run.outcome"):
        field = "classification" if name.endswith("classification") else "outcome"
        public, _ = split_render_inputs({
            "bridge_runs": [{"phase": "run_end", "cycle_id": "c1", field: value}],
            "bridge_exits": [{"ts": "2026-09-29T10:00:00Z", field: value}],
            "bridge_active_run": {"run_id": "r", field: value}})
        values = {public["bridge_runs"][0][field], public["bridge_exits"][0][field],
                  public["bridge_active_run"][field]}
        return values.pop() if len(values) == 1 else values
    if name == "exit_status.signal":
        public, _ = split_render_inputs({"bridge_exits": [{"exit_status": value}]})
        return public["bridge_exits"][0]["exit_status"]
    if name == "ledger.outcome":
        public, _ = split_render_inputs({"ledger_tail": [{"outcome": value, "status": value}]})
        row = public["ledger_tail"][0]
        return row["outcome"] if row["outcome"] == row["status"] else (row["outcome"], row["status"])
    if name == "ledger.decision":
        return split_render_inputs({"ledger_tail": [{"decision": value}]})[0]["ledger_tail"][0]["decision"]
    if name == "error_card.skip_reason":
        return split_render_inputs({"ledger_tail": [{"skip_reason": value}]})[0]["ledger_tail"][0]["skip_reason"]
    if name == "priority.provenance":
        view = split_render_inputs({"derived_view": {"priority_items": [{"provenance": value}]}})[0]["derived_view"]
        return view["priority_items"][0]["provenance"]
    if name == "derived_view.derived_status":
        return split_render_inputs({"derived_view": {"derived_status": value}})[0]["derived_view"]["derived_status"]
    if name == "derived_view.sort":
        return split_render_inputs({"derived_view": {"sort": value}})[0]["derived_view"]["sort"]
    if name == "local_ci.state":
        return split_render_inputs({"local_ci": {"probe": "present", "state": value}})[0]["local_ci"]["state"]
    if name == "scorecard.unavailable":
        loop = {f"{cause}_{kind}": value for cause in ("execution_failure", "model_call_incomplete")
                for kind in ("events", "tasks", "share")}
        public = split_render_inputs({"scorecard": {"loop": {**loop, "paused_supplier_outcomes": value}}})[0]
        values = set(public["scorecard"]["loop"].values())
        assert len(public["scorecard"]["loop"]) == 7
        return values.pop() if len(values) == 1 else values
    raise AssertionError(name)


@pytest.mark.parametrize("name, value", _eeebot_cases())
def test_every_eeebot_writer_value_passes_the_projection_unchanged(name: str, value: str) -> None:
    assert _project(name, value) == value, EEEBOT_SNAPSHOT[name][0]


@pytest.mark.parametrize("classification", EEEBOT_SNAPSHOT["run_end.classification"][1])
def test_every_run_end_classification_is_published_in_the_cycle_status(classification: str) -> None:
    source = {
        "ledger_tail": [{"phase": "started", "cycle_id": "cycle-run", "ts": "2026-09-29T10:00:00Z"}],
        "ledger_history": [{"phase": "started", "cycle_id": "cycle-run", "ts": "2026-09-29T10:00:00Z"}],
        "bridge_runs": [{"phase": "run_end", "run_id": "r-1", "cycle_id": "cycle-run",
                         "started_at": "2026-09-29T10:00:05Z", "finished_at": "2026-09-29T10:20:00Z",
                         "classification": classification, "outcome": "success", "exit_status": 0}],
    }
    public, _ = split_render_inputs(source)
    pages = tv.render_public_pages(public, "eeepc", generated_at="2026-09-29 12:00:00")
    shown = "".join(pages.values())
    assert f"({classification})" in shown and "(other)" not in shown


# --- dashboard writers: exported constants cover every literal they return ------

def _functions(tree: ast.AST, names: set[str]) -> list[ast.FunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]


def _returned_literals(fns: list[ast.FunctionDef], key: str) -> set[str]:
    found: set[str] = set()
    for fn in fns:
        for node in ast.walk(fn):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == key:
                        found |= {c.value for c in ast.walk(v)
                                  if isinstance(c, ast.Constant) and isinstance(c.value, str)}
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == key for t in node.targets):
                found |= {c.value for c in ast.walk(node.value) if isinstance(c, ast.Constant)
                          and isinstance(c.value, str)}
            elif isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == key:
                found |= {c.value for c in ast.walk(node.iter) if isinstance(c, ast.Constant)
                          and isinstance(c.value, str)}
    return found


_TREES = {
    "local": ast.parse(open(tv.__file__, encoding="utf-8").read()),
    "remote": ast.parse(tv.REMOTE_READER_SCRIPT),
    "agent_context": ast.parse(open(ac.__file__, encoding="utf-8").read()),
}

_DASHBOARD_WRITERS = [
    ("probe", {"read_local_ci_status_local", "read_local_ci_status", "read_executor_model_status_local",
               "read_executor_model_status"}, tv.PROBE_STATES, ("local", "remote")),
    ("status", {"read_derived_view_local", "read_derived_view", "read_systemd_drift_local",
                "read_systemd_drift"}, tv.VIEW_STATES, ("local", "remote")),
    ("status", {"read_compaction_local", "read_compaction"}, tv.COMPACTION_STATES, ("local", "remote")),
    ("source", {"read_lessons_local", "read_lessons"}, tv.LESSON_SOURCES, ("local", "remote")),
    ("enabled", {"_ci_actions_unanswerable", "_ci_actions_state"}, tv.CI_ACTIONS_ENABLED_UNKNOWN, ("local",)),
    ("kind", {"compute_truncation_streak"}, ("truncated", "dropped"), ("local", "remote", "agent_context")),
]


@pytest.mark.parametrize("key, names, vocabulary, trees", _DASHBOARD_WRITERS,
                         ids=[f"{w[0]}:{sorted(w[1])[0]}" for w in _DASHBOARD_WRITERS])
def test_dashboard_writer_literals_are_inside_the_exported_vocabulary(key, names, vocabulary, trees) -> None:
    fns = [fn for tree in trees for fn in _functions(_TREES[tree], names)]
    assert fns, names
    literals = _returned_literals(fns, key) - {"", key}
    assert literals, (key, names)
    assert literals <= set(vocabulary), sorted(literals - set(vocabulary))


def test_classify_model_returns_only_model_classes() -> None:
    fns = _functions(_TREES["local"], {"classify_model"}) + _functions(_TREES["remote"], {"classify_model"})
    returns = {n.value.value for fn in fns for n in ast.walk(fn)
               if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)}
    assert returns and returns <= set(tv.MODEL_CLASSES)


@pytest.mark.parametrize("key, vocabulary, build", [
    ("probe", tv.PROBE_STATES, lambda v: ({"local_ci": {"probe": v}}, ("local_ci", "probe"))),
    ("probe", tv.PROBE_STATES, lambda v: ({"executor_model_status": {"probe": v}}, ("executor_model_status", "probe"))),
    ("status", tv.VIEW_STATES, lambda v: ({"derived_view": {"status": v}}, ("derived_view", "status"))),
    ("status", tv.VIEW_STATES, lambda v: ({"systemd_drift": {"status": v}}, ("systemd_drift", "status"))),
    ("status", tv.COMPACTION_STATES, lambda v: ({"compaction": {"status": v}}, ("compaction", "status"))),
    ("latest_class", tv.MODEL_CLASSES,
     lambda v: ({"executor_model_status": {"latest_class": v}}, ("executor_model_status", "latest_class"))),
    ("source", tv.LESSON_SOURCES, lambda v: ({"lessons": [{"source": v}]}, ("lessons", 0, "source"))),
    ("enabled", tv.CI_ACTIONS_ENABLED_UNKNOWN,
     lambda v: ({"ci_freshness": {"r/x": {"actions_enabled": v}}}, ("ci_freshness", "r/x", "actions_enabled"))),
    ("kind", ("truncated", "dropped"), lambda v: (
        {"agent_context": {"truncation_streak": {"entries": [{"kind": v}]}}},
        ("agent_context", "truncation_streak", "entries", 0, "kind"))),
])
def test_every_dashboard_writer_value_passes_the_projection_unchanged(key, vocabulary, build) -> None:
    for value in vocabulary:
        source, path = build(value)
        projected = split_render_inputs(source)[0]
        for step in path:
            projected = projected[step]
        assert projected == value, (key, value)


def test_two_sinks_uses_the_exported_dashboard_vocabularies() -> None:
    assert sinks._PROBE_STATES is tv.PROBE_STATES and sinks._VIEW_STATES is tv.VIEW_STATES
    assert set(tv.CI_FRESHNESS_STATES) <= set(sinks._CI_STATES)
    assert set(tv.CI_LATEST_CONCLUSIONS) <= set(sinks._CI_CONCLUSIONS)
