from __future__ import annotations

import json
from pathlib import Path
import threading
import urllib.request

import pytest

from scripts import techtree_viewer as tv
from scripts.two_sinks import publish_ordered, split_render_inputs


@pytest.mark.parametrize("canary", ["privatecanary", "accessdenied", "password:hunter2", "x:y"])
def test_m1_unknown_reason_codes_are_withheld(canary: str) -> None:
    public, private = split_render_inputs({
        "ledger_tail": [{"phase": "outcome", "reason": canary}],
        "strategist_decisions": [{"success": False, "reason": canary, "decision": canary}],
    })
    encoded = json.dumps(public)
    assert canary not in encoded
    assert "withheld" in encoded
    assert private["withheld_reason_counts"]
    assert "withheld_reason_counts" not in encoded


def test_m2_public_projection_uses_enums_and_validated_scalars() -> None:
    marker = "PRIVATE_CANARY_M2"
    public, _ = split_render_inputs({
        "derived_view": {"status": "present", "reason": marker, "charter": {
            "source": "goal_text_json", "text": marker,
        }, "priority_items": [{"number": "12", "evidence": marker}]},
        "local_ci": {"probe": "probe_unavailable", "reason": marker, "summary": marker,
                  "exit_code": "1"},
        "ci_freshness": {"repo": {"state": marker, "reason": marker}},
    })
    payload = json.dumps(public)
    assert marker not in payload
    assert "evidence" not in payload
    assert "number" not in public["derived_view"]["priority_items"][0]
    assert "exit_code" not in public["local_ci"]


def test_m3_projection_drops_untrusted_counter_values_and_recomputes_text_size() -> None:
    marker = "PRIVATE_CANARY_COUNTER"
    public, _ = split_render_inputs({
        "agent_context": {"prompt_text": marker, "prompt_text_chars": marker},
        "reflections": [{"cycle_id": "c", "summary": "safe", "summary_chars": marker}],
    })
    assert public["agent_context"]["prompt_text_chars"] == len(marker)
    assert public["reflections"][0]["summary_chars"] == len("safe")
    public, _ = split_render_inputs({"portfolio": {"item_chars": -1, "item_lines": True,
                                                      "item_count": "private"}})
    assert public["portfolio"] == {}


def test_m4_public_strategist_never_renders_reason_prose() -> None:
    row = {"success": False, "reason": "privatecanary", "decision": "password:hunter2"}
    public, _ = split_render_inputs({"strategist_decisions": [row]})
    html = tv._build_strategist_run_item(public["strategist_decisions"])
    assert "privatecanary" not in html
    assert "password:hunter2" not in html


def test_m5_built_tree_scan_uses_relative_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.two_sinks as sinks
    captured = {}
    monkeypatch.setattr(sinks, "_publish_scan_pages", lambda pages: captured.update(pages))
    (tmp_path / "nested").mkdir()
    (tmp_path / "index.html").write_text("top", encoding="utf-8")
    (tmp_path / "nested" / "index.html").write_text("nested", encoding="utf-8")
    sinks.scan_built_tree(tmp_path)
    assert captured == {"index.html": "top", "nested/index.html": "nested"}


def test_m6_server_canonicalizes_paths_and_head_redirects(tmp_path: Path) -> None:
    from http.server import ThreadingHTTPServer
    from scripts.two_sinks import SnapshotHTTPRequestHandler
    root = tmp_path / "site"
    (root / "v1").mkdir(parents=True)
    (root / "v1" / "index.html").write_text("ok", encoding="utf-8")
    (root / "current").symlink_to("v1", target_is_directory=True)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    class Handler(SnapshotHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

    Handler.site_root = root.resolve()
    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        for route in ("/%63urrent/", "/./current/"):
            request = urllib.request.Request(base + route, method="HEAD")
            with urllib.request.urlopen(request) as response:
                assert response.status == 200
                assert response.geturl().endswith("/v1/index.html")
                assert response.read() == b""
        for route in ("/.v1.tmp/index.html", "/%2e%2e/README.md", "http://elsewhere/"):
            with pytest.raises(Exception):
                urllib.request.urlopen(base + route)
        request = urllib.request.Request(base + "/v1/index.html", method="HEAD")
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
            assert response.read() == b""
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_m6_server_startup_rejects_incomplete_current_snapshot(tmp_path: Path) -> None:
    import scripts.two_sinks as sinks
    root = tmp_path / "site"
    root.mkdir()
    (root / "v1").mkdir()
    (root / "current").symlink_to("v1", target_is_directory=True)
    with pytest.raises(FileNotFoundError):
        sinks.serve_site(root, "127.0.0.1", 8080)


def test_m7_page_names_and_versions_are_validated(tmp_path: Path) -> None:
    from scripts.two_sinks import atomic_snapshot_swap
    for bad in ("../escape.html", "/absolute.html", "nested/../../escape.html"):
        with pytest.raises(ValueError):
            atomic_snapshot_swap(tmp_path / "site", {bad: "x"}, "v1")
    for bad_version in ("x\\r\\nInjected: yes", "x" * 65, "../outside"):
        with pytest.raises(ValueError):
            atomic_snapshot_swap(tmp_path / "site", {"index.html": "x"}, bad_version)


def test_projection_survives_real_render_publish_in_every_public_file(tmp_path: Path) -> None:
    source = {
        "portfolio": {}, "scorecard": {},
        "ledger_tail": [{"phase": "outcome", "reason": "privatecanary"}],
        "goal_text": {"text": "PRIVATE_GOAL_BODY"}, "agents_md": "PRIVATE_AGENTS_BODY",
        "agent_context": {"prompt_text": "PRIVATE_PROMPT_BODY", "task_text": "PRIVATE_TASK_BODY",
                         "prompt_text_chars": "BAD_COUNTER"},
        "strategist_decisions": [{"success": False, "reason": "password:hunter2", "decision": "accessdenied"}],
        "reflections": [{"summary": "PRIVATE_REFLECTION_BODY", "findings": ["PRIVATE_FINDING_BODY"]}],
        "lessons": [{"problem": "PRIVATE_LESSON_BODY"}],
        "derived_view": {"status": "present", "reason": "PRIVATE_DERIVED_REASON",
                         "charter": {"source": "goal_text_json", "text": "PRIVATE_CHARTER_BODY"},
                         "priority_items": [{"label": "safe", "evidence": "PRIVATE_EVIDENCE_BODY"}]},
        "ci_freshness": {"repo": {"state": "PRIVATE_CI_STATE", "reason": "PRIVATE_CI_DETAIL"}},
        "local_ci": {"probe": "probe_unavailable", "reason": "PRIVATE_CI_REASON",
                      "summary": "PRIVATE_CI_SUMMARY", "exit_code": "BAD_EXIT"},
    }
    public, private = split_render_inputs(source)
    pages = tv.render_public_pages(public, "test")
    publish_ordered(tmp_path / "site", pages, {}, "vprojection", lambda _: (0, {}))
    files = list((tmp_path / "site" / "vprojection").rglob("*"))
    rendered = json.dumps({p.name: p.read_text(encoding="utf-8") for p in files if p.is_file()})
    for canary in (
        "PRIVATE_GOAL_BODY", "PRIVATE_AGENTS_BODY", "PRIVATE_PROMPT_BODY", "PRIVATE_TASK_BODY",
        "password:hunter2", "accessdenied", "PRIVATE_REFLECTION_BODY", "PRIVATE_FINDING_BODY",
        "PRIVATE_LESSON_BODY", "PRIVATE_DERIVED_REASON", "PRIVATE_CHARTER_BODY",
        "PRIVATE_EVIDENCE_BODY", "PRIVATE_CI_REASON", "PRIVATE_CI_SUMMARY",
        "PRIVATE_CI_STATE", "PRIVATE_CI_DETAIL", "privatecanary",
    ):
        assert canary not in rendered, canary
    assert private["withheld_reason_counts"]


def test_m8_host_failure_persists_even_when_public_publish_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import techtree_autopublish as ap
    HostSnapshotError = ap.sinks.HostSnapshotError
    state_dir = tmp_path / "state"
    ap.save_publish_state(state_dir, "old", 1.0)
    monkeypatch.setattr(ap, "compute_tree_digest", lambda *_: "new")
    monkeypatch.setattr(ap, "_unreadable_tree_source", lambda *_: None)
    monkeypatch.setattr(ap.tv, "read_local_state", lambda *_a, **_kw: {"_error": None})
    monkeypatch.setattr(ap.tv, "read_ci_freshness", lambda: {})
    monkeypatch.setattr(ap.tv, "render_public_pages", lambda *_a, **_kw: {"index.html": "safe"})
    monkeypatch.setattr(ap.sinks, "render_private_pages", lambda *_a, **_kw: {})
    monkeypatch.setattr(ap.sinks, "publish_ordered", lambda *_a, **_kw: (_ for _ in ()).throw(
        HostSnapshotError("host failed", publish_result=(0, {"index.html": "fp"}))))
    args = ap.parse_args(["--state-root", str(tmp_path), "--state-dir", str(state_dir), "--site-root", str(tmp_path / "site")])
    assert ap.run(args) == 1
    saved = ap.load_publish_state(state_dir)
    assert saved["host_snapshot_failed_since"] is not None
    assert saved["last_host_error"] == "host failed"


def test_m8_host_recovery_clears_failure_state(tmp_path: Path) -> None:
    from scripts import techtree_autopublish as ap
    state_dir = tmp_path / "state"
    ap.save_publish_state(state_dir, "old", 1.0, host_snapshot_failed_since=2.0,
                          last_host_error="previous")
    ap.save_publish_state(state_dir, "new", 3.0, clear_host_failure=True)
    saved = ap.load_publish_state(state_dir)
    assert "host_snapshot_failed_since" not in saved
    assert "last_host_error" not in saved