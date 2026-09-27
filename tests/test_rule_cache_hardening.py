from __future__ import annotations

import ast
import hashlib
import re

import pytest

from scripts import publish_scan as ps


def test_duplicate_and_reserved_rule_names_fail_closed():
    with pytest.raises(RuntimeError, match="duplicate or reserved scanner rule name"):
        ps.validate_rule_names(("dup", "dup"))
    with pytest.raises(RuntimeError, match="duplicate or reserved scanner rule name"):
        ps.validate_rule_names(("json_secret_field", "json_secret_field"))
    with pytest.raises(RuntimeError, match="duplicate or reserved scanner rule name"):
        ps.validate_rule_names(("env_secret_kv",))


def test_transitive_shared_fingerprint_covers_all_rule_dependencies():
    tree = ast.parse(__import__("inspect").getsource(ps))
    covered = ps._shared_fingerprint_dependency_names()
    constants = {
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.isupper()
    }
    assert "RAW_TEXT_TAGS" in constants
    assert constants <= covered


def test_new_rule_that_detects_previously_approved_page_refuses_publication(monkeypatch):
    page = {"index.html": "public page contains UNIQUE_SECRET_CANARY_98231"}
    cache: dict[str, bool] = {}
    ps.scan_pages(page, clean_cache=cache)
    new_rule = ps.SecretPattern(
        "new_secret_canary", re.compile(r"UNIQUE_SECRET_CANARY_98231"), "test canary",
    )
    monkeypatch.setattr(ps, "STANDALONE_PATTERNS", (*ps.STANDALONE_PATTERNS, new_rule))
    with pytest.raises(ps.PublicationScanError, match="new_secret_canary"):
        ps.scan_pages(page, clean_cache=cache)


def test_warm_cache_result_matches_cold_full_scan():
    page = {"index.html": '<main>safe</main>', "cycles-archive-1.json": '{"n":1}'}
    cold_cache: dict[str, bool] = {}
    ps.scan_pages(page, clean_cache=cold_cache)
    warm_cache = dict(cold_cache)
    # Warm scan should accept the same page set without rescanning.
    ps.scan_pages(page, clean_cache=warm_cache)
    assert warm_cache == cold_cache
    for name, content in page.items():
        expected = ps.scan_text(content, html_mode=not name.endswith(".json"), json_mode=name.endswith(".json"))
        assert expected == {}
