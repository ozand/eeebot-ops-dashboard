"""#315 external review, PART B: the split_render_inputs projection (D1 scope:
R2, R6, R7, R8, R9 and R1 partial; R1/R3/R4/R5 in full are D1.1 #356).

Every test goes through the REAL ``split_render_inputs`` and, where text
could leak, the real public renderer, with a unique canary string.
"""
from __future__ import annotations

import json

import pytest

from scripts import techtree_viewer as tv
from scripts.two_sinks import split_render_inputs

CANARY = "CANARY-315B-7f1e2d"


def _render(public: dict) -> dict[str, str]:
    return tv.render_public_pages(public, "eeepc", generated_at="2026-09-29 12:00:00")


def _nowhere(public: dict, pages: dict[str, str] | None = None) -> None:
    assert CANARY not in json.dumps(public, default=str)
    for name, html in (pages or {}).items():
        assert CANARY not in html, name


# --- R2 (P1): local_ci summary only from a validated exit code -----------------

@pytest.mark.parametrize("exit_code", [False, "0", 0.0, None])
def test_r2_local_ci_summary_never_reads_passed_from_an_invalid_exit_code(exit_code) -> None:
    public, _ = split_render_inputs({"local_ci": {"probe": "present", "state": "ran", "exit_code": exit_code}})
    local_ci = public["local_ci"]
    assert local_ci["summary"] != "passed", local_ci
    assert local_ci["summary"] == "result unavailable"
    page = _render(public)["index.html"]
    assert ": passed</span>" not in page


def test_r2_a_valid_zero_exit_code_still_reads_passed() -> None:
    public, _ = split_render_inputs({"local_ci": {"probe": "present", "state": "ran", "exit_code": 0}})
    assert public["local_ci"]["summary"] == "passed"


# --- R6 (P1): bare counters in agents_meta / goal_meta are validated ------------

def test_r6_bare_meta_counters_are_validated(tmp_path) -> None:
    public, _ = split_render_inputs({
        "agents_meta": {"present": True, "lines": CANARY, "chars": True, "count": -3},
    })
    meta = public["agents_meta"]
    assert "lines" not in meta and "chars" not in meta and "count" not in meta, meta
    _nowhere(public, _render(public))


def test_r6_valid_meta_counters_are_kept() -> None:
    public, _ = split_render_inputs({"agents_md": "line one\nline two\n"})
    assert public["agents_meta"] == {"present": True, "lines": 2, "chars": len("line one\nline two")}


# --- R7 (P2): goal_meta omits unavailable counters -----------------------------

@pytest.mark.parametrize("goal_text", [None, "not a dict", {"charter": "no priorities list"}])
def test_r7_goal_meta_never_emits_none_counters(goal_text) -> None:
    data = {} if goal_text is None else {"goal_text": goal_text}
    public, _ = split_render_inputs(data)
    meta = public["goal_meta"]
    assert None not in meta.values(), meta
    assert "lines" not in meta and "chars" not in meta and "priority_count" not in meta


def test_r7_goal_meta_priority_count_when_available() -> None:
    public, _ = split_render_inputs({"goal_text": {"priorities": ["a", "b", "c"]}})
    assert public["goal_meta"] == {"present": True, "priority_count": 3, "state": "present"}


# --- R8 (P2): a broken section never crashes the projection --------------------

@pytest.mark.parametrize("key, broken, canary_checked", [
    ("ci_freshness", {"ozand/eeebot": {"state": ["unhashable", CANARY]}}, True),
    # the value domain of derived_view.status is D1.1 #356 (R3); here: no crash
    ("derived_view", {"status": [CANARY]}, False),
    ("agent_context", {"tier2_memory": None}, True),
    ("agent_context", {"tier2_skills": 7}, True),
    ("agent_context", {"tier2_memory": {"files": 5}}, True),
])
def test_r8_broken_shape_gets_a_safe_section_not_an_exception(key, broken, canary_checked) -> None:
    public, _ = split_render_inputs({key: broken, "portfolio": {"current": None, "nodes": {}}})
    assert public["portfolio"] == {"current": None, "nodes": {}}, "other sections are projected"
    if canary_checked:
        _nowhere(public)
    _render(public)  # the public pages still render


# --- R9 (P2): identifier keys of maps are not counters -------------------------

def test_r9_repository_identifier_ending_in_count_is_kept() -> None:
    public, _ = split_render_inputs({
        "ci_freshness": {"ozand/request_count": {"state": "success"}},
        "llm_stats": {"ozand/request_count": {"calls": 2}},
    })
    assert "ozand/request_count" in public["ci_freshness"], public["ci_freshness"]
    assert public["llm_stats"] == {"ozand/request_count": {"calls": 2}}


def test_r9_genuine_counter_fields_are_still_validated() -> None:
    public, _ = split_render_inputs({"llm_stats": {"cycle-1": {"calls": 2, "retry_count": "x", "token_count": 5}}})
    # source counters are never trusted (dropped; recomputed where generated)
    assert public["llm_stats"] == {"cycle-1": {"calls": 2}}


# --- R1 partial: error / reason / last_where and a non-dict streak become codes --

def test_r1_top_level_error_becomes_a_code() -> None:
    public, _ = split_render_inputs({"_error": f"state root unreadable: /home/{CANARY}/state"})
    assert public["_error"] == "state_read_failed"
    _nowhere(public, _render(public))


def test_r1_bridge_run_free_text_becomes_codes() -> None:
    public, _ = split_render_inputs({"bridge_runs": [{
        "cycle_id": "cycle-1", "classification": "unit_timeout", "started_at": "2026-09-29T10:00:00Z",
        "error": f"Traceback: {CANARY}", "reason": CANARY, "last_where": f"/opt/{CANARY}.py:12",
    }]})
    run = public["bridge_runs"][0]
    assert run["error"] == "error" and run["last_where"] == "withheld"
    assert run["cycle_id"] == "cycle-1" and run["classification"] == "unit_timeout"
    _nowhere(public, _render(public))


def test_r1_non_dict_bridge_exit_streak_is_never_passed_through() -> None:
    public, _ = split_render_inputs({"bridge_exit_streak": f"broken: {CANARY}"})
    assert public["bridge_exit_streak"] == {}
    _nowhere(public, _render(public))
