"""ADR-036 D1 two-sink contract tests."""
from __future__ import annotations

from pathlib import Path
import json
import sys

import pytest

from scripts.two_sinks import (
    atomic_snapshot_swap,
    DEFAULT_BIND_ADDRESS,
    DEFAULT_BIND_PORT,
    parse_bind_settings,
    publish_ordered,
    scan_built_tree,
    split_render_inputs,
    validate_publish_allowlist,
)

PUBLIC = {"index.html": "<html><head></head><body>public</body></html>"}
PRIVATE = {"cycle.html": "private calls"}


def test_public_pages_same_snapshot_in_both_sinks(tmp_path: Path):
    """ADR-036 §1: host precedes gh-pages and both use one version."""
    order = []
    publish_ordered(tmp_path / "site", PUBLIC, PRIVATE, "v1", lambda pages: order.append(("gh", pages)))
    assert (tmp_path / "site/current").resolve().name == "v1"
    assert 'content="v1"' in (tmp_path / "site/v1/index.html").read_text()
    assert len(order) == 1
    assert order[0][0] == "gh"
    assert 'content="v1"' in order[0][1]["index.html"]
    assert "Snapshot v1" in order[0][1]["index.html"]
    assert "cycle.html" not in order[0][1]


def test_publish_allowlist_refuses_unlisted_pages(tmp_path: Path):
    """ADR-036 §3: unexpected publication paths fail loudly."""
    with pytest.raises(ValueError, match="unlisted"):
        validate_publish_allowlist({"secret.bin": "x"})


def test_private_pages_never_reach_gh_pages(tmp_path: Path):
    """ADR-036 §3: private pages exist only in the host snapshot."""
    sent = []
    publish_ordered(tmp_path / "site", PUBLIC, PRIVATE, "v1", sent.append)
    assert "cycle.html" in {p.name for p in (tmp_path / "site/v1").iterdir()}
    assert len(sent) == 1
    assert "index.html" in sent[0]
    assert "cycle.html" not in sent[0]


def test_public_pages_carry_no_call_content(tmp_path: Path):
    """ADR-036 §3: real read_local_state with markers across all text sources -> no marker in any public file."""
    from scripts import techtree_viewer as tv

    state = tmp_path / "state"
    repo = tmp_path / "repo"
    state.mkdir()
    repo.mkdir()

    markers = {
        "prompt_sys": "MARKER_PROMPT_SYS_9911",
        "prompt_task": "MARKER_PROMPT_TASK_9922",
        "subagent_res": "MARKER_SUBAGENT_RES_9944",
        "agents_md": "MARKER_AGENTS_MD_9955",
        "goal_text": "MARKER_GOAL_TEXT_9966",
        "reflections": "MARKER_REFLECTIONS_9977",
    }

    pdir = state / "prompts"
    pdir.mkdir()
    (pdir / "c1.system.txt").write_text(f"system {markers['prompt_sys']}", encoding="utf-8")
    (pdir / "c1.task.txt").write_text(f"task {markers['prompt_task']}", encoding="utf-8")

    sa_dir = state / "subagents"
    sa_dir.mkdir()
    (sa_dir / "res-1.json").write_text(
        json.dumps({"cycle_id": "c1", "subagent_id": "s1", "label": "test", "status": "ok", "task": markers['subagent_res'], "summary": markers['subagent_res']}),
        encoding="utf-8",
    )

    (repo / "AGENTS.md").write_text(f"# Instructions\n\n{markers['agents_md']}\n", encoding="utf-8")

    gdir = state / "goals"
    gdir.mkdir()
    (gdir / "goal_text.json").write_text(
        json.dumps({"charter": f"Goals charter {markers['goal_text']}", "goal_text": markers['goal_text']}),
        encoding="utf-8",
    )

    rdir = state / "reflector"
    rdir.mkdir()
    (rdir / "reflections.jsonl").write_text(
        json.dumps({"cycle_id": "c1", "summary": markers['reflections'], "findings": [markers['reflections']]}) + "\n",
        encoding="utf-8",
    )

    ldir = state / "ledger"
    ldir.mkdir()
    (ldir / "cycles.jsonl").write_text(
        json.dumps({"phase": "proposed", "cycle_id": "c1", "task_title": "t"}) + "\n" +
        json.dumps({"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-09-25T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    data = tv.read_local_state(str(state), str(repo))
    raw_str = json.dumps(data, default=str)
    for m in markers.values():
        assert m in raw_str

    public_data, private_data = split_render_inputs(data)
    pub_str = json.dumps(public_data, default=str)
    for k, m in markers.items():
        assert m not in pub_str, f"Marker {k} ({m}) found in public_data"

    pages = tv.render_pages(public_data, "eeepc")
    for fname, content in pages.items():
        for k, m in markers.items():
            assert m not in content, f"Marker {k} ({m}) found in rendered public page {fname}"


def test_no_private_file_of_any_kind_is_published(tmp_path: Path):
    """ADR-036 §3: allowlist is extension-independent."""
    for name in ("calls.json", "dump.gz", "index.idx", "unexpected.bin"):
        with pytest.raises(ValueError, match="unlisted"):
            validate_publish_allowlist({name: "payload"})


def test_built_tree_scan_refuses_private_markers(tmp_path: Path):
    """ADR-036 §3, §5: scan the complete built tree for private markers."""
    bad_payloads = [
        '{"token":"sk-abcdefghijklmnop12345"}',
        '/etc/eeepc-agent/secret.env',
        'token: ghp_1234567890abcdefghijklmnopqrstuvwxyz',
        'Authorization: Bearer secret-token-xyz-12345',
        'aws_key = AKIA1234567890ABCDEF',
        'slack = xoxb-12345-abcdef',
        'api_key=my_secret_key_123',
        '{"reasoning_content": "internal thinking"}',
        '{"messages": [{"role": "user"}]}',
        '{"prompt": {"text": "hello"}}',
    ]
    for i, payload in enumerate(bad_payloads):
        target = tmp_path / f"probe_{i}.json"
        target.write_text(payload, encoding="utf-8")
        with pytest.raises(Exception, match=r"(?i)sensitive markers|rejected|private marker"):
            scan_built_tree(tmp_path)
        target.unlink()


def test_snapshot_cleanup_prunes_older_than_previous(tmp_path: Path):
    """ADR-036 M2: after successful swap, keep current and previous version dirs; prune older."""
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "1"}, "v1")
    assert (root / "current").resolve().name == "v1"
    assert {p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()} == {"v1"}

    atomic_snapshot_swap(root, {"index.html": "2"}, "v2")
    assert (root / "current").resolve().name == "v2"
    assert {p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()} == {"v1", "v2"}

    atomic_snapshot_swap(root, {"index.html": "3"}, "v3")
    assert (root / "current").resolve().name == "v3"
    assert {p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()} == {"v2", "v3"}


def test_snapshot_version_and_footer_rendered_on_pages():
    """ADR-036 S1: version and generation timestamp appear on page footer, meta not duplicated."""
    from scripts.two_sinks import add_snapshot_version
    pages = {"index.html": "<html><head></head><body><h1>Hello</h1></body></html>"}
    v1 = add_snapshot_version(pages, "v1", generated_at="2026-09-26T00:00:00Z")
    html = v1["index.html"]
    assert '<meta name="snapshot-version" content="v1">' in html
    assert '<footer class="snapshot-meta">Snapshot v1 · generated 2026-09-26T00:00:00Z</footer>' in html
    v2 = add_snapshot_version(v1, "v1", generated_at="2026-09-26T00:00:00Z")
    assert v2["index.html"].count('<meta name="snapshot-version"') == 1
    assert v2["index.html"].count('<footer class="snapshot-meta"') == 1


def test_sink_failure_leaves_consistent_snapshot(tmp_path: Path):
    """ADR-036 §1: failed second sink leaves the complete host snapshot."""
    def fail(_pages):
        raise RuntimeError("publisher failed")
    with pytest.raises(RuntimeError):
        publish_ordered(tmp_path / "site", PUBLIC, PRIVATE, "v1", fail)
    assert (tmp_path / "site/current/index.html").is_file()
    assert "private calls" in (tmp_path / "site/current/cycle.html").read_text()


def test_public_renderer_input_has_no_call_text(tmp_path: Path):
    """ADR-036 §3: public renderer receives no prompt or priority text."""
    public, _ = split_render_inputs({"cycle_prompts": "private-call", "goal_text": {"charter": "secret charter"}, "safe": 1})
    assert "cycle_prompts" not in public
    assert "secret" not in json.dumps(public)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows unprivileged symlink swap limitation")
def test_snapshot_swap_replaces_existing_symlink_atomically(tmp_path: Path):
    """ADR-036 S3: swap atomically replaces existing symlink on POSIX."""
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "v1"}, "v1")
    assert (root / "current").resolve().name == "v1"
    atomic_snapshot_swap(root, {"index.html": "v2"}, "v2")
    assert (root / "current").resolve().name == "v2"


def test_server_root_redirects_to_version_snapshot(tmp_path: Path):
    """ADR-036 Decision 2: root :8080 redirects to /<version>/ so reader stays in one snapshot."""
    from scripts.two_sinks import SnapshotHTTPRequestHandler
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "hello"}, "v1")

    class MockHandler(SnapshotHTTPRequestHandler):
        def __init__(self, path):
            self.path = path
            self.site_root = root
            self.response_code = None
            self.headers_sent = {}
        def send_response(self, code):
            self.response_code = code
        def send_header(self, k, v):
            self.headers_sent[k] = v
        def end_headers(self):
            pass

    h = MockHandler("/")
    h.do_GET()
    assert h.response_code == 302
    assert h.headers_sent.get("Location") == "/v1/"


def test_snapshot_swap_is_atomic_for_readers(tmp_path: Path, monkeypatch):
    """ADR-036 §1: current switches via rename, never remove-then-link."""
    import os
    root = tmp_path / "site"
    renames = []
    real_replace = os.replace
    def record_replace(source, destination):
        renames.append((Path(source).name, Path(destination).name))
        if Path(destination).name == "current":
            monkeypatch.setattr(os, "replace", real_replace)
        return real_replace(source, destination)
    monkeypatch.setattr(os, "replace", record_replace)
    atomic_snapshot_swap(root, PUBLIC, "old")
    monkeypatch.setattr(os, "replace", real_replace)
    # Windows cannot atomically replace an existing directory symlink without
    # privilege; exercise the rename contract on the first activation here.
    assert (root / "current").resolve().name == "old"
    assert (".current-old", "current") in renames
    assert (root / "old/index.html").is_file()


def test_host_bind_address_and_interface_are_configurable():
    """ADR-036 §2: operator controls bind address; default stays all interfaces."""
    assert parse_bind_settings() == ("0.0.0.0", 8080)
    assert parse_bind_settings("100.64.0.2", 9090) == ("100.64.0.2", 9090)
    assert DEFAULT_BIND_ADDRESS == "0.0.0.0"
    assert DEFAULT_BIND_PORT == 8080
    args = __import__("scripts.techtree_autopublish", fromlist=["parse_args"]).parse_args([
        "--serve", "--bind-address", "100.64.0.3", "--bind-port", "8081",
    ])
    assert (args.bind_address, args.bind_port, args.serve) == ("100.64.0.3", 8081, True)


def test_host_snapshot_written_even_without_gh_token(tmp_path: Path, monkeypatch):
    """ADR-036 M3: host snapshot is always written; missing GH_TOKEN skips only gh-pages and returns 1."""
    from scripts import techtree_autopublish as ap

    root = tmp_path / "state"
    state_dir = tmp_path / "state_dir"
    site_root = tmp_path / "site"

    for rel, content in {
        "evolution/tree.json": '{"current_sha": "a", "nodes": {}}',
        "tech_tree/portfolio.json": '{"current": null, "nodes": {}}',
        "hypotheses/lifecycle.json": '{"entries": {}}',
        "scorecard/latest.json": '{"computed_at_utc": "2026-08-18T00:00:00Z"}',
        "ledger/cycles.jsonl": '{"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-09-25T10:00:00Z"}\n',
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("EEEBOT_SITE_ROOT", str(site_root))

    args = ap.parse_args(["--state-root", str(root), "--state-dir", str(state_dir), "--site-root", str(site_root)])
    rc = ap.run(args)

    assert rc == 1
    assert (site_root / "current" / "index.html").is_file()


def test_publish_to_pages_enforces_allowlist_and_scans_base_tree():
    """ADR-036 B1: publish_to_pages enforces allowlist directly and rejects unlisted pages."""
    from scripts import techtree_viewer as tv

    rc, _ = tv.publish_to_pages({"forbidden.json": "{}"})
    assert rc == 1

    with pytest.raises(Exception, match=r"(?i)sensitive markers|rejected|leak"):
        tv.publish_to_pages({"index.html": "token=sk-1234567890123456"})


def test_host_failure_does_not_block_gh_pages_publish(tmp_path: Path):
    """ADR-036 M3: host write failure does not prevent gh-pages publish call."""
    from scripts.two_sinks import HostSnapshotError
    published = []

    def mock_publisher(pages):
        published.append(pages)
        return 0, {}

    bad_root = tmp_path / "nonexistent" / "file"
    bad_root.parent.mkdir()
    bad_root.write_text("not a directory")

    with pytest.raises(HostSnapshotError) as exc_info:
        publish_ordered(bad_root, PUBLIC, PRIVATE, "v1", mock_publisher)

    assert len(published) == 1
    assert "index.html" in published[0]
    assert "cycle.html" not in published[0]
    assert exc_info.value.publish_result == (0, {})


def test_server_unit_file_contains_required_security_directives():
    """ADR-036 B4: server systemd unit file has all required isolation directives."""
    unit_path = Path(__file__).resolve().parent.parent / "deploy" / "eeebot-dashboard-server.service"
    assert unit_path.is_file()
    content = unit_path.read_text(encoding="utf-8")
    required = [
        "DynamicUser=yes",
        "ReadOnlyPaths=/var/lib/eeebot-site",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "RestrictAddressFamilies=AF_INET AF_INET6",
        "InaccessiblePaths=/var/lib/eeebot-techtree /etc/eeepc-agent",
    ]
    for directive in required:
        assert directive in content, f"Missing directive {directive} in unit file"


def test_directory_listing_disabled_in_snapshot_server(tmp_path: Path):
    """ADR-036: directory listing returns 404 in SnapshotHTTPRequestHandler."""
    from scripts.two_sinks import SnapshotHTTPRequestHandler
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "hello"}, "v1")

    class MockHandler(SnapshotHTTPRequestHandler):
        def __init__(self):
            self.site_root = root
            self.error_code = None
        def send_error(self, code, message=None):
            self.error_code = code

    h = MockHandler()
    res = h.list_directory(root / "v1")
    assert res is None
    assert h.error_code == 404


def test_unchanged_pages_produce_zero_uploads_across_runs():
    """ADR-036 B2: normalized fingerprints strip volatile snapshot metadata; unchanged pages produce 0 uploads."""
    from scripts import techtree_viewer as tv
    from scripts.two_sinks import add_snapshot_version

    base_html = "<html><head><title>Test</title></head><body><h1>Content</h1></body></html>"
    p1 = add_snapshot_version({"index.html": base_html}, "v1", generated_at="2026-09-26T00:00:00Z")
    fp1 = tv._page_fingerprint(p1["index.html"])

    p2 = add_snapshot_version({"index.html": base_html}, "v2", generated_at="2026-09-26T00:05:00Z")
    fp2 = tv._page_fingerprint(p2["index.html"])

    assert fp1 == fp2, "Fingerprints must match across different snapshot versions if content is unchanged"


def test_host_snapshot_failure_preserves_gh_fingerprints_and_records_failure(tmp_path: Path, monkeypatch):
    """ADR-036: Host failure preserves gh-pages fingerprints, records host_snapshot_failed_since (not overwritten), and skips uploads on run 2."""
    import time
    from scripts import techtree_autopublish as ap

    root = tmp_path / "state"
    state_dir = tmp_path / "state_dir"
    bad_site = tmp_path / "bad_site"
    bad_site.write_text("not a directory")

    for rel, content in {
        "evolution/tree.json": '{"current_sha": "a", "nodes": {}}',
        "tech_tree/portfolio.json": '{"current": null, "nodes": {}}',
        "hypotheses/lifecycle.json": '{"entries": {}}',
        "scorecard/latest.json": '{"computed_at_utc": "2026-08-18T00:00:00Z"}',
        "ledger/cycles.jsonl": '{"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": "2026-09-25T10:00:00Z"}\n',
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    monkeypatch.setenv("GH_TOKEN", "mock-token")
    monkeypatch.setenv("EEEBOT_SITE_ROOT", str(bad_site))

    published_batches = []
    def fake_publish(pages, **kw):
        prev = kw.get("previous_fingerprints") or {}
        changed = {k: v for k, v in pages.items() if prev.get(k) != ap.tv._page_fingerprint(v)}
        published_batches.append(changed)
        fp = {k: ap.tv._page_fingerprint(v) for k, v in pages.items()}
        return 0, fp

    monkeypatch.setattr(ap.tv, "render_public_pages", lambda *a, **kw: {"index.html": "<html>fixed content</html>"})
    monkeypatch.setattr(ap.tv, "publish_to_pages", fake_publish)

    args = ap.parse_args([
        "--state-root", str(root), "--state-dir", str(state_dir), "--site-root", str(bad_site),
        "--staleness-floor-hours", "0.0001",
    ])

    rc1 = ap.run(args)
    assert rc1 == 1
    state1 = ap.load_publish_state(state_dir)
    assert state1.get("host_snapshot_failed_since") is not None
    assert state1.get("last_host_error") is not None
    assert len(state1.get("page_fingerprints", {})) > 0
    first_failed_since = state1["host_snapshot_failed_since"]
    assert len(published_batches[0]) > 0

    time.sleep(1)
    rc2 = ap.run(args)
    assert rc2 == 1
    state2 = ap.load_publish_state(state_dir)
    assert state2.get("host_snapshot_failed_since") == first_failed_since
    assert len(published_batches) == 2
    assert len(published_batches[1]) == 0


def test_goal_meta_three_states_and_rendering(tmp_path: Path):
    """ADR-036: goal_meta has absent / unexpected_shape / present states; priority_count/lines/chars null when not present."""
    from scripts.two_sinks import split_render_inputs
    from scripts import techtree_viewer as tv

    pub_absent, _ = split_render_inputs({"other": 1})
    meta_absent = pub_absent["goal_meta"]
    assert meta_absent["state"] == "absent"
    assert meta_absent["priority_count"] is None
    assert meta_absent["lines"] is None
    assert meta_absent["chars"] is None
    panel_absent = tv.build_agent_panel(None, meta_absent, None)
    assert "0 lines" not in panel_absent
    assert "0 chars" not in panel_absent
    assert "goals charter absent" in panel_absent

    pub_shape, _ = split_render_inputs({"goal_text": "plain string not a dict"})
    meta_shape = pub_shape["goal_meta"]
    assert meta_shape["state"] == "unexpected_shape"
    assert meta_shape["priority_count"] is None
    assert meta_shape["lines"] is None
    assert meta_shape["chars"] is None
    panel_shape = tv.build_agent_panel(None, meta_shape, None)
    assert "0 lines" not in panel_shape
    assert "0 chars" not in panel_shape
    assert "unexpected shape" in panel_shape

    pub_pres, _ = split_render_inputs({"goal_text": {"charter": "c1\nc2", "priorities": ["p1", "p2"]}})
    meta_pres = pub_pres["goal_meta"]
    assert meta_pres["state"] == "present"
    assert meta_pres["priority_count"] == 2
    assert meta_pres["lines"] == 2
    assert meta_pres["chars"] == 5
    panel_pres = tv.build_agent_panel(None, meta_pres, None)
    assert "Goals charter (2 lines)" in panel_pres
    assert "5 chars" in panel_pres

    pub_no_prio, _ = split_render_inputs({"goal_text": {"charter": "line1"}})
    meta_no_prio = pub_no_prio["goal_meta"]
    assert meta_no_prio["state"] == "present"
    assert meta_no_prio["priority_count"] is None


def test_publisher_service_unit_declares_site_root_writable() -> None:
    """Codex comment 4109822802: Publisher service unit must grant write access to site root."""
    unit_path = Path(__file__).resolve().parent.parent / "systemd" / "eeebot-techtree-publish.service"
    text = unit_path.read_text(encoding="utf-8")
    assert "ReadWritePaths=/var/lib/eeebot-site" in text


def test_atomic_snapshot_swap_sets_traversable_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex comment 4109822806: Staging directory and written files must be readable by DynamicUser server."""
    import os
    chmod_calls = []
    orig_chmod = os.chmod

    def fake_chmod(path, mode, *args, **kwargs):
        chmod_calls.append((Path(path), mode))
        return orig_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", fake_chmod)
    site_root = tmp_path / "site"
    atomic_snapshot_swap(site_root, {"index.html": "content"}, "v1")

    dir_chmods = [mode for path, mode in chmod_calls if path.name == "v1" or ".v1." in path.name]
    file_chmods = [mode for path, mode in chmod_calls if path.name == "index.html"]
    assert 0o755 in dir_chmods
    assert 0o644 in file_chmods


def test_f1_operator_priority_and_local_ci_output_projected_safely() -> None:
    """External review F1: operator priority label and local_ci output must not leak into public projection or HTML."""
    raw_data = {
        "derived_view": {
            "status": "present",
            "schema_version": "derived-view-v1",
            "generated_at_utc": "2026-09-25T00:00:00Z",
            "charter": {"source": "release_goals_md", "merged": False, "text": "CANARY_PUBLIC_CHARTER_OK"},
            "derived_status": "present",
            "derived_priorities": [
                {"number": 1, "label": "DERIVED_PUBLIC_LABEL_OK", "vector": "V1", "direction": "shrink"},
            ],
            "priority_items": [
                {
                    "rank": 1, "id": "p-op", "number": 42,
                    "label": "OPERATOR_SECRET_GOAL_CANARY_9876",
                    "provenance": "operator", "kind": "bug", "vector": "V1",
                    "summary": "OPERATOR_SUMMARY_CANARY_555",
                },
                {
                    "rank": 2, "id": "p-derived", "number": 1,
                    "label": "DERIVED_PUBLIC_LABEL_OK",
                    "provenance": "self-derived", "kind": "feature", "vector": "V1",
                },
            ],
        },
        "local_ci": {
            "probe": "ok",
            "state": "ran",
            "exit_code": 1,
            "summary": "FAILURES_AND_TEST_OUTPUT_CANARY_54321",
            "ts_utc": "2026-09-25T00:00:00Z",
        },
    }

    public, _ = split_render_inputs(raw_data)
    pub_json = json.dumps(public)
    assert "OPERATOR_SECRET_GOAL_CANARY_9876" not in pub_json
    assert "OPERATOR_SUMMARY_CANARY_555" not in pub_json
    assert "FAILURES_AND_TEST_OUTPUT_CANARY_54321" not in pub_json
    assert "DERIVED_PUBLIC_LABEL_OK" in pub_json
    assert "CANARY_PUBLIC_CHARTER_OK" in pub_json


def test_f10_current_alias_redirects_and_swap_preserves_old_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """External review F10: /current/... must redirect to /<version>/... to prevent reader snapshot skew."""
    from scripts.two_sinks import SnapshotHTTPRequestHandler
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "v1", "data.json": "{}"}, "v1")

    class MockHandler(SnapshotHTTPRequestHandler):
        def __init__(self, path):
            self.path = path
            self.site_root = root
            self.response_code = None
            self.headers_sent = {}
        def send_response(self, code):
            self.response_code = code
        def send_header(self, k, v):
            self.headers_sent[k] = v
        def end_headers(self):
            pass

    h = MockHandler("/current/data.json")
    h.do_GET()
    assert h.response_code == 302
    assert h.headers_sent.get("Location") == "/v1/data.json"


def test_f12_host_catches_any_exception_and_reports_cleanup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """External review F12: host sink must catch Exception (not only OSError), and cleanup failure must not be swallowed."""
    from scripts.two_sinks import HostSnapshotError
    root = tmp_path / "site"

    # Part 1: non-OSError in host snapshot must still run publisher and raise HostSnapshotError
    publisher_called = []
    def fake_publisher(pages):
        publisher_called.append(pages)
        return 0, {"index.html": "sha"}

    def failing_swap(*args, **kwargs):
        raise UnicodeEncodeError("utf-8", "lone surrogate", 0, 1, "test surrogate error")

    monkeypatch.setattr("scripts.two_sinks.atomic_snapshot_swap", failing_swap)

    with pytest.raises(HostSnapshotError):
        publish_ordered(
            root,
            {"index.html": "public"},
            {"cycle.html": "private"},
            "v1",
            publisher=fake_publisher,
        )
    assert len(publisher_called) == 1

    # Part 2: cleanup failure in atomic_snapshot_swap must not be silently swallowed
    import shutil
    def failing_rmtree(path, *args, **kwargs):
        raise PermissionError(f"denied deletion of {path}")

    monkeypatch.undo()
    monkeypatch.setattr(shutil, "rmtree", failing_rmtree)

    atomic_snapshot_swap(root, {"index.html": "v1"}, "v1")
    atomic_snapshot_swap(root, {"index.html": "v2"}, "v2")
    with pytest.raises(Exception) as exc_info:
        atomic_snapshot_swap(root, {"index.html": "v3"}, "v3")
    assert "cleanup" in str(exc_info.value).lower() or "pruning" in str(exc_info.value).lower()


def test_b1_viewer_main_publish_routes_through_split_and_publish_ordered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """External review B1: techtree_viewer main --publish must route through split_render_inputs and publish_ordered."""
    from scripts import techtree_viewer as tv

    ordered_calls = []
    def fake_publish_ordered(site_root, public_pages, private_pages, version, **kwargs):
        ordered_calls.append((public_pages, private_pages))
        return 0, {}

    monkeypatch.setattr("scripts.two_sinks.publish_ordered", fake_publish_ordered)
    monkeypatch.setattr(tv, "read_local_state", lambda *a, **kw: {"_error": None})
    monkeypatch.setattr(tv, "fetch_remote_state", lambda *a, **kw: {"_error": None})
    monkeypatch.setattr(tv, "publish_to_pages", lambda *a, **kw: pytest.fail("publish_to_pages called directly!"))

    out_dir = tmp_path / "out"
    rc = tv.main(["--local", "--state-root", str(tmp_path), "--out", str(out_dir), "--publish"])
    assert rc == 0
    assert len(ordered_calls) == 1
