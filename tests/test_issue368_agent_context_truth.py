"""Issue #368: agent.html must show what the runtime recorded, not stale
dashboard constants, and must never pair one cycle's metadata with another
cycle's text.

Every test drives the real publisher path: a state directory on disk ->
``techtree_viewer.read_local_state`` (the ``--local`` reader the host
publisher runs) -> ``render_pages`` -> the public ``agent.html``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from scripts import techtree_viewer as tv
from scripts.agent_context import SEPARATOR

CYCLE_A = "cycle-368-a"
CYCLE_B = "cycle-368-b"

# Sizes taken from the issue: goals 3,849 and operating 6,103 exceed the
# old per-block numbers (3,200 / 5,000) but sit inside the shared pool.
SECTIONS: dict[str, int] = {
    "identity": 1000,
    "soul": 1200,
    "goals": 3849,
    "user": 500,
    "operating": 6103,
    "agents": 4000,
    "priorities": 800,
    "skills_catalogue": 0,
    "memory": 900,
    "runtime": 300,
    "scorecard": 500,
    "position": 400,
}


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "phase": "system_prompt",
        "cycle_id": CYCLE_A,
        "ts": "2026-09-27T20:19:00Z",
        "chars": 20000,
        "cap": 35000,
        "rung": "full",
        "sections": dict(SECTIONS),
        "dropped": [],
        "trimmed": [],
        "missing": [],
        "truncated": [],
    }
    row.update(overrides)
    return row


def _prompt_text(sections: dict[str, int], filler: str = "x") -> str:
    return SEPARATOR.join(filler * size for size in sections.values() if size > 0)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _state(
    tmp_path: Path,
    *,
    rows: list[dict[str, Any]] | None = None,
    prompts: dict[str, str] | None = None,
    llm_calls: list[dict[str, Any]] | None = None,
    compaction: list[dict[str, Any]] | None = None,
) -> Path:
    state = tmp_path / "state"
    _write_jsonl(state / "ledger" / "cycles.jsonl", rows if rows is not None else [_row()])
    prompts_dir = state / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    for name, text in (prompts or {}).items():
        (prompts_dir / name).write_text(text, encoding="utf-8")
    if llm_calls is not None:
        _write_jsonl(state / "llm_calls" / "2026-09-27.jsonl", llm_calls)
    if compaction is not None:
        _write_jsonl(state / "compaction" / "journal.jsonl", compaction)
    return state


def _render(tmp_path: Path, state: Path) -> dict[str, str]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    data = tv.read_local_state(str(state), instance_repo=str(repo))
    return tv.render_pages(data, host="eeepc", generated_at="2026-09-27 21:00:00")


# --- item 1: release pool, not stale per-block caps --------------------------

def test_item1_release_blocks_show_shared_pool_not_stale_per_block_caps(tmp_path):
    state = _state(tmp_path, prompts={f"{CYCLE_A}.system.txt": _prompt_text(SECTIONS)})
    html = _render(tmp_path, state)["agent.html"]

    # goals 3,849 and operating 6,103 must not sit next to "3,200c"/"5,000c"
    # caps they appear to overrun.
    assert "3,200c" not in html
    assert '<td class="num">5,000c</td>' not in html
    # release pool occupancy: 1,000 + 1,200 + 3,849 + 500 + 6,103
    assert "12,652 / 15,500c" in html
    # sections the builder defines are mapped, never "unmapped"
    assert 'class="t1-owner t1-owner-unmapped"' not in html
    for cap in ("3,000c", "600c", "1,200c"):  # priorities / scorecard / position
        assert f'<td class="num">{cap}</td>' in html


STATIC = "(static, runtime not reporting)"


def test_item1_static_pool_numbers_say_they_are_static(tmp_path):
    """No pool telemetry on the row: 15,500 and 5,000 come from the
    dashboard's documented map and the page must say so, every time."""
    state = _state(tmp_path, prompts={f"{CYCLE_A}.system.txt": _prompt_text(SECTIONS)})
    html = _render(tmp_path, state)["agent.html"]

    assert f"shared pool 15,500c {STATIC} · floor 5,000c {STATIC}" in html  # operating row
    assert f"12,652 / 15,500c {STATIC} used" in html
    assert f"OPERATING.md floor 5,000c {STATIC}" in html
    assert "shared pool 15,500c ·" not in html  # never unlabelled
    assert "shared pool 15,500c</td>" not in html


def test_item1_recorded_pool_is_shown_without_static_label(tmp_path):
    state = _state(
        tmp_path,
        rows=[_row(release_pool_chars={"limit": 16000, "used": 12345}, operating_reserve_chars=5500)],
        prompts={f"{CYCLE_A}.system.txt": _prompt_text(SECTIONS)},
    )
    html = _render(tmp_path, state)["agent.html"]

    assert "shared pool 16,000c · floor 5,500c" in html
    assert "12,345 / 16,000c used (recorded by runtime)" in html
    assert "OPERATING.md floor 5,500c (a minimum" in html
    assert STATIC not in html
    assert "15,500" not in html


# --- item 2: window from runtime telemetry, or unknown -----------------------

def test_item2_dialogue_window_comes_from_llm_calls_context_window(tmp_path):
    state = _state(
        tmp_path,
        prompts={f"{CYCLE_A}.system.txt": _prompt_text(SECTIONS)},
        llm_calls=[{
            "ts": "2026-09-27T20:20:00Z", "cycle_id": CYCLE_A, "component": "executor",
            "prompt_tokens": 51234, "context_window": 131072,
        }],
    )
    html = _render(tmp_path, state)["agent.html"]

    assert "98,000" not in html and "8,000)" not in html and "90,000 tokens" not in html
    assert "model window 131,072 tokens (executor llm_calls.context_window)" in html
    assert "compaction reserve unknown" in html


def test_item2_dialogue_window_is_unknown_without_context_window(tmp_path):
    state = _state(
        tmp_path,
        prompts={f"{CYCLE_A}.system.txt": _prompt_text(SECTIONS)},
        llm_calls=[{
            "ts": "2026-09-27T20:20:00Z", "cycle_id": CYCLE_A, "component": "executor",
            "prompt_tokens": 51234, "context_window": None,
        }],
    )
    html = _render(tmp_path, state)["agent.html"]

    assert "98,000" not in html and "90,000 tokens" not in html
    assert "model window unknown" in html


# --- item 3: UTC converted to MSK, not relabelled -----------------------------

def test_item3_utc_timestamp_is_converted_to_msk(tmp_path):
    state = _state(tmp_path, prompts={f"{CYCLE_A}.system.txt": _prompt_text(SECTIONS)})
    html = _render(tmp_path, state)["agent.html"]

    assert "20:19:00 MSK" not in html  # relabelled UTC
    assert "2026-09-27 23:19:00 MSK" in html


# --- item 4: no snapshot substitution -----------------------------------------

def test_item4_missing_snapshot_text_is_not_substituted_from_another_cycle(tmp_path):
    other_text = "B" * 12345
    state = _state(
        tmp_path,
        rows=[_row(cycle_id=CYCLE_B, ts="2026-09-27T19:00:00Z"), _row()],
        # the selected (latest) row is cycle A; ONLY cycle B's files exist
        prompts={f"{CYCLE_B}.system.txt": other_text, f"{CYCLE_B}.task.txt": "## Identity\n" + "t" * 777},
    )
    newer = os.path.getmtime(state / "prompts" / f"{CYCLE_B}.system.txt") + 60
    os.utime(state / "prompts" / f"{CYCLE_B}.system.txt", (newer, newer))
    html = _render(tmp_path, state)["agent.html"]

    assert "12,345 chars received by model" not in html  # cycle B's size
    assert "12,345 chars" not in html
    assert "789c" not in html  # cycle B's task text size
    assert f"text for this snapshot unavailable ({CYCLE_A})" in html


# --- item 5: separate stage states, no overclaiming "Prompt fit: full" --------

def test_item5_stage_states_are_separate_when_block_load_truncates(tmp_path):
    state = _state(
        tmp_path,
        rows=[_row(truncated=["AGENTS.md"])],
        prompts={f"{CYCLE_A}.system.txt": _prompt_text(SECTIONS)},
        compaction=[
            {"ts": "2026-09-27T20:25:00Z", "cycle_id": CYCLE_A, "iteration": 3, "reason": "below_threshold"},
            {"ts": "2026-09-27T20:40:00Z", "cycle_id": CYCLE_A, "iteration": 9, "reason": "compacted"},
            {"ts": "2026-09-26T10:00:00Z", "cycle_id": "cycle-other", "iteration": 2, "reason": "compacted"},
        ],
    )
    html = _render(tmp_path, state)["agent.html"]

    assert "Prompt fit: full" not in html
    assert '<span class="context-badge badge-success prompt-stage-badge">Assembly fit: full</span>' in html
    assert '<span class="context-badge badge-danger prompt-stage-badge">Source instructions: 1 truncated (AGENTS.md)</span>' in html
    assert "History: compacted (1×)" in html  # cycle A only, not cycle-other's row


# --- public-output canary ------------------------------------------------------

def test_snapshot_text_canary_never_reaches_any_public_file(tmp_path, monkeypatch):
    """ADR-036 rule 3: snapshot text is LAN-only. #368 changes which text is
    READ, never what is PUBLISHED. A canary in the selected cycle's own
    .system.txt/.task.txt, and in another cycle's, goes through the real
    generator -- the publisher's own ``main --local`` entry point, which
    reads the state dir via ``read_agent_context_dict`` and writes the site
    to disk -- and no written file contains it."""
    from scripts.agent_context import read_agent_context_dict

    canary_a = "CANARY-368-a7f3c1e9-SELECTED"
    canary_b = "CANARY-368-5d20b4aa-OTHER"
    text_a = _prompt_text(SECTIONS)
    text_a = canary_a + text_a[len(canary_a):]
    task_a = f"## Identity\n{canary_a}\n"
    state = _state(
        tmp_path,
        rows=[_row(cycle_id=CYCLE_B, ts="2026-09-27T19:00:00Z"), _row()],
        prompts={
            f"{CYCLE_A}.system.txt": text_a,
            f"{CYCLE_A}.task.txt": task_a,
            f"{CYCLE_B}.system.txt": f"{canary_b}\n",
            f"{CYCLE_B}.task.txt": f"## Identity\n{canary_b}\n",
        },
    )
    # the reader really does carry the canary text into the render input
    context = read_agent_context_dict(state)
    assert canary_a in context["prompt_text"] and canary_a in context["task_text"]

    # only the network probe is stubbed; reading and rendering are real
    monkeypatch.setattr(tv, "read_ci_freshness", lambda *a, **k: None)
    out = tmp_path / "site"
    assert tv.main(["--local", "--state-root", str(state), "--out", str(out)]) == 0

    written = [p for p in out.rglob("*") if p.is_file()]
    assert (out / "agent.html") in written
    agent_html = (out / "agent.html").read_text(encoding="utf-8")
    assert f"{len(text_a):,} chars received by model" in agent_html  # sizes published
    for path in written:
        data = path.read_bytes()
        assert canary_a.encode() not in data, path
        assert canary_b.encode() not in data, path
