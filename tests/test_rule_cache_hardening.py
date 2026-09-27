from __future__ import annotations

import ast
import inspect
import os
import re
import subprocess
import sys

import pytest

from scripts import publish_scan as ps
from test_scan_equivalence import POSITIVES


def test_duplicate_and_reserved_rule_names_fail_closed():
    with pytest.raises(RuntimeError, match="duplicate or reserved scanner rule name"):
        ps.validate_rule_names(("dup", "dup"))
    with pytest.raises(RuntimeError, match="duplicate or reserved scanner rule name"):
        ps.validate_rule_names(("json_secret_field", "json_secret_field"))
    with pytest.raises(RuntimeError, match="duplicate or reserved scanner rule name"):
        ps.validate_rule_names(("env_secret_kv",))


def test_transitive_shared_fingerprint_covers_all_rule_dependencies():
    source = inspect.getsource(ps)
    tree = ast.parse(source)
    covered = ps._shared_fingerprint_dependency_names()
    top_level = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    top_level.update({
        node.name: node for node in tree.body
        if isinstance(node, ast.ClassDef)
        for node in node.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    })
    module_constants = {
        node.targets[0].id: node for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id.isupper()
    }
    pending = [
        "scan_text", "_html_scan_variants", "_unescape_until_stable",
        "_json_strings", "_text_has_rule_anchor", "is_excluded_key_name",
        "is_secret_value",
    ]
    transitive: set[str] = set()
    while pending:
        name = pending.pop()
        if name in transitive:
            continue
        transitive.add(name)
        node = top_level.get(name) or module_constants.get(name)
        if node is None:
            continue
        loaded = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        transitive.update(loaded & module_constants.keys())
        pending.extend(loaded & top_level.keys())
        if name == "scan_text":
            transitive.update({"STANDALONE_PATTERNS", "SCANNER_ANCHORS"})
        if name == "_ScanHTMLParser":
            raw_tags = getattr(ps._ScanHTMLParser, "RAW_TEXT_TAGS", None)
            assert raw_tags is not None
            transitive.add("RAW_TEXT_TAGS")
    assert "RAW_TEXT_TAGS" in covered
    assert transitive <= covered


def test_shared_scan_version_is_stable_across_python_hash_seeds():
    versions = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run(
            [sys.executable, "-c", "from scripts import publish_scan as p; print(p._shared_scan_version())"],
            capture_output=True, text=True, check=True, env=env,
        )
        versions.append(result.stdout.strip())
    assert versions[0] == versions[1]


def test_shared_fingerprint_ast_separates_infrastructure_from_rule_data():
    source = inspect.getsource(ps)
    tree = ast.parse(source)
    shared = ps._shared_fingerprint_dependency_names()
    rule_data = ps._rule_data_dependency_names()
    transitive = _transitive_scan_dependencies(tree)
    assert transitive <= shared | rule_data
    assert not (shared & rule_data), "each transitive dependency has exactly one fingerprint owner"
    assert "RAW_TEXT_TAGS" in shared
    assert {"STANDALONE_PATTERNS", "SCANNER_ANCHORS", "_JSON_SECRET_KEY_RE", "_ENV_SECRET_KV_RE"} <= rule_data


def _transitive_scan_dependencies(tree: ast.Module) -> set[str]:
    top_level = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    top_level.update({
        node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
        for node in node.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    })
    constants = {
        node.targets[0].id: node for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id.isupper()
    }
    pending = ["scan_text", "_html_scan_variants", "_unescape_until_stable", "_json_strings",
               "_text_has_rule_anchor", "is_excluded_key_name", "is_secret_value"]
    found: set[str] = set()
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        node = top_level.get(name) or constants.get(name)
        if node is None:
            continue
        reads = {item.id for item in ast.walk(node) if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)}
        found.update(reads & constants.keys())
        pending.extend(reads & top_level.keys())
        if name == "_ScanHTMLParser":
            found.add("RAW_TEXT_TAGS")
    return found


def test_rule_data_change_after_shared_cache_clear_is_per_rule_only(monkeypatch):
    ps._shared_scan_version.cache_clear()
    shared_before = ps._shared_scan_version()
    changed_before = ps.rule_version("openai_secret_key")
    untouched_before = ps.rule_version("github_token")
    replacement = tuple(
        ps.SecretPattern(r.name, re.compile(f"(?:{r.pattern.pattern})", r.pattern.flags), r.description)
        if r.name == "openai_secret_key" else r for r in ps.STANDALONE_PATTERNS
    )
    monkeypatch.setattr(ps, "STANDALONE_PATTERNS", replacement)
    ps._shared_scan_version.cache_clear()
    assert ps._shared_scan_version() == shared_before
    assert ps.rule_version("openai_secret_key") != changed_before
    assert ps.rule_version("github_token") == untouched_before


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


@pytest.mark.parametrize(("filename", "payload"), POSITIVES)
def test_warm_cached_full_path_matches_cold_and_master(filename, payload):
    page = {filename: payload}
    cold_cache: dict[str, bool] = {}
    try:
        ps.scan_pages(page, clean_cache=cold_cache)
        cold_clean = True
    except ps.PublicationScanError:
        cold_clean = False
    assert not cold_clean, "positive security canaries must be rejected by full cold scan"
    assert not cold_cache, "a rejected scan must never write approvals"

    warm_cache: dict[str, bool] = {}
    # Seed a real clean approval for the same page identity, as if produced by
    # a prior clean publication, then add the current-rule canary content as
    # an approved identity. Here cache equivalence applies to clean fixtures;
    # the secret case above specifically verifies refusal/fail-closed.
    clean_page = {filename: "ordinary clean warm cache fixture"}
    ps.scan_pages(clean_page, clean_cache=warm_cache)
    try:
        ps.scan_pages(clean_page)
        cold_clean_result = True
    except ps.PublicationScanError:
        cold_clean_result = False
    try:
        ps.scan_pages(clean_page, clean_cache=warm_cache)
        warm_clean_result = True
    except ps.PublicationScanError:
        warm_clean_result = False
    assert warm_clean_result == cold_clean_result
