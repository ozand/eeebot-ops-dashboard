"""#340: page approvals are keyed by page sha256 + the version of EACH
rule, recording which rules the approval covers. When one rule's version
changes, only that rule re-scans; every other rule's approval is reused.
Every rule still scans the whole page (no fragment splitting, no seams).
"""
from __future__ import annotations

import re
import time

import pytest

from scripts import publish_scan as ps


def _bump_one_rule_version(monkeypatch: pytest.MonkeyPatch, rule_name: str) -> None:
    """Change ONE rule's own pattern text without changing what it
    matches (wraps it in a no-op non-capturing group) -- its version
    changes; every other rule's does not."""
    new_patterns = []
    for rule in ps.STANDALONE_PATTERNS:
        if rule.name == rule_name:
            new_patterns.append(
                ps.SecretPattern(rule.name, re.compile(f"(?:{rule.pattern.pattern})", rule.pattern.flags), rule.description)
            )
        else:
            new_patterns.append(rule)
    monkeypatch.setattr(ps, "STANDALONE_PATTERNS", tuple(new_patterns))


def test_changing_one_rule_version_rescans_only_that_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    changed = "openai_secret_key"
    pages = {
        "index.html": "ordinary clean page A content, nothing sensitive here at all",
        "tokens.html": "ordinary clean page B content, also nothing sensitive whatsoever",
    }
    cache: dict[str, bool] = {}
    ps.scan_pages(dict(pages), clean_cache=cache)
    before = dict(cache)
    assert before
    version_before = ps.rule_version(changed)

    # Every rule approved for every page before the version bump.
    for fname in pages:
        content_sha = __import__("hashlib").sha256(pages[fname].encode("utf-8")).hexdigest()
        for name in ps.rule_names():
            assert cache.get(ps.rule_cache_key(name, content_sha, mode="html", extra_version=ps.inherited_blob_decoder_version())) is True

    _bump_one_rule_version(monkeypatch, changed)
    version_after = ps.rule_version(changed)
    assert version_after != version_before, "the bump must actually change the rule's own version"

    calls: list[list[str] | None] = []
    real_scan_text = ps.scan_text

    def _spy_scan_text(content, **kwargs):
        calls.append(list(kwargs.get("rules")) if kwargs.get("rules") is not None else None)
        return real_scan_text(content, **kwargs)

    monkeypatch.setattr(ps, "scan_text", _spy_scan_text)

    ps.scan_pages(dict(pages), clean_cache=cache)

    # Every scan_text call this run only asked for the ONE changed rule --
    # not a full re-scan of every rule.
    assert calls, "scan_text must have been invoked for the stale rule"
    for requested_rules in calls:
        assert requested_rules == [changed], requested_rules

    for fname in pages:
        content_sha = __import__("hashlib").sha256(pages[fname].encode("utf-8")).hexdigest()
        for name in ps.rule_names():
            key = ps.rule_cache_key(name, content_sha, mode="html", extra_version=ps.inherited_blob_decoder_version())
            assert cache.get(key) is True, name
            if name == changed:
                assert key not in before, "the changed rule's cache key must be a NEW version"
            else:
                assert key in before, f"{name}'s approval must be reused, not a new key"


def test_new_rule_added_only_that_rule_scans_existing_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a brand-new rule behaves the same as changing an existing
    one: only the new rule (never seen before) is stale."""
    pages = {"index.html": "ordinary clean page content"}
    cache: dict[str, bool] = {}
    ps.scan_pages(dict(pages), clean_cache=cache)
    before = dict(cache)

    new_rule = ps.SecretPattern("rule_cache_test_canary", re.compile(r"RULE_CACHE_TEST_CANARY_NO_MATCH"), "test")
    monkeypatch.setattr(ps, "STANDALONE_PATTERNS", (*ps.STANDALONE_PATTERNS, new_rule))

    calls: list[list[str] | None] = []
    real_scan_text = ps.scan_text

    def _spy_scan_text(content, **kwargs):
        calls.append(list(kwargs.get("rules")) if kwargs.get("rules") is not None else None)
        return real_scan_text(content, **kwargs)

    monkeypatch.setattr(ps, "scan_text", _spy_scan_text)
    ps.scan_pages(dict(pages), clean_cache=cache)

    assert calls == [["rule_cache_test_canary"]]
    for key in before:
        assert cache.get(key) is True, "every pre-existing rule approval must be untouched"


def test_measured_cold_after_single_rule_change_beats_a_full_cold_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """#340 measurement: shared HTML/JSON extraction still runs on any
    stale page (there is no fragment cache, no seams), so a single-rule
    cold pass is NOT as fast as a fully-warm repeat -- but it must still
    be meaningfully faster than a full cold scan of every rule, proving
    only the changed rule actually re-ran."""
    content = (
        '<!doctype html><html><body>' +
        ('<p class="status">ordinary dashboard text and counters 123456</p>' * 20000) +
        '</body></html>'
    )
    pages = {"lineage.html": content}

    full_cold_cache: dict[str, bool] = {}
    start = time.perf_counter()
    ps.scan_pages(dict(pages), clean_cache=full_cold_cache)
    full_cold_seconds = time.perf_counter() - start

    warm_cache: dict[str, bool] = dict(full_cold_cache)
    start = time.perf_counter()
    ps.scan_pages(dict(pages), clean_cache=warm_cache)
    warm_seconds = time.perf_counter() - start

    single_rule_cache: dict[str, bool] = dict(full_cold_cache)
    _bump_one_rule_version(monkeypatch, "openai_secret_key")
    start = time.perf_counter()
    ps.scan_pages(dict(pages), clean_cache=single_rule_cache)
    single_rule_cold_seconds = time.perf_counter() - start

    assert warm_seconds < single_rule_cold_seconds < full_cold_seconds, (
        f"warm={warm_seconds:.4f}s, single-rule-cold={single_rule_cold_seconds:.4f}s, "
        f"full-cold={full_cold_seconds:.4f}s -- single-rule must land strictly between"
    )
    assert single_rule_cold_seconds < full_cold_seconds * 0.7, (
        f"single-rule cold ({single_rule_cold_seconds:.4f}s) should be well under "
        f"full cold ({full_cold_seconds:.4f}s) -- only one of ~{len(ps.rule_names())} rules re-ran"
    )
