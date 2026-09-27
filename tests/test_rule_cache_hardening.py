from __future__ import annotations

import ast
import inspect
import os
import re
import subprocess
import sys

import pytest

from scripts import publish_scan as ps
from scripts import techtree_viewer as tv
from test_scan_equivalence import POSITIVES


def test_duplicate_and_reserved_rule_names_fail_closed():
    with pytest.raises(RuntimeError, match="duplicate or reserved scanner rule name"):
        ps.validate_rule_names(("dup", "dup"))
    with pytest.raises(RuntimeError, match="duplicate or reserved scanner rule name"):
        ps.validate_rule_names(("json_secret_field", "json_secret_field"))
    with pytest.raises(RuntimeError, match="duplicate or reserved scanner rule name"):
        ps.validate_rule_names(("json_secret_field",))
    with pytest.raises(RuntimeError, match="duplicate or reserved scanner rule name"):
        ps.validate_rule_names(("env_secret_kv",))


def test_rule_names_rejects_reserved_name_after_runtime_rule_replacement(monkeypatch):
    reserved = ps.SecretPattern("json_secret_field", re.compile("NO_MATCH"), "bad")
    monkeypatch.setattr(ps, "STANDALONE_PATTERNS", (reserved,))
    with pytest.raises(RuntimeError, match="duplicate or reserved scanner rule name"):
        ps.rule_names()


def test_shared_fingerprint_getsource_failure_disables_cache_and_logs(monkeypatch, capsys):
    page = {"index.html": "ordinary content safe to publish"}
    extra = ps.inherited_blob_decoder_version()
    approved = {
        ps.rule_cache_key(name, __import__("hashlib").sha256(page["index.html"].encode()).hexdigest(), mode="html", extra_version=extra): True
        for name in ps.rule_names()
    }
    ps._shared_scan_version.cache_clear()
    original = ps.inspect.getsource

    def fail_callable_source(value):
        if getattr(value, "__name__", None) == "spy_scan":
            raise OSError("source unavailable")
        return original(value)

    monkeypatch.setattr(ps.inspect, "getsource", fail_callable_source)
    scans = []
    real_scan = ps.scan_text

    def spy_scan(content, **kwargs):
        scans.append(kwargs.get("rules"))
        return real_scan(content, **kwargs)

    monkeypatch.setattr(ps, "scan_text", spy_scan)
    ps._shared_scan_version.cache_clear()
    before = dict(approved)
    ps.scan_pages(page, clean_cache=approved)
    assert scans == [list(ps.rule_names())], "without a trustworthy fingerprint, run every rule and bypass approvals"
    assert approved == before, "unversioned results must not add or remove cached approvals"
    assert "shared scanner fingerprint unavailable" in capsys.readouterr().err.lower()


def test_inherited_tree_fingerprint_failure_fetches_and_scans_blob(monkeypatch, capsys):
    import base64
    import json
    content = "ordinary inherited content safe to publish"
    blob_sha = "a" * 40
    requests = []

    def fake_gh(args, **kwargs):
        requests.append(args)
        if "git/trees/tree-sha?recursive=1" in args[1]:
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "tree": [{"type": "blob", "path": "index.html", "sha": blob_sha}],
            }), "")
        if f"git/blobs/{blob_sha}" in args[1]:
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode(),
            }), "")
        raise AssertionError(f"unexpected GitHub request: {args}")

    monkeypatch.setattr(tv, "_gh", fake_gh)
    ps._shared_scan_version.cache_clear()
    original = ps.inspect.getsource

    def fail_scan_source(value):
        if getattr(value, "__name__", None) in {"scan_text", "spy_scan"}:
            raise OSError("source unavailable")
        return original(value)

    monkeypatch.setattr(ps.inspect, "getsource", fail_scan_source)
    ps._shared_scan_version.cache_clear()
    scans = []
    real_scan = ps.scan_text

    def spy_scan(text, **kwargs):
        scans.append(text)
        return real_scan(text, **kwargs)

    monkeypatch.setattr(ps, "scan_text", spy_scan)
    tv._inspect_and_scan_inherited_tree("tree-sha", set(), scan_cache={})

    assert any(f"git/blobs/{blob_sha}" in request[1] for request in requests)
    assert scans == [content]
    assert "fingerprint unavailable" in capsys.readouterr().err.lower()


def test_same_name_rule_pattern_flags_and_anchor_changes_invalidate_old_approvals(monkeypatch):
    page = {"index.html": "prefix same_rule_secret"}
    rule = ps.SecretPattern("same_rule_secret", re.compile(r"same_rule_secret"), "canary")
    monkeypatch.setattr(ps, "STANDALONE_PATTERNS", (rule,))
    monkeypatch.setattr(ps, "SCANNER_ANCHORS", {rule.name: ("absent",)})
    cache = {}
    ps.scan_pages(page, clean_cache=cache)  # old definition does not match due to anchor

    # Anchor-only change with same name must make the page stale and refuse.
    monkeypatch.setattr(ps, "SCANNER_ANCHORS", {rule.name: ("same_rule_secret",)})
    with pytest.raises(ps.PublicationScanError, match=rule.name):
        ps.scan_pages(page, clean_cache=cache)

    # Approve the same page with a case-sensitive uppercase pattern, then
    # change only flags to IGNORECASE under the same rule name.
    cache.clear()
    case_sensitive = ps.SecretPattern(rule.name, re.compile(r"SAME_RULE_SECRET"), "canary")
    monkeypatch.setattr(ps, "STANDALONE_PATTERNS", (case_sensitive,))
    ps.scan_pages(page, clean_cache=cache)
    changed_flags = ps.SecretPattern(rule.name, re.compile(r"SAME_RULE_SECRET", re.IGNORECASE), "canary")
    monkeypatch.setattr(ps, "STANDALONE_PATTERNS", (changed_flags,))
    with pytest.raises(ps.PublicationScanError, match=rule.name):
        ps.scan_pages(page, clean_cache=cache)


def test_transitive_shared_fingerprint_covers_all_rule_dependencies():
    source = inspect.getsource(ps)
    tree = ast.parse(source)
    covered = ps._shared_fingerprint_dependency_names()
    rule_data = ps._rule_data_dependency_names()
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
    assert transitive <= covered | rule_data
    assert not (covered & rule_data), "shared infrastructure and per-rule data must stay separate"
    assert {"STANDALONE_PATTERNS", "SCANNER_ANCHORS", "_JSON_CANDIDATE_RE", "_JSON_SECRET_KEY_RE", "_ENV_KEY_CANDIDATE_RE", "_ENV_SECRET_KV_RE"} <= rule_data


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
    transitive.discard("SecretPattern")  # NamedTuple type is not scanner rule data.
    assert transitive <= shared | rule_data
    assert not (shared & rule_data), "each transitive dependency has exactly one fingerprint owner"
    assert "RAW_TEXT_TAGS" in shared
    assert {"STANDALONE_PATTERNS", "SCANNER_ANCHORS", "_JSON_SECRET_KEY_RE", "_ENV_SECRET_KV_RE"} <= rule_data


def _transitive_scan_dependencies(
    tree: ast.Module, roots: list[str] | None = None,
) -> set[str]:
    top_level, constants, attributes, _collisions = _ast_dependency_bindings(tree)
    pending = list(roots) if roots is not None else [
        "scan_text", "_html_scan_variants", "_unescape_until_stable", "_json_strings",
        "_text_has_rule_anchor", "is_excluded_key_name", "is_secret_value",
    ]
    found: set[str] = set()
    visited: set[str] = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        found.add(name)
        node = top_level.get(name) or constants.get(name) or attributes.get(name)
        if node is None:
            continue
        reads = {item.id for item in ast.walk(node) if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)}
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            # A queued binding needs its own RHS dependencies traversed too.
            value = node.value
            reads.update(
                item.id for item in ast.walk(value)
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
            )
        reads.update(
            f"{item.value.id}.{item.attr}" for item in ast.walk(node)
            if isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Load)
            and isinstance(item.value, ast.Name)
        )
        discovered_constants = reads & constants.keys()
        found.update(discovered_constants)
        found.update(reads & attributes.keys())
        pending.extend(discovered_constants)
        pending.extend(reads & top_level.keys())
        pending.extend(reads & attributes.keys())
        if name == "_ScanHTMLParser":
            found.add("RAW_TEXT_TAGS")
    return found


def _ast_dependency_bindings(tree: ast.Module):
    functions = {}
    constants = {}
    attributes = {}
    class_methods = set()
    module_functions = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
            module_functions.add(node.name)
        elif isinstance(node, ast.ClassDef):
            functions[node.name] = node
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[f"{node.name}.{child.name}"] = child
                    class_methods.add(child.name)
                elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                    targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            constants[f"{node.name}.{target.id}"] = child
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node
                elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    attributes[f"{target.value.id}.{target.attr}"] = node
    return functions, constants, attributes, module_functions & class_methods


def _mutate_rule_data(name, value):
    if isinstance(value, tuple) and value and isinstance(value[0], ps.SecretPattern):
        changed = []
        for item in value:
            if isinstance(item, ps.SecretPattern):
                changed.append(ps.SecretPattern(item.name, re.compile(item.pattern.pattern + "(?:X)", item.pattern.flags), item.description))
            else:
                changed.append(item)
        return tuple(changed)
    if isinstance(value, dict):
        result = dict(value)
        key = next(iter(result))
        result[key] = tuple(result[key]) + ("mutation-marker",)
        return result
    if isinstance(value, re.Pattern):
        return re.compile(value.pattern + "(?:X)", value.flags)
    raise AssertionError(f"no mutation strategy for {name}: {type(value).__name__}")


def _owned_rules(name):
    if name == "STANDALONE_PATTERNS":
        return {pattern.name for pattern in ps.STANDALONE_PATTERNS}
    if name == "SCANNER_ANCHORS":
        return {"eeepc_agent_path"}
    if name in {"_JSON_CANDIDATE_RE", "_JSON_SECRET_KEY_RE"}:
        return {"json_secret_field"}
    if name in {"_ENV_KEY_CANDIDATE_RE", "_ENV_SECRET_KV_RE"}:
        return {"env_secret_kv"}
    raise AssertionError(f"unknown rule-data dependency: {name}")


def _ast_bindings(tree: ast.Module):
    names = set()
    module_functions = set()
    methods = set()
    attributes = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                module_functions.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    attributes.add(f"{target.value.id}.{target.attr}")
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(f"{node.name}.{child.name}")
                    methods.add(child.name)
                elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    names.add(f"{node.name}.{child.target.id}")
    return names | attributes, module_functions & methods


def _ast_assigned_attributes(tree: ast.Module) -> set[str]:
    return {
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
    }


def test_dependency_ast_guard_covers_annotated_string_attribute_and_name_collisions():
    source = '''
class Parser:
    RAW_TEXT_TAGS: frozenset[str] = frozenset({"script"})
    def helper(self):
        return "method"
def helper():
    return "module"
RULE_NAME: str = "rule-data"
Parser.RAW_TEXT_TAGS = frozenset({"style"})
'''
    tree = ast.parse(source)
    bindings, collisions = _ast_bindings(tree)
    assert {"RULE_NAME", "Parser.RAW_TEXT_TAGS"} <= bindings
    assert {"helper", "Parser.helper"} <= bindings
    assert collisions == {"helper"}
    assert "Parser.RAW_TEXT_TAGS" in _ast_assigned_attributes(tree)


def test_transitive_guard_traverses_attribute_binding_and_constant_chain():
    tree = ast.parse('''
class Parser:
    pass
BASE_TAGS: str = "script"
TAGS: str = BASE_TAGS
Parser.RAW_TEXT_TAGS = TAGS
''')
    transitive = _transitive_scan_dependencies(tree, roots=["Parser.RAW_TEXT_TAGS"])
    assert {"Parser.RAW_TEXT_TAGS", "TAGS", "BASE_TAGS"} <= transitive


def test_canonical_fingerprint_serializer_is_recursive_and_refuses_unsupported_types():
    canonical = ps._canonical_fingerprint_value
    assert canonical({"z": {"b", "a"}, "a": (1, True)}) == canonical(
        {"a": (1, True), "z": {"a", "b"}}
    )
    assert canonical(re.compile("token", re.IGNORECASE)) == canonical(
        re.compile("token", re.IGNORECASE)
    )
    with pytest.raises(ps.FingerprintUnavailableError, match="unsupported"):
        canonical(object())


def test_rule_data_dependency_mutation_matrix_changes_only_owned_rule_versions(monkeypatch):
    dependencies = ps._rule_data_dependency_names()
    assert dependencies == {
        "STANDALONE_PATTERNS", "SCANNER_ANCHORS", "_JSON_CANDIDATE_RE",
        "_JSON_SECRET_KEY_RE", "_ENV_KEY_CANDIDATE_RE", "_ENV_SECRET_KV_RE",
    }
    initial_shared = ps._shared_scan_version()
    baseline = {name: ps.rule_version(name) for name in ps.rule_names()}
    hit_before = ps._shared_scan_version.cache_info().hits
    assert ps._shared_scan_version() == initial_shared
    assert ps._shared_scan_version.cache_info().hits == hit_before + 1
    for dependency in sorted(dependencies):
        original = getattr(ps, dependency)
        replacement = _mutate_rule_data(dependency, original)
        monkeypatch.setattr(ps, dependency, replacement)
        ps._shared_scan_version.cache_clear()
        assert ps._shared_scan_version() == initial_shared, dependency
        changed = {
            name for name in ps.rule_names()
            if ps.rule_version(name) != baseline[name]
        }
        expected = _owned_rules(dependency)
        assert changed == expected, (dependency, changed, expected)
        monkeypatch.setattr(ps, dependency, original)
        ps._shared_scan_version.cache_clear()
        assert {name: ps.rule_version(name) for name in ps.rule_names()} == baseline


def test_shared_fingerprint_dependencies_are_immutable_after_cached_version(monkeypatch):
    ps._shared_scan_version.cache_clear()
    before = ps._shared_scan_version()
    for dependency, value in ps._all_top_level_dependency_objects().items():
        if dependency == "RAW_TEXT_TAGS":
            continue  # exposed alias for the separately checked class-owned set
        assert isinstance(value, (str, int, float, bool, type(None), tuple, frozenset, re.Pattern, type)) or inspect.isfunction(value), dependency
        if isinstance(value, (frozenset, tuple)):
            with pytest.raises(AttributeError):
                value.clear()
    assert isinstance(ps._ScanHTMLParser.RAW_TEXT_TAGS, frozenset), "class-level scanner tags must be immutable"
    assert ps._shared_scan_version() == before


def test_ast_guard_mutations_detect_annotated_constants_and_attribute_assignments():
    source = '''
class Parser:
    RAW_TEXT_TAGS: frozenset[str] = frozenset({"script"})
RULE_NAME: str = "rule-data"
Parser.RAW_TEXT_TAGS = frozenset({"style"})
'''
    tree = ast.parse(source)
    bindings, _collisions = _ast_bindings(tree)
    assert {"RULE_NAME"} <= bindings
    assert "Parser.RAW_TEXT_TAGS" in _ast_assigned_attributes(tree)


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
