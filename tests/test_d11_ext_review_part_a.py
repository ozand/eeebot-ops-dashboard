"""#356 D1.1, architect's external review of #378 @f381a8a6, PART A:
combinators and entry points. One test (or parametrized group) per finding,
with the review's inputs; each goes through the real projection."""
from __future__ import annotations

import json

import pytest

from scripts import two_sinks as sinks
from scripts.two_sinks import split_render_inputs
from d11_full_source import full_source

S = "PRIVATE: operator goal amber; /var/lib/operator/private.md"
HOST_PATH = "/var/lib/operator/private.md"


def _public(source: dict) -> dict:
    public, _ = split_render_inputs(source)
    return public


def _nowhere(public: object, *fragments: str) -> None:
    blob = json.dumps(public, default=str)
    for fragment in fragments or (S, "operator goal amber", HOST_PATH):
        assert fragment not in blob, fragment


# --- P1-1: gate violations are a finite set of codes --------------------------

def test_p1_1_violation_text_never_reaches_public() -> None:
    public = _public({"ledger_tail": [{"cycle_id": "c1", "phase": "gate", "violations": [
        S, f"test_weakening: {S}", {"rule": S}, "gate_failed", "made_up_rule: x"]}]})
    _nowhere(public)
    assert public["ledger_tail"][0]["violations"] == [
        "violation", "test_weakening", "violation", "gate_failed", "violation"]


# --- P1-2: only repo-relative paths are public -----------------------------------

@pytest.mark.parametrize("bad", [HOST_PATH, "../../etc/passwd", "nanobot/../../x", "a//b.py", "/x.py"])
def test_p1_2_non_relative_paths_are_omitted(bad: str) -> None:
    public = _public({
        "ledger_tail": [{"cycle_id": "c1", "target_path": bad, "files_changed": [bad, "nanobot/ok.py"]}],
        "cycle_files": {"c1": [bad, "nanobot/ok.py"]},
        "demand_completed": {"entries": {"g": {"files_changed": [bad]}}},
        "demand_futility": {"gaps": {"g": {"surface": [bad]}}},
        "scorecard": {"quality": {"artifact_graph": {"oldest_leaves": [{"path": bad}]}}},
    })
    row = public["ledger_tail"][0]
    assert "target_path" not in row and row["files_changed"] == ["nanobot/ok.py"]
    assert public["cycle_files"] == {"c1": ["nanobot/ok.py"]}
    _nowhere(public, bad)


# --- P1-3: _map writes only keys its validator returned unchanged ---------------

def test_p1_3_map_never_publishes_an_unvalidated_key() -> None:
    by_enum = sinks._map(sinks._enum("safe", fallback="safe"), sinks._count)({S: 1, "safe": 2}, None)
    by_code = sinks._map(sinks._code("k"), sinks._count)({S: 1}, None)
    _nowhere(by_enum)
    _nowhere(by_code)
    assert by_enum == {"safe": 2} and by_code == {}


# --- P1-4: metrics are a finite, typed set of names -----------------------------

def test_p1_4_metrics_are_finite_named_and_typed() -> None:
    public = _public({"scorecard": {"loop": {
        "integrations": "prompt_text:PRIVATE:operator-goal-amber",
        "prompt_text": "PRIVATE:operator-goal-amber",
        "zz_private_metric": 5, "a" * 400: 1, "count": "PRIVATE", "lines": -1,
        "confirmed_integration_ratio": 0.5,
    }, "heldout": {"passed": "PRIVATE:x", "checked": 4}}})
    _nowhere(public, "PRIVATE", "operator-goal-amber", "zz_private_metric", "a" * 400)
    assert public["scorecard"]["loop"] == {"confirmed_integration_ratio": 0.5}
    assert public["scorecard"]["heldout"] == {"checked": 4}


# --- P2: null only where explicitly nullable -----------------------------------

def test_p2_null_is_dropped_where_the_field_is_not_nullable() -> None:
    public = _public({
        "ledger_tail": [{"cycle_id": "c1", "push_attempts": None, "reason": None, "outcome": None}],
        "cycle_titles": {"c1": None},
        "scorecard": {"loop": {"integrations": None}},
        "generator_sha": None,
        "lessons": None,
    })
    assert public["ledger_tail"] == [{"cycle_id": "c1"}]
    assert public["cycle_titles"] == {}
    assert public["scorecard"]["loop"] == {}
    assert public["generator_sha"] == "" and public["lessons"] == []
    # ...while an EXPLICITLY nullable field or section keeps its null
    public = _public({"portfolio": {"current": None, "nodes": {}}, "bridge_exit_streak": None,
                      "executor_llm_stats": {"cycle_id": "c1", "prompt_tokens": None}})
    assert public["portfolio"] == {"current": None, "nodes": {}}
    assert public["bridge_exit_streak"] is None
    assert public["executor_llm_stats"] == {"cycle_id": "c1", "prompt_tokens": None}


# --- P2: timestamps are real calendar dates and offsets ---------------------------

@pytest.mark.parametrize("bad", ["2026-02-30T10:00:00Z", "2026-13-01", "2026-09-29T25:00:00Z",
                                 "2026-09-29T10:61:00Z", "2026-09-29T10:00:00+25:00", "2026-09-29T10:00:00+05:99"])
def test_p2_impossible_timestamps_are_dropped(bad: str) -> None:
    public = _public({"ledger_tail": [{"cycle_id": "c1", "ts": bad}]})
    assert public["ledger_tail"] == [{"cycle_id": "c1"}]
    for good in ("2026-09-29T10:00:00Z", "2026-09-29T10:00:00.123+03:00", "2026-09-29", "2024-02-29T00:00:00+0530"):
        assert _public({"ledger_tail": [{"ts": good}]})["ledger_tail"] == [{"ts": good}]


# --- P2: a huge int never wipes the section ----------------------------------------

def test_p2_huge_int_does_not_raise_and_wipe_the_section() -> None:
    public, private = split_render_inputs({"ledger_tail": [{"cycle_id": "c1", "delta": 10 ** 400}],
                                           "_newest_source_age_seconds": 10 ** 400})
    assert public["ledger_tail"][0]["cycle_id"] == "c1"
    assert "projection_error" not in private.get("withheld_reason_counts", {})


# --- P2: split_render_inputs on a non-dict ------------------------------------------

@pytest.mark.parametrize("data", [None, [], "x", 7])
def test_p2_split_render_inputs_on_a_non_dict_does_not_crash(data) -> None:
    public, private = split_render_inputs(data)
    assert isinstance(public, dict) and isinstance(private, dict)
    assert public["goal_meta"]["present"] is False


# --- P2: sized_list drops a non-list, sized_text blanks are never shared --------------

def test_p2_sized_list_drops_a_non_list() -> None:
    public = _public({"reflections": [{"cycle_id": "c1", "findings": S, "recommendations": ["r"]}]})
    row = public["reflections"][0]
    assert "findings" not in row and "findings_count" not in row
    assert row["recommendations_count"] == 1
    _nowhere(public)


def test_p2_sized_text_blank_is_a_fresh_object_per_record() -> None:
    node = sinks._obj({}, sized_text={"body": []})
    first, second = node({"body": "a"}, None), node({"body": "b"}, None)
    assert first["body"] == [] and first["body"] is not second["body"]
    first["body"].append(S)
    assert node({"body": "c"}, None)["body"] == []
