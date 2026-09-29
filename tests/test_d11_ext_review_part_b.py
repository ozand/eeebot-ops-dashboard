"""#356 D1.1, architect's external review of #378 @f381a8a6, PART B:
policy decisions. The canary S goes into every field of the review's list
and into map keys; each case goes through the real projection."""
from __future__ import annotations

import json

import pytest

from scripts import techtree_viewer as tv
from scripts.two_sinks import split_render_inputs
from d11_full_source import full_source

S = "PRIVATE: operator goal amber; /var/lib/operator/private.md"
FRAGMENTS = (S, "operator goal amber", "/var/lib/operator/private.md")


def _public(source: dict) -> dict:
    public, _ = split_render_inputs(source)
    return public


def _nowhere(public: dict, pages: bool = True) -> None:
    blob = json.dumps(public, default=str)
    for fragment in FRAGMENTS:
        assert fragment not in blob, fragment
    if pages:
        for name, html in tv.render_public_pages(public, "eeepc", generated_at="2026-09-29 12:00:00").items():
            for fragment in FRAGMENTS:
                assert fragment not in html, (name, fragment)


# --- B-F1 / B-F2: provenance absent is unknown; label and direction only for self-derived ---

@pytest.mark.parametrize("listname, index", [("derived_priorities", 0), ("priority_items", 1)])
def test_bf1_bf2_absent_provenance_publishes_neither_label_nor_direction(listname: str, index: int) -> None:
    source = full_source()
    row = source["derived_view"][listname][index]
    row.pop("provenance", None)
    row.update(label=S, direction=S)
    public = _public(source)
    projected = public["derived_view"][listname][index]
    assert "label" not in projected and "direction" not in projected, projected
    _nowhere(public)


@pytest.mark.parametrize("provenance", ["operator", "unknown", "self_derived "])
def test_bf2_direction_is_omitted_unless_explicitly_self_derived(provenance: str) -> None:
    source = full_source()
    for row in (source["derived_view"]["derived_priorities"][0], source["derived_view"]["priority_items"][1]):
        row.update(provenance=provenance, direction=S)
    public = _public(source)
    for row in (public["derived_view"]["derived_priorities"][0], public["derived_view"]["priority_items"][1]):
        assert "direction" not in row
    _nowhere(public)


def test_bf1_bf2_explicit_self_derived_keeps_label_and_direction() -> None:
    source = full_source()
    source["derived_view"]["derived_priorities"][0]["provenance"] = "self-derived"
    source["derived_view"]["derived_priorities"].append({"number": 4, "label": S, "direction": S})
    view = _public(source)["derived_view"]
    assert view["derived_priorities"][0]["direction"] == "reduce repeats"
    assert view["derived_priorities"][0]["label"] == "Self label"
    assert view["priority_items"][1]["label"] == "Self label"
    assert "label" not in view["derived_priorities"][1] and "direction" not in view["derived_priorities"][1]


# --- B-F4: model-output texts are private; title and *_chars are public ---------

def test_bf4_lesson_hypothesis_is_private() -> None:
    source = full_source()
    source["lessons"][0]["hypothesis"] = S
    public = _public(source)
    lesson = public["lessons"][0]
    assert lesson["hypothesis"] == "" and lesson["hypothesis_chars"] == len(S)
    assert lesson["title"] == "Retry is not a fix"
    _nowhere(public)


def test_bf4_answered_evidence_is_a_cycle_id() -> None:
    source = full_source()
    entries = source["hypotheses"]["entries"]
    entries["H-1"]["answered_evidence"] = S
    entries["H-2"] = {"status": "supported", "title": "Two", "answered_evidence": "cycle-9"}
    public = _public(source)
    assert "answered_evidence" not in public["hypotheses"]["entries"]["H-1"]
    assert public["hypotheses"]["entries"]["H-2"]["answered_evidence"] == "cycle-9"
    _nowhere(public)


_DURABLE_TEXTS = ("hypothesis", "action", "insight_criterion", "success_criterion")


def test_bf4_durable_hypothesis_texts_are_private() -> None:
    source = full_source()
    entry = source["hypotheses_durable"]["entries"][0]
    entry.update({name: S for name in _DURABLE_TEXTS})
    entry["hadi"] = {"hypothesis": S, "action": S}
    public = _public(source)
    projected = public["hypotheses_durable"]["entries"][0]
    assert projected["title"] == "Durable one"
    for name in _DURABLE_TEXTS:
        assert projected[name] == "" and projected[f"{name}_chars"] == len(S)
    assert projected["hadi"] == {"hypothesis": "", "hypothesis_chars": len(S), "action": "", "action_chars": len(S)}
    _nowhere(public)


# --- B-F3: charter text only as the untouched release goals.md charter -----------

@pytest.mark.parametrize("charter", [
    # goal_text_json / an invalid source: already withheld by D1 (test_two_sinks::test_m2_...)
    {"source": "release_goals_md", "merged": True, "text": S},
    {"source": "release_goals_md", "text": S},
    {"source": "release_goals_md", "merged": "false", "text": S},
])
def test_bf3_charter_text_is_public_only_for_an_unmerged_release_charter(charter: dict) -> None:
    source = full_source()
    source["derived_view"]["charter"] = charter
    public = _public(source)
    assert "text" not in public["derived_view"]["charter"]
    _nowhere(public)


def test_bf3_release_charter_text_stays_public() -> None:
    source = full_source()
    source["derived_view"]["charter"] = {"source": "release_goals_md", "merged": False, "text": "Public charter"}
    source["derived_view"]["charter"]["source"] = "release_goals_md"
    charter = _public(source)["derived_view"]["charter"]
    assert charter == {"source": "release_goals_md", "merged": False, "text": "Public charter"}
    source["derived_view"]["charter"]["merged"] = True
    assert "text" not in _public(source)["derived_view"]["charter"]


# --- B-F5: huge ints are bounded before any formatting ----------------------------

def test_bf5_huge_priority_number_and_exit_code_never_crash() -> None:
    source = full_source()
    source["derived_view"]["priority_items"][0]["number"] = 10 ** 5000
    source["local_ci"]["exit_code"] = 10 ** 5000
    public, private = split_render_inputs(source)
    assert public["derived_view"]["priority_items"][0]["label"] == "Operator priority"
    assert public["local_ci"]["summary"] == "result unavailable"
    assert "projection_error" not in private.get("withheld_reason_counts", {})
    json.dumps(public)  # a huge int would raise ValueError here
    tv.render_public_pages(public, "eeepc")


# --- map keys carry the canary ----------------------------------------------------

_MAP_PATHS = [
    ("cycle_titles",), ("cycle_files",), ("llm_stats",), ("demand_rotation", "served"),
    ("demand_completed", "entries"), ("hypotheses", "entries"), ("portfolio", "nodes"),
    ("evolution_tree", "nodes"), ("demand_futility", "gaps"), ("ci_freshness", "repositories"),
    ("proposer_stats", "days"), ("token_heatmap", "hourly"), ("scorecard", "feeds", "feeds"),
    ("agent_context", "system_prompt", "sections"), ("agent_context", "system_prompt", "rule_owners"),
    ("strategist_decisions", 0, "inputs_status"),
]


def test_canary_as_a_key_in_every_map() -> None:
    source = full_source()
    source["agent_context"]["system_prompt"]["rule_owners"] = {"rule_one": "agents"}
    for path in _MAP_PATHS:
        target = source
        for step in path:
            target = target[step]
        sample = next(iter(target.values()))
        target[S] = sample
        target["x/../../var/lib/operator/private.md"] = sample  # passes the D1.1 lexical key check
    source["demand_futility"] = {S: {"attempt_count": 1}}
    _nowhere(_public(source))


# --- deep isolation ---------------------------------------------------------------

def _mutable_ids(value: object) -> set[int]:
    found: set[int] = set()
    if isinstance(value, (dict, list)):
        found.add(id(value))
        for item in value.values() if isinstance(value, dict) else value:
            found |= _mutable_ids(item)
    return found


def test_public_and_private_share_no_mutable_object_and_empty_defaults_are_fresh() -> None:
    public, private = split_render_inputs(full_source())
    assert not _mutable_ids(public) & _mutable_ids(private)
    first, _ = split_render_inputs({"lessons": "broken", "bridge_exit_streak": "broken", "derived_view": None})
    second, _ = split_render_inputs({"lessons": "broken", "bridge_exit_streak": "broken", "derived_view": None})
    assert first["lessons"] == [] and first["derived_view"] == {}
    assert not _mutable_ids(first) & _mutable_ids(second)
