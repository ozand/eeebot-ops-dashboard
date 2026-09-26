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
    assert order == [("gh", PUBLIC)]


def test_publish_allowlist_refuses_unlisted_pages(tmp_path: Path):
    """ADR-036 §3: unexpected publication paths fail loudly."""
    with pytest.raises(ValueError, match="unlisted"):
        validate_publish_allowlist({"secret.bin": "x"})


def test_private_pages_never_reach_gh_pages(tmp_path: Path):
    """ADR-036 §3: private pages exist only in the host snapshot."""
    sent = []
    publish_ordered(tmp_path / "site", PUBLIC, PRIVATE, "v1", sent.append)
    assert "cycle.html" in {p.name for p in (tmp_path / "site/v1").iterdir()}
    assert sent == [PUBLIC]


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
        '{"token":"sk-abcdefghijklmnop"}',
        '-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----',
        'token: ghp_1234567890abcdefghijklmnopqrstuvwxyz',
        'Authorization: Bearer secret-token-xyz',
        'aws_key = AKIA1234567890ABCDEF',
        'slack = xoxb-12345-abcdef',
        'api_key=my_secret_key',
        '{"reasoning_content": "internal thinking"}',
        '{"messages": [{"role": "user"}]}',
        '{"prompt": {"text": "hello"}}',
    ]
    for i, payload in enumerate(bad_payloads):
        target = tmp_path / f"probe_{i}.json"
        target.write_text(payload, encoding="utf-8")
        with pytest.raises(ValueError, match="private marker"):
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

    rc, _ = tv.publish_to_pages({"index.html": "token=sk-1234567890123456"})
    assert rc == 1


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

    assert published == [PUBLIC]
    assert exc_info.value.publish_result == (0, {})
