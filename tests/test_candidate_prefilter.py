from __future__ import annotations

import re

import pytest

from scripts import publish_scan as ps


def _new_guard(sample: str, patterns=ps.STANDALONE_PATTERNS) -> bool:
    for rule in patterns:
        anchors = ps.SCANNER_ANCHORS.get(rule.name)
        if not anchors:
            return True
        if ps._text_has_rule_anchor(sample, anchors, rule.pattern):
            return True
    return bool(ps._JSON_CANDIDATE_RE.search(sample) or ps._ENV_KEY_CANDIDATE_RE.search(sample))


def test_all_secret_key_shapes_are_candidates_before_full_scan():
    samples = [
        ("DB_PASS=abcdefghijk", ps._ENV_SECRET_KV_RE),
        ("AUTH=abcdefghijk", ps._ENV_SECRET_KV_RE),
        ('{"my_token_value":"abcdefghijk"}', ps._JSON_SECRET_KEY_RE),
        ("Basic " + "A" * 16, ps.STANDALONE_PATTERNS[6].pattern),
        ("Authorization : Basic " + "A" * 16, ps.STANDALONE_PATTERNS[6].pattern),
        ("ACCOUNT_KEY=abcdefghijk", ps._ENV_SECRET_KV_RE),
        ("GH_TOKEN=abcdefghijk", ps._ENV_SECRET_KV_RE),
    ]
    for sample, matcher in samples:
        assert matcher.search(sample)
        assert _new_guard(sample), sample
        with pytest.raises(ps.PublicationScanError):
            ps.scan_pages({"index.html": sample})


def test_missing_anchor_fails_closed():
    from scripts.publish_scan import SecretPattern
    unknown = SecretPattern("unanchored_rule", re.compile(r"CREDENTIAL_[A-Z]+"), "test")
    assert _new_guard("ordinary clean text", (*ps.STANDALONE_PATTERNS, unknown))


def test_each_scanner_rule_has_declared_anchor():
    rules = (
        *ps.STANDALONE_PATTERNS,
        ps.SecretPattern("_env_secret_kv", ps._ENV_SECRET_KV_RE, "env"),
        ps.SecretPattern("_json_secret_key", ps._JSON_SECRET_KEY_RE, "json"),
    )
    for rule in rules:
        anchors = ps.SCANNER_ANCHORS.get(rule.name)
        assert anchors, rule.name
        for sample in _positive_samples(rule.name):
            matcher = (ps._JSON_SECRET_KEY_RE if rule.name == "_json_secret_key"
                       else ps._ENV_SECRET_KV_RE if rule.name == "_env_secret_kv"
                       else rule.pattern)
            assert matcher.search(sample), (rule.name, sample)
            assert _new_guard(sample), (rule.name, sample)


def _positive_samples(name):
    return {
        "eeepc_agent_path": ["/etc/eeepc-agent"],
        "openai_secret_key": ["sk-" + "A" * 24],
        "github_token": ["ghp_" + "A" * 20, "github_pat_" + "A" * 20],
        "bearer_token": ["Bearer " + "A" * 20],
        "aws_access_key": ["AKIA" + "A" * 16],
        "slack_token": ["xoxb-" + "A" * 12],
        "basic_auth": ["Basic " + "A" * 16, "Authorization : Basic " + "A" * 16],
        "url_credentials": ["http://u:p@h", "https://u:p@h"],
        "private_key_header": ["-----BEGIN PRIVATE KEY-----"],
        "structural_reasoning_content": ['"reasoning_content"', "'reasoning_content'"],
        "structural_messages": ['"messages":', "'messages':"],
        "structural_prompt": ['"prompt": {', "'prompt': {"],
        "_env_secret_kv": ["DB_PASS=abcdefghijk", "AUTH=abcdefghijk", "ACCOUNT_KEY=abcdefghijk", "GH_TOKEN=abcdefghijk"],
        "_json_secret_key": ['{"my_token_value":"abcdefghijk"}'],
    }[name]


def test_candidate_guard_has_no_false_negatives_for_known_pattern_corpus():
    for rule in ps.STANDALONE_PATTERNS:
        for sample in _positive_samples(rule.name):
            assert rule.pattern.search(sample), sample
            assert _new_guard(sample), sample
