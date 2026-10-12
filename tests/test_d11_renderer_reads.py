"""#378 (architect decision, variant A): a structural guard between the
public renderer and the typed public projection.

Every field the renderer reads on the PUBLISHED path must be a decision:
either the projection publishes it (``two_sinks.public_schema_fields()``),
or it is listed below -- in INTENTIONALLY_WITHHELD (public data D1.1
deliberately does not publish) or RENDERER_LOCAL (keys of structures the
renderer builds itself, never public data). A new read without a decision
turns ``test_every_renderer_read_on_the_publish_path_is_decided`` red.

Heuristic (static, AST):
- Roots: ``techtree_viewer.render_public_pages`` and
  ``two_sinks.render_private_pages`` (empty until D2 fills it).
- Reachability: a function is reached when a reached function mentions its
  NAME (a call, or a reference such as a callback) -- resolved by bare name
  across techtree_viewer.py, agent_context.py and about_page.py. This
  over-approximates (a shared name pulls in every definition of it), which
  errs toward reporting more reads, never fewer.
- A read is a string literal in ``x.get("key"...)`` or a load subscript
  ``x["key"]``, inside a reached function (nested functions included).

Blind spots (reads this does NOT see):
- membership tests (``"key" in row``): not counted, because the same syntax
  is a substring test on a string (``"skip" in decision``); an existence
  check is in practice followed by a ``.get``/subscript read, which counts;
- keys computed at runtime (``row[name]`` over a variable, f-strings,
  ``getattr``) and iteration over a whole record (``for k, v in row.items()``,
  ``json.dumps(row)``) -- token_heatmap is dumped wholesale into tokens.html;
  its fields are covered by the projection schema, not by this test;
- calls through a name bound at runtime (a dict of handlers, a lambda
  assigned to a differently named variable);
- which OBJECT a key is read from: a literal read on a renderer-local dict
  looks the same as a read on public data -- RENDERER_LOCAL records those;
- modules outside the three listed.
The legacy single-page ``render_page`` branch (build_tech_canvas,
_lane_a/_lane_b_layout, _direction_box_html, _evo_box_html,
build_daily_digest) is NOT reachable from the published path and is
deliberately out of scope (follow-up issue on the dashboard).
"""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from scripts.two_sinks import public_schema_fields

ROOT = Path(__file__).resolve().parents[1]
RENDERER_MODULES = ("scripts/techtree_viewer.py", "scripts/agent_context.py", "scripts/about_page.py")
ROOTS = (("scripts/techtree_viewer.py", "render_public_pages"), ("scripts/two_sinks.py", "render_private_pages"))

#: public data the renderer reads but D1.1 deliberately does NOT publish.
INTENTIONALLY_WITHHELD: dict[str, str] = {
    "agents_md": "operator-private AGENTS.md body; only agents_meta (presence, lines, chars) is public",
    "goal_text": "operator-private goal_text.json; only goal_meta (state, presence, priority count) is public",
    "cycle_titles_error": "probe exception text (host paths, stderr); not a public key",
    "health_last_integrated_ts": "not produced by any state reader; the renderer derives it from ledger rows",
    "health_recent_outcomes": "not produced by any state reader; the renderer derives it from ledger rows",
    "generalized_insight": "lesson insight text (alias); lesson text is LAN-only, only insight/result sizes are public",
    "reusable_insight": "lesson insight text (alias); lesson text is LAN-only, only insight/result sizes are public",
}

#: keys of structures the renderer builds itself (or static page tables).
RENDERER_LOCAL: dict[str, str] = {
    **{key: "lineage payload built by _build_unified_lineage" for key in (
        "basis", "boundary", "coverage", "cycle_node_count", "cycle_node_index", "node_id", "parent",
        "parent_basis", "parent_known", "parent_status", "source_available", "target", "ts_status")},
    **{key: "coverage summary built by the lineage builder (_lineage_coverage_text)" for key in (
        "emitted_nodes", "excluded_nodes", "from_ts", "to_ts", "retention_limit", "unique_candidate_nodes")},
    **{key: "cycle-details record built by build_cycle_details" for key in (
        "gate_violations", "subagents", "original_bytes")},
    **{key: "prompt parse / skill table built inside build_two_tier_context_html" for key in (
        "actual_chars", "catalogue", "heading", "memory", "mismatches", "omitted", "on_disk", "outside_cap",
        "pool", "recorded_chars")},
    **{key: "static about-page / glossary tables (about_page.py)" for key in (
        "cadence", "definition", "holds", "identifiers", "page", "produces", "role", "stage", "term", "unit",
        "why")},
    "GENERATOR_SHA_FILE": "module constant lookup in _generator_sha, not data",
    "cycle_details": "private D2 details supplied by local host state; never passed to public pages",
    "reflection": "sanitized summary counts projected for host-private cycle detail pages",
}


def _index(sources: dict[str, str]) -> dict[str, list[ast.AST]]:
    funcs: dict[str, list[ast.AST]] = defaultdict(list)
    for text in sources.values():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs[node.name].append(node)
    return funcs


def _root_functions(sources: dict[str, str]) -> list[ast.AST]:
    roots = []
    for rel, name in ROOTS:
        tree = ast.parse(sources.get(rel) or (ROOT / rel).read_text(encoding="utf-8"))
        roots += [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    return roots


def renderer_reads(sources: dict[str, str] | None = None) -> dict[str, set[str]]:
    """{literal key: {function names reading it}} on the published path."""
    sources = sources or {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in RENDERER_MODULES}
    funcs = _index({rel: sources[rel] for rel in RENDERER_MODULES})
    todo = _root_functions(sources)
    assert len(todo) == len(ROOTS), "a root function is missing"
    seen: set[int] = set()
    reads: dict[str, set[str]] = defaultdict(set)
    while todo:
        fn = todo.pop()
        if id(fn) in seen:
            continue
        seen.add(id(fn))
        for n in ast.walk(fn):
            name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else None
            if name in funcs:
                todo.extend(funcs[name])
            key = None
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                    and n.args and isinstance(n.args[0], ast.Constant)):
                key = n.args[0].value
            elif isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) and isinstance(n.slice, ast.Constant):
                key = n.slice.value
            if isinstance(key, str):
                reads[key].add(fn.name)
    return reads


def _undecided(reads: dict[str, set[str]]) -> dict[str, set[str]]:
    decided = public_schema_fields() | set(INTENTIONALLY_WITHHELD) | set(RENDERER_LOCAL)
    return {key: fns for key, fns in reads.items() if key not in decided}


def test_every_renderer_read_on_the_publish_path_is_decided() -> None:
    undecided = _undecided(renderer_reads())
    assert not undecided, (
        "renderer reads with no projection decision -- add the field to _PUBLIC_SCHEMA (typed), "
        f"or to INTENTIONALLY_WITHHELD / RENDERER_LOCAL with a reason: {dict(sorted(undecided.items()))}")


def test_a_new_read_in_a_reachable_function_turns_the_guard_red() -> None:
    """Mutation: one new public-data read added to a reachable function."""
    sources = {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in RENDERER_MODULES}
    anchor = '    """ADR-036 public entry point; caller supplies only public-safe data."""\n'
    assert sources["scripts/techtree_viewer.py"].count(anchor) == 1
    sources["scripts/techtree_viewer.py"] = sources["scripts/techtree_viewer.py"].replace(
        anchor, anchor + "    data.get('zz_d11_new_public_field')\n")
    assert _undecided(renderer_reads(sources)) == {"zz_d11_new_public_field": {"render_public_pages"}}


def test_the_decision_lists_are_neither_stale_nor_published() -> None:
    reads = renderer_reads()
    listed = {**INTENTIONALLY_WITHHELD, **RENDERER_LOCAL}
    assert not set(INTENTIONALLY_WITHHELD) & set(RENDERER_LOCAL)
    assert not set(listed) & public_schema_fields(), "a withheld/local key is also in the public schema"
    stale = sorted(set(listed) - set(reads))
    assert not stale, f"no longer read on the publish path -- remove: {stale}"
    assert all(reason.strip() for reason in listed.values())
