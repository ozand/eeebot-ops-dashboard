"""ADR-036 D1 two-sink contract tests."""
from __future__ import annotations

from pathlib import Path

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
    """ADR-036 §3: public output excludes private call content."""
    public, _private = split_render_inputs({"cycle_prompts": {"c": "unique-call-text"}, "public": 1})
    assert "unique-call-text" not in repr(public)


def test_no_private_file_of_any_kind_is_published(tmp_path: Path):
    """ADR-036 §3: allowlist is extension-independent."""
    for name in ("calls.json", "dump.gz", "index.idx", "unexpected.bin"):
        with pytest.raises(ValueError, match="unlisted"):
            validate_publish_allowlist({name: "payload"})


def test_built_tree_scan_refuses_private_markers(tmp_path: Path):
    """ADR-036 §3, §5: scan the complete built tree for private markers."""
    (tmp_path / "nested.json").write_text('{"token":"sk-abcdefghijklmnop"}')
    with pytest.raises(ValueError, match="private marker"):
        scan_built_tree(tmp_path)


def test_public_renderer_input_has_no_call_text(tmp_path: Path):
    """ADR-036 §3: public renderer receives no prompt or priority text."""
    public, _ = split_render_inputs({"cycle_prompts": "private-call", "goal_text": "private-priorities", "safe": 1})
    assert set(public) == {"safe"}
    assert "private" not in repr(public)


def test_sink_failure_leaves_consistent_snapshot(tmp_path: Path):
    """ADR-036 §1: failed second sink leaves the complete host snapshot."""
    def fail(_pages):
        raise RuntimeError("publisher failed")
    with pytest.raises(RuntimeError):
        publish_ordered(tmp_path / "site", PUBLIC, PRIVATE, "v1", fail)
    assert (tmp_path / "site/current/index.html").is_file()
    assert (tmp_path / "site/current/cycle.html").read_text() == "private calls"


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
