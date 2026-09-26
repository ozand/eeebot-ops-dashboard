from __future__ import annotations

import subprocess

import pytest

from scripts import publish_scan as ps


POSITIVES = [
    ("index.html", "/etc/eeepc-agent/litellm.env"),
    ("index.html", "sk-12345678901234567890abcdef"),
    ("index.html", "ghp_1234567890123456"),
    ("index.html", "Bearer mytoken1234567890abcdef"),
    ("index.html", "AKIAIOSFODNN7EXAMPLE"),
    ("index.html", "xoxb-1234567890-abcdef123456"),
    ("index.html", "Basic dXNlcjpwYXNzd29yZDEyMzQ="),
    ("index.html", "Authorization : Basic dXNlcjpwYXNzd29yZDEyMzQ="),
    ("index.html", "https://admin:secretpass123@example.com/api"),
    ("index.html", "-----BEGIN PRIVATE KEY-----"),
    ("index.html", "<code>ghp_<span>abcdefghijklmnop123456</span></code>"),
    ("index.html", '<div>{&quot;<span>messages</span>&quot;: []}</div>'),
    ("index.html", "<div>{&amp;quot;messages&amp;quot;: []}</div>"),
    ("index.html", '<script>document.body.innerHTML=\'ghp_<span class="red">abcdefghijklmnop123456</span>\'</script>'),
    ("index.html", 'API_KEY="mysecrettoken12345"'),
    ("index.html", "GH_TOKEN='mysecrettoken12345'"),
    ("index.html", '{"password": "my_super_secret_password"}'),
    ("index.html", '{"api_key": "live_apikey_abcdef123456"}'),
    ("index.html", '{"token": "session_token_xyz987654"}'),
    ("index.html", "{\n'api_key': 'secret123456'\n}"),
    ("index.html", 'PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\\nMIIEogIBAAKCAQEA0Y\\n-----END RSA PRIVATE KEY-----"'),
    ("index.html", "Authorization: sk-super_secret_canary_value_9988776655"),
    ("index.html", "password=abc1234567890; password=another-secret-999"),
    ("index.html", "basic dXNlcjpwYXNzd29yZDEyMzQ="),
    ("index.html", "https://u:p@example.test http://u:p@example.test"),
    ("index.html", '<p title="ghp_abcdefghijklmnop123456" data-other="value"></p>'),
    ("index.html", "<p>API_KEY=abc<span></span>def12345</p>"),
    ("index.html", "<p>{'messages': [{'role': 'user'}]}</p>"),
    ("index.html", "<div>ghp_&lt;span&gt;abcdefghijklmnop123456&lt;/span&gt;</div>"),
    ("index.html", "<div>{&amp;#34;password&amp;#34;: &amp;#34;secret123456&amp;#34;}</div>"),
    ("index.html", "'auth_token': 'mysecretvalue'"),
    ("cycles-archive-1.json", '{"excerpt":"{&quot;messages&quot;: []}"}'),
]


def _reference_findings(content: str, *, html_mode: bool, json_mode: bool) -> dict[str, int]:
    variants = [content, ps._unescape_until_stable(content)]
    if json_mode:
        import json
        try:
            decoded = json.loads(content)
        except (json.JSONDecodeError, UnicodeError):
            pass
        else:
            strings = [ps._unescape_until_stable(v) for v in ps._json_strings(decoded)]
            if strings:
                variants.append("\x00".join(strings))
    elif html_mode:
        variants.extend(ps._html_scan_variants(variants[-1]))
    variants = list(dict.fromkeys(variants))
    result: dict[str, int] = {}
    for rule in ps.STANDALONE_PATTERNS:
        count = max((len(list(rule.pattern.finditer(v))) for v in variants), default=0)
        if count:
            result[rule.name] = count
    json_count = 0
    env_count = 0
    for variant in variants:
        json_hits = 0
        for match in ps._JSON_SECRET_KEY_RE.finditer(variant):
            key = match.group(1)
            val = match.group(2) or match.group(3) or ""
            if not ps.is_excluded_key_name(key) and ps.is_secret_value(val):
                json_hits += 1
        json_count = max(json_count, json_hits)
        env_hits = 0
        for match in ps._ENV_SECRET_KV_RE.finditer(variant):
            key = match.group(1)
            val = match.group(2) or match.group(3) or match.group(4) or ""
            if not ps.is_excluded_key_name(key) and ps.is_secret_value(val):
                env_hits += 1
        env_count = max(env_count, env_hits)
    if json_count:
        result["json_secret_field"] = json_count
    if env_count:
        result["env_secret_kv"] = env_count
    return result


@pytest.mark.parametrize(("filename", "payload"), POSITIVES)
def test_new_scan_findings_match_combined_regex_reference(filename: str, payload: str) -> None:
    is_json = filename.endswith(".json")
    assert ps.scan_text(payload, html_mode=not is_json, json_mode=is_json) == _reference_findings(
        payload, html_mode=not is_json, json_mode=is_json
    )


def test_unanchored_rule_runs_full_regex_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = ps.SecretPattern("unanchored_canary", __import__("re").compile(r"UNANCHORED_[A-Z]+"), "test")
    monkeypatch.setattr(ps, "STANDALONE_PATTERNS", (*ps.STANDALONE_PATTERNS, rule))
    assert ps.SCANNER_ANCHORS.get(rule.name) is None
    assert ps.scan_text("prefix UNANCHORED_SECRET suffix") == {rule.name: 1}


def test_live_gh_pages_findings_match_reference_and_remain_clean() -> None:
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "origin/gh-pages"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for line in listing:
        _meta, path = line.split("\t", 1)
        raw = subprocess.run(
            ["git", "show", f"origin/gh-pages:{path}"], capture_output=True, check=True
        ).stdout.decode("utf-8")
        is_json = path.endswith(".json")
        expected = _reference_findings(raw, html_mode=not is_json, json_mode=is_json)
        actual = ps.scan_text(raw, html_mode=not is_json, json_mode=is_json)
        assert actual == expected, path
        assert actual == {}, path
