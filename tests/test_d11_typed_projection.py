"""#356 ADR-036 D1.1: the typed public projection.

Acceptance cases R1/R3/R4/R5 (external review of D1 part B) and the issue
body's tests. Every case uses the canary S and asserts that S appears
NOWHERE in the public output: the ``public`` dict, every rendered public
page, and (for the end-to-end case) every file of the real snapshot and of
the public sink's upload.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import techtree_autopublish as ap
from scripts import techtree_viewer as tv
from scripts.two_sinks import PUBLIC_DATA_KEYS, _sanitize_public_value, publish_ordered, split_render_inputs
from d11_full_source import full_source

S = "PRIVATE: operator goal amber; /var/lib/operator/private.md"
FRAGMENTS = (S, "operator goal amber", "/var/lib/operator/private.md")


def _render(public: dict) -> dict[str, str]:
    return tv.render_public_pages(public, "eeepc", generated_at="2026-09-29 12:00:00")


def _assert_nowhere(public: dict, pages: dict[str, str] | None = None) -> None:
    blob = json.dumps(public, default=str)
    for fragment in FRAGMENTS:
        assert fragment not in blob, fragment
    for name, html in (pages if pages is not None else _render(public)).items():
        for fragment in FRAGMENTS:
            assert fragment not in html, (name, fragment)


def _split(source: dict) -> dict:
    public, _ = split_render_inputs(source)
    return public


def _set(source: dict, path: tuple, value: object) -> dict:
    target: Any = source
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return source


# --- R1: rows are constructed, never copied ------------------------------------

_ROW_LISTS = ("ledger_tail", "ledger_history", "subagent_records", "reflections",
              "strategist_decisions", "lessons")


@pytest.mark.parametrize("key", _ROW_LISTS)
def test_r1_private_row_fields_and_non_dict_rows_never_survive(key: str) -> None:
    source = full_source()
    row = {**source[key][0], "reason": S, "details": S, "prompt_text": S, "future_answer": S,
           "rationale": S, "summary": S, "problem": S, "task_excerpt": S}
    source[key] = [row, S, [S], 7]
    public = _split(source)
    assert len(public[key]) == 1, "non-dict rows are dropped"
    assert not {"details", "prompt_text", "future_answer"} & set(public[key][0])
    _assert_nowhere(public)


def test_r1_ledger_tail_case_verbatim() -> None:
    public = _split({"ledger_tail": [{"reason": S, "details": S, "prompt_text": S}]})
    assert public["ledger_tail"] == [{"reason": "withheld (<=64)"}]
    _assert_nowhere(public)


# --- R3: explicit enums, real bools, validated timestamps -----------------------

_REPO = ("ci_freshness", "repositories", "ozand/eeebot")

#: (field set to S, a sibling that must survive in the SAME record, its value).
#: The sibling proves the record was projected, not dropped wholesale.
_R3_CASES = [
    ((*_REPO, "latest_conclusion"), (*_REPO, "actions_enabled"), True),
    ((*_REPO, "observed_at_utc"), (*_REPO, "latest_conclusion"), "success"),
    ((*_REPO, "actions_enabled"), (*_REPO, "latest_conclusion"), "success"),
    ((*_REPO, "freshness_state"), (*_REPO, "latest_conclusion"), "success"),
    ((*_REPO, "freshness", "latest_conclusion"), (*_REPO, "freshness", "state"), "fresh"),
    ((*_REPO, "freshness", "latest_completed_at_utc"), (*_REPO, "freshness", "state"), "fresh"),
    (("ci_freshness", "observed_at_utc"), (*_REPO, "latest_conclusion"), "success"),
    (("bridge_exits", 0, "classification"), ("bridge_exits", 0, "outcome"), "success"),
    (("bridge_exits", 0, "outcome"), ("bridge_exits", 0, "exit_status"), 0),
    (("bridge_active_run", "classification"), ("bridge_active_run", "run_id"), "r-2"),
    (("bridge_active_run", "outcome"), ("bridge_active_run", "run_id"), "r-2"),
    (("local_ci", "probe"), ("local_ci", "exit_code"), 0),
    (("local_ci", "state"), ("local_ci", "probe"), "present"),
    (("local_ci", "ts_utc"), ("local_ci", "probe"), "present"),
    (("derived_view", "status"), ("derived_view", "generated_at_utc"), "2026-09-29T09:00:00Z"),
    (("derived_view", "derived_status"), ("derived_view", "generated_at_utc"), "2026-09-29T09:00:00Z"),
]
_R3_PATHS = [case[0] for case in _R3_CASES]


def _get(value: Any, path: tuple) -> Any:
    for step in path:
        value = value[step]
    return value


@pytest.mark.parametrize("path, sibling, expected", _R3_CASES, ids=lambda p: ".".join(map(str, p)) if isinstance(p, tuple) else None)
def test_r3_typed_fields_never_carry_free_text(path: tuple, sibling: tuple, expected: object) -> None:
    public = _split(_set(full_source(), path, S))
    _assert_nowhere(public)
    assert _get(public, sibling) == expected


def test_r3_flat_ci_freshness_map_is_typed_too() -> None:
    public = _split({"ci_freshness": {"ozand/eeebot": {
        "state": "success", "latest_conclusion": S, "observed_at_utc": S, "actions_enabled": S}}})
    assert public["ci_freshness"] == {"ozand/eeebot": {"state": "success", "latest_conclusion": "unknown"}}
    _assert_nowhere(public)


def test_r3_domains_are_enum_bool_and_timestamp() -> None:
    source = full_source()
    repo = source["ci_freshness"]["repositories"]["ozand/eeebot"]
    repo.update(latest_conclusion="exploded", actions_enabled="yes", observed_at_utc="yesterday")
    source["local_ci"].update(probe="PRESENT", state="ran!", ts_utc="2026-09-29 as of now")
    source["derived_view"].update(status="present-ish", derived_status=True)
    source["bridge_active_run"].update(classification="killed by operator", outcome=["success"])
    public = _split(source)
    projected = public["ci_freshness"]["repositories"]["ozand/eeebot"]
    assert projected["latest_conclusion"] == "unknown"
    assert "actions_enabled" not in projected and "observed_at_utc" not in projected
    assert not {"probe", "state", "ts_utc"} & set(public["local_ci"])
    assert not {"status", "derived_status"} & set(public["derived_view"])
    assert public["bridge_active_run"]["classification"] == "other"
    assert public["bridge_active_run"]["outcome"] == "other"


def test_r3_valid_values_are_kept() -> None:
    # agents_meta / goal_meta are built by the split from agents_md / goal_text
    assert set(full_source()) >= PUBLIC_DATA_KEYS - {"agents_meta", "goal_meta"}
    public = _split(full_source())
    _assert_nowhere(public)
    assert public["bridge_exits"] == [{"ts": "2026-09-29T09:10:00Z", "cycle_id": "cycle-1",
                                       "outcome": "success", "exit_status": 0}]
    repo = public["ci_freshness"]["repositories"]["ozand/eeebot"]
    assert (repo["latest_conclusion"], repo["actions_enabled"], repo["observed_at_utc"]) == (
        "success", True, "2026-09-29T10:00:00Z")
    assert public["local_ci"]["probe"] == "present" and public["local_ci"]["state"] == "ran"
    assert public["derived_view"]["status"] == "present"
    assert public["bridge_runs"][0]["classification"] == "unit_timeout"


# --- R4: agent_context --------------------------------------------------------

def test_r4_non_string_prompt_and_task_are_withheld() -> None:
    source = full_source()
    source["agent_context"]["prompt_text"] = [{"type": "text", "text": S}]
    source["agent_context"]["task_text"] = {"text": S}
    public = _split(source)
    context = public["agent_context"]
    assert context["prompt_text"] is None and context["task_text"] is None
    assert "prompt_text_chars" not in context and "task_text_chars" not in context
    _assert_nowhere(public)


@pytest.mark.parametrize("tier2_lessons", [
    [{"problem": S, "solution": S}],
    {"index_status": "present", "files": [{"name": "l.yaml", "problem": S, "solution": S}], "problem": S},
])
def test_r4_tier2_lessons_rows_carry_no_text(tier2_lessons) -> None:
    source = full_source()
    source["agent_context"]["tier2_lessons"] = tier2_lessons
    _assert_nowhere(_split(source))


def test_r4_tier2_skill_path_and_content_and_memory_path() -> None:
    source = full_source()
    context = source["agent_context"]
    context["tier2_skills"][0].update(path=S, content=[S], desc={"x": S})
    context["tier2_memory"]["files"][0].update(path=S, content=S)
    public = _split(source)
    skill = public["agent_context"]["tier2_skills"][0]
    assert "path" not in skill and skill["content"] == "" and "content_chars" not in skill
    memory_file = public["agent_context"]["tier2_memory"]["files"][0]
    assert "path" not in memory_file and memory_file["content_chars"] == len(S)
    _assert_nowhere(public)


# --- R5: one provenance policy -------------------------------------------------

def test_r5_operator_provenance_label_in_derived_priorities_is_withheld() -> None:
    source = full_source()
    source["derived_view"]["derived_priorities"][0].update(label=S, provenance="operator")
    public = _split(source)
    assert public["derived_view"]["derived_priorities"][0]["label"] == "Priority #3"
    _assert_nowhere(public)
    # the same policy keeps EXPLICIT self-derived labels public, numbers
    # operator items, and (#378 B-F1) withholds a label with no provenance
    view = _split(full_source())["derived_view"]
    assert "label" not in view["derived_priorities"][0]
    assert view["priority_items"][1]["label"] == "Self label"
    assert view["priority_items"][0]["label"] == "Priority #1"


@pytest.mark.parametrize("provenance", [None, "", "imported", S])
def test_r5_unknown_provenance_withholds_the_label_in_both_lists(provenance) -> None:
    source = full_source()
    view = source["derived_view"]
    for row in (view["derived_priorities"][0], view["priority_items"][1]):
        row.update(label=S, id="p-private")
        if provenance is None:
            row.pop("provenance", None)
        else:
            row["provenance"] = provenance
    if provenance is None:
        # derived_priorities.json rows are self-derived by construction (the
        # list's own provenance); a priority item with none recorded is unknown.
        view["derived_priorities"][0]["provenance"] = "unknown"
    public = _split(source)
    for row in (public["derived_view"]["derived_priorities"][0], public["derived_view"]["priority_items"][1]):
        assert "label" not in row and "id" not in row, row
    _assert_nowhere(public)


# --- issue body: unknown keys at every level, wrong types, non-dict rows -------

def _inject_everywhere(value: object, path: tuple = ()) -> object:
    """An unknown key (and a key that IS the canary) in every dict, and a
    non-dict row plus a dict row of unknown keys in every list."""
    if isinstance(value, dict):
        result = {key: _inject_everywhere(item, (*path, key)) for key, item in value.items()}
        result[S] = S
        if path != ("cycle_titles",):  # a map of public titles keyed by cycle id
            result["zz_d11_unknown"] = S
        return result
    if isinstance(value, list):
        return [*(_inject_everywhere(item, (*path, "*")) for item in value), S, {"zz_d11_unknown": S}, [S]]
    return value


def test_unknown_nested_key_at_every_level_is_dropped() -> None:
    source = {key: _inject_everywhere(value, (key,)) if key in PUBLIC_DATA_KEYS else value
              for key, value in full_source().items()}
    _assert_nowhere(_split(source))


def _string_leaf_paths(value: object, path: tuple = ()) -> list[tuple]:
    if isinstance(value, dict):
        return [p for key, item in value.items() for p in _string_leaf_paths(item, (*path, key))]
    if isinstance(value, list):
        return [p for index, item in enumerate(value) for p in _string_leaf_paths(item, (*path, index))]
    return [path] if isinstance(value, str) else []


@pytest.mark.parametrize("wrong", [[S], {"text": S}, {S: S}], ids=["list", "dict", "keyed"])
def test_every_string_field_given_a_wrong_type_is_dropped(wrong) -> None:
    source = full_source()
    paths = [p for p in _string_leaf_paths(source) if p[0] in PUBLIC_DATA_KEYS]
    assert len(paths) > 100
    for path in paths:
        _set(source, path, copy.deepcopy(wrong))
    _assert_nowhere(_split(source))


#: D1 (#315 R1/R6/R7) already gives these a fixed safe shape.
_D1_PROJECTED = {"_error", "agents_meta", "goal_meta", "bridge_exit_streak", "bridge_runs"}


@pytest.mark.parametrize("key", sorted(PUBLIC_DATA_KEYS - _D1_PROJECTED))
def test_a_wrong_shape_section_never_passes_through(key: str) -> None:
    for broken in (S, [S], {"zz_d11_unknown": S}, {S: S}):
        if key == "cycle_titles" and broken == {"zz_d11_unknown": S}:
            continue  # a well-formed {cycle id: public commit subject} map
        source = full_source()
        source[key] = copy.deepcopy(broken)
        _assert_nowhere(_split(source))


# --- isolation -----------------------------------------------------------------
# Guards: on 9229636c the recursive counter sweep already rebuilt every public
# container, so these hold there too; the typed projection must keep it so.

def _containers(value: object) -> list[object]:
    found = []
    if isinstance(value, (dict, list)):
        found.append(value)
        for item in value.values() if isinstance(value, dict) else value:
            found.extend(_containers(item))
    return found


def _mutate_all(value: object) -> None:
    for container in _containers(value):
        if isinstance(container, dict):
            for key in list(container):
                if isinstance(container[key], str):
                    container[key] = S
            container["zz_mutated"] = S
        else:
            container.append(S)


def _hostile_source() -> dict:
    """Every string field replaced by a list holding the canary, plus
    unknown keys and rows everywhere: isolation must hold for ANY input."""
    source = full_source()
    for path in [p for p in _string_leaf_paths(source) if p[0] in PUBLIC_DATA_KEYS]:
        _set(source, path, [S])
    return {key: _inject_everywhere(value, (key,)) if key in PUBLIC_DATA_KEYS else value
            for key, value in source.items()}


def test_public_shares_no_mutable_object_with_source_or_private() -> None:
    source = _hostile_source()
    public, private = split_render_inputs(source)
    source_ids = {id(c) for c in _containers(source)} | {id(c) for c in _containers(private)}
    assert not source_ids & {id(c) for c in _containers(public)}


def test_mutating_private_after_the_split_does_not_change_public() -> None:
    source = _hostile_source()
    public, private = split_render_inputs(source)
    before = copy.deepcopy(public)
    _mutate_all(private)
    _mutate_all(source)
    assert public == before


# --- the REAL render + publish, every final public file --------------------------

def test_real_render_and_publish_every_public_file(tmp_path: Path) -> None:
    source = {key: _inject_everywhere(value, (key,)) if key in PUBLIC_DATA_KEYS else value
              for key, value in full_source().items()}
    for path in _R3_PATHS:
        _set(source, path, S)
    context = source["agent_context"]
    context["prompt_text"] = [{"type": "text", "text": S}]
    context["tier2_skills"][0].update(path=S, content=[S])
    context["tier2_memory"]["files"][0]["path"] = S
    source["derived_view"]["derived_priorities"][0].update(label=S, provenance="operator")
    source["derived_view"]["priority_items"][1].update(label=S)
    source["derived_view"]["priority_items"][1].pop("provenance")
    source["ledger_tail"][0].update(details=S, prompt_text=S, reason=S)
    source["goal_text"] = {"text": S}
    source["agents_md"] = S

    public, private = split_render_inputs(source)
    assert S in json.dumps(private, default=str), "the private side keeps the source"
    uploaded: list[dict[str, str]] = []
    site = tmp_path / "site"
    site.mkdir()  # the D4 host step creates the site root
    publish_ordered(site, _render(public), {}, "v356", lambda pages: uploaded.append(pages) or (0, {}))

    files = [path for path in (site / "v356").rglob("*") if path.is_file()]
    assert (site / "v356" / "index.html") in files and len(files) >= 5
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for fragment in FRAGMENTS:
            assert fragment not in text, (path.name, fragment)
    assert uploaded and set(uploaded[0]) == {p.relative_to(site / "v356").as_posix() for p in files}
    for name, text in uploaded[0].items():
        for fragment in FRAGMENTS:
            assert fragment not in text, (name, fragment)
    _assert_nowhere(public)


# --- scope item 3: the projection version is part of the publish digest --------

def test_projection_version_change_changes_the_publish_digest(tmp_path: Path, monkeypatch) -> None:
    before = ap.compute_tree_digest(tmp_path)
    monkeypatch.setattr(ap.sinks, "PROJECTION_VERSION", "d1.1-typed-test-bump")
    assert ap.compute_tree_digest(tmp_path) != before


def test_the_ci_freshness_resanitize_path_is_the_typed_projection() -> None:
    # autopublish and viewer --publish re-project ci_freshness after the split
    projected = _sanitize_public_value("ci_freshness", full_source()["ci_freshness"])
    assert projected["repositories"]["ozand/eeebot"]["latest_conclusion"] == "success"
    broken = _sanitize_public_value("ci_freshness", _set(full_source(), ("ci_freshness", "observed_at_utc"), S)["ci_freshness"])
    assert S not in json.dumps(broken)
