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
    combined = ps._compile_scanner_patterns(tuple(ps.STANDALONE_PATTERNS))
    names = {r.name for r in ps.STANDALONE_PATTERNS}
    result: dict[str, int] = {}
    for variant in variants:
        for match in combined.finditer(variant):
            if match.group("json_key") is not None:
                key = match.group("json_key")
                val = match.group("json_dval") or match.group("json_sval") or match.group("json_uval") or ""
                name = "json_secret_field" if not ps.is_excluded_key_name(key) and ps.is_secret_value(val) else None
            elif match.group("env_key") is not None:
                key = match.group("env_key")
                val = match.group("env_dval") or match.group("env_sval") or match.group("env_uval") or ""
                name = "env_secret_kv" if not ps.is_excluded_key_name(key) and ps.is_secret_value(val) else None
            else:
                name = next((n for n in names if match.group(n) is not None), None)
            if name:
                result[name] = result.get(name, 0) + 1
    return result


@pytest.mark.parametrize(("filename", "payload"), POSITIVES)
def test_new_scan_findings_match_combined_regex_reference(filename: str, payload: str) -> None:
    is_json = filename.endswith(".json")
    assert ps.scan_text(payload, html_mode=not is_json, json_mode=is_json) == _reference_findings(
        payload, html_mode=not is_json, json_mode=is_json
    )


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
