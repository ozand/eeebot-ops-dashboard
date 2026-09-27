from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest

from scripts import publish_scan as ps


POSITIVES = [
    ("cycles-archive-1.json", '{"password": "my_super_secret_password"}'),
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


@pytest.fixture(scope="module")
def master_scanner(tmp_path_factory: pytest.TempPathFactory):
    """Load the actual origin/master scanner as an independent reference."""
    source = subprocess.run(
        ["git", "show", "origin/master:scripts/publish_scan.py"],
        capture_output=True, check=False,
    )
    if source.returncode:
        pytest.skip("requires origin/master for scanner reference comparisons")
    path = tmp_path_factory.mktemp("master-scanner") / "publish_scan_master.py"
    path.write_bytes(source.stdout)
    spec = importlib.util.spec_from_file_location("publish_scan_master", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # inspect.getsource() on a class (used by the scanner's version fingerprint
    # since #343) resolves the module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assert_master_findings_preserved(
    content: str, master_scanner,
) -> dict[str, int]:
    expected = master_scanner.scan_text(content)
    actual = ps.scan_text(content)
    assert all(actual.get(name, 0) >= count for name, count in expected.items())
    return actual


@pytest.mark.parametrize(("filename", "payload"), POSITIVES)
def test_new_scanner_preserves_master_findings_for_every_positive(
    filename: str, payload: str, master_scanner,
) -> None:
    """New scan findings must include all findings from origin/master."""
    _assert_master_findings_preserved(payload, master_scanner)


@pytest.mark.parametrize(("filename", "payload"), POSITIVES)
def test_scan_pages_preserves_master_findings_for_every_positive(
    filename: str, payload: str, master_scanner,
) -> None:
    """#340: the full scan_pages path (page approval cache included) must
    still reject everything origin/master's scanner would have caught --
    equivalence through the whole path, not just scan_text directly."""
    try:
        master_scanner.scan_pages({filename: payload})
        master_rejects = False
    except master_scanner.PublicationScanError:
        master_rejects = True
    if not master_rejects:
        pytest.skip("origin/master does not reject this fixture; nothing to preserve")
    with pytest.raises(ps.PublicationScanError):
        ps.scan_pages({filename: payload})


def test_scan_pages_live_gh_pages_findings_preserve_master_and_remain_clean(master_scanner) -> None:
    """#340: same as test_live_gh_pages_findings_preserve_master_and_remain_clean
    but through scan_pages (with a fresh cache each call), proving the
    per-rule cache never masks a finding origin/master's scanner would
    also report."""
    has_remote_branch = subprocess.run(
        ["git", "rev-parse", "--verify", "origin/gh-pages"], capture_output=True, text=True, check=False
    )
    if has_remote_branch.returncode:
        pytest.skip("requires fetched origin/gh-pages branch; run git fetch origin gh-pages")
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "origin/gh-pages"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for line in listing:
        _meta, path = line.split("\t", 1)
        raw = subprocess.run(
            ["git", "show", f"origin/gh-pages:{path}"], capture_output=True, check=True
        ).stdout.decode("utf-8")
        try:
            master_scanner.scan_pages({path: raw})
        except master_scanner.PublicationScanError as exc:
            with pytest.raises(ps.PublicationScanError):
                ps.scan_pages({path: raw})
            continue
        ps.scan_pages({path: raw})  # must not raise


def test_unanchored_rule_runs_full_regex_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = ps.SecretPattern("unanchored_canary", __import__("re").compile(r"UNANCHORED_[A-Z]+"), "test")
    monkeypatch.setattr(ps, "STANDALONE_PATTERNS", (*ps.STANDALONE_PATTERNS, rule))
    assert ps.SCANNER_ANCHORS.get(rule.name) is None
    assert ps.scan_text("prefix UNANCHORED_SECRET suffix") == {rule.name: 1}


def test_live_gh_pages_findings_preserve_master_and_remain_clean(master_scanner) -> None:
    has_remote_branch = subprocess.run(
        ["git", "rev-parse", "--verify", "origin/gh-pages"], capture_output=True, text=True, check=False
    )
    if has_remote_branch.returncode:
        pytest.skip("requires fetched origin/gh-pages branch; run git fetch origin gh-pages")
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "origin/gh-pages"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for line in listing:
        _meta, path = line.split("\t", 1)
        raw = subprocess.run(
            ["git", "show", f"origin/gh-pages:{path}"], capture_output=True, check=True
        ).stdout.decode("utf-8")
        is_json = path.endswith(".json")
        optimized = _assert_master_findings_preserved(raw, master_scanner)
        assert optimized == {}, path
