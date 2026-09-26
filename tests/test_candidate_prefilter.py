from __future__ import annotations

import pytest

from scripts import publish_scan as ps


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
        assert ps._has_scan_candidate(sample, ps.STANDALONE_PATTERNS), sample
        with pytest.raises(ps.PublicationScanError):
            ps.scan_pages({"index.html": sample})


def test_candidate_guard_has_no_false_negatives_for_known_pattern_corpus():
    cases = [
        ("/etc/eeepc-agent/config", ps.STANDALONE_PATTERNS[0].pattern),
        ("sk-" + "A" * 24, ps.STANDALONE_PATTERNS[1].pattern),
        ("ghp_" + "A" * 20, ps.STANDALONE_PATTERNS[2].pattern),
        ("Bearer " + "A" * 20, ps.STANDALONE_PATTERNS[3].pattern),
        ("AKIA" + "A" * 16, ps.STANDALONE_PATTERNS[4].pattern),
        ("xoxb-" + "A" * 12, ps.STANDALONE_PATTERNS[5].pattern),
        ("Authorization: Basic " + "A" * 20, ps.STANDALONE_PATTERNS[6].pattern),
        ("https://user:password@example.test/path", ps.STANDALONE_PATTERNS[7].pattern),
        ("-----BEGIN PRIVATE KEY-----", ps.STANDALONE_PATTERNS[8].pattern),
        ('"reasoning_content"', ps.STANDALONE_PATTERNS[9].pattern),
        ('"messages":', ps.STANDALONE_PATTERNS[10].pattern),
        ('"prompt": {', ps.STANDALONE_PATTERNS[11].pattern),
        ("DB_PASS=abcdefghijk", ps._ENV_SECRET_KV_RE),
        ("AUTH=abcdefghijk", ps._ENV_SECRET_KV_RE),
        ('{"my_token_value":"abcdefghijk"}', ps._JSON_SECRET_KEY_RE),
    ]
    for sample, matcher in cases:
        assert matcher.search(sample), sample
        assert ps._has_scan_candidate(sample, ps.STANDALONE_PATTERNS), sample
