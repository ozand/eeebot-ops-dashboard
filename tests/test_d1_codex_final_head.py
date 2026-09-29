"""#315: Codex findings on the D1 head 568ed218 (items 1 and 4; items 2 and
3 are unit/README changes).

(1) P1 thread 4127705268: a plain ``--local`` render (no ``--publish``,
    include_ci_freshness) must write the HTML -- the sanitizer it uses was
    imported only inside the ``--publish`` branch.
(4) P2 thread 4134051930: a bridge diagnostic is escaped exactly ONCE, at
    the HTML boundary; the public output still carries neither form of it.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts import techtree_viewer as tv
from scripts.two_sinks import split_render_inputs


def _state(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    for rel, content in {
        "evolution/tree.json": '{"current_sha": "a", "nodes": {}}',
        "tech_tree/portfolio.json": '{"current": null, "nodes": {}}',
        "hypotheses/lifecycle.json": '{"entries": {}}',
        "scorecard/latest.json": '{"computed_at_utc": "2026-09-29T00:00:00Z"}',
        "ledger/cycles.jsonl": '{"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-09-29T10:00:00Z"}\n',
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_item1_plain_local_render_with_ci_freshness_writes_html(tmp_path: Path, monkeypatch) -> None:
    # only the network probe is stubbed; reading, sanitizing and rendering are real
    monkeypatch.setattr(tv, "read_ci_freshness", lambda *a, **k: {"ozand/eeebot": {"state": "success"}})
    out = tmp_path / "site"
    rc = tv.main(["--local", "--state-root", str(_state(tmp_path)), "--out", str(out)])
    assert rc == 0
    assert (out / "index.html").is_file() and (out / "index.html").stat().st_size > 0


DIAGNOSTIC = "bad <path> & x"
WHERE = "bridge.py:<line> & y"


def _panel(bridge_exit_streak: dict) -> str:
    return tv.build_now_panel(
        {"now": "2026-09-29T12:00:00Z"}, {}, [], None, None,
        health_recent_outcomes=["integrated"], health_last_integrated_ts="2026-09-29T11:50:00Z",
        now="2026-09-29T12:00:00Z", age_seconds=60.0,
        bridge_exit_streak=bridge_exit_streak,
    )


def test_item4_bridge_diagnostic_is_escaped_exactly_once() -> None:
    raw = {"consecutive_failures": 5, "last_error": DIAGNOSTIC, "last_where": WHERE}
    verdict, reason = tv.health_verdict(
        60.0, "2026-09-29T11:50:00Z", ["integrated"], False, "2026-09-29T12:00:00Z", bridge_exit_streak=raw,
    )
    assert reason == f"bridge crash loop: 5 consecutive invocation failures: {DIAGNOSTIC} at {WHERE}"  # raw text

    html = _panel(raw)
    assert "&amp;lt;" not in html and "&amp;amp;" not in html, "double-encoded"
    assert "<path>" not in html and "<line>" not in html, "never raw"
    assert "bad &lt;path&gt; &amp; x" in html
    assert html.count("bad &lt;path&gt; &amp; x") >= 2  # health banner AND streak item


def test_item4_public_output_still_carries_neither_form() -> None:
    raw = {"consecutive_failures": 5, "last_error": DIAGNOSTIC, "last_where": WHERE}
    public, _ = split_render_inputs({"bridge_exit_streak": raw})
    html = _panel(public["bridge_exit_streak"])
    for form in (DIAGNOSTIC, "bad &lt;path&gt;", "&lt;path&gt;", "<path>", "bridge.py:"):
        assert form not in html, form
    assert "path" not in json.dumps(public) and "bridge.py" not in json.dumps(public)
