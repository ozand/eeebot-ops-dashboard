"""ADR-036: Tests for publication rejection scanner and leak prevention.

Verifies that:
- Every prohibited credential, path, and call-text marker triggers rejection.
- Secret values are never included in exception messages or reports.
- Current public fixture pages render cleanly with zero false positives.
- publish_to_pages and autopublish dry-run enforce the scanner gate.
- Inherited base_tree files are inspected and rejected if contaminated.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import publish_scan as ps
from scripts import techtree_viewer as tv
from test_techtree_viewer import _fixture


def test_adr036_pattern_eeepc_agent_path_triggers_rejection() -> None:
    """ADR-036: /etc/eeepc-agent path must never be published."""
    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": "Config lives at /etc/eeepc-agent/litellm.env"})
    assert "eeepc_agent_path" in str(exc_info.value)
    assert "index.html" in str(exc_info.value)


def test_adr036_pattern_openai_key_triggers_rejection_and_ignores_risk() -> None:
    """ADR-036: sk- tokens must trigger rejection, but risk- words must not."""
    ps.scan_pages({"about.html": "risk-based evaluation and risk-reward tradeoffs"})
    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"about.html": "sk-12345678901234567890abcdef"})
    assert "openai_secret_key" in str(exc_info.value)


def test_adr036_pattern_github_token_triggers_rejection() -> None:
    """ADR-036: ghp_, gho_, ghs_, ghu_, and github_pat_ must trigger rejection."""
    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": "ghp_1234567890123456"})
    assert "github_token" in str(exc_info.value)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": "github_pat_12345678901234567890"})
    assert "github_token" in str(exc_info.value)


def test_adr036_pattern_bearer_and_basic_auth_trigger_rejection() -> None:
    """ADR-036: Bearer and Basic authentication headers must trigger rejection."""
    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": "Authorization: Bearer mytoken1234567890abcdef"})
    assert "bearer_token" in str(exc_info.value)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": "Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ="})
    assert "basic_auth" in str(exc_info.value)


def test_adr036_pattern_aws_and_slack_tokens_trigger_rejection() -> None:
    """ADR-036: AWS access keys and Slack API tokens must trigger rejection."""
    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": "AKIAIOSFODNN7EXAMPLE"})
    assert "aws_access_key" in str(exc_info.value)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": "xoxb-1234567890-abcdef123456"})
    assert "slack_token" in str(exc_info.value)


def test_adr036_pattern_url_credentials_trigger_rejection() -> None:
    """ADR-036: URLs with embedded username and password must trigger rejection."""
    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": "https://admin:secretpass123@example.com/api"})
    assert "url_credentials" in str(exc_info.value)


def test_adr036_pattern_env_kv_and_json_secrets_trigger_rejection() -> None:
    """ADR-036: Env KV lines and JSON secret fields must trigger rejection."""
    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": "GH_TOKEN=mysecretgithubtoken123"})
    assert "env_secret_kv" in str(exc_info.value)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": '{"password": "my_super_secret_password"}'})
    assert "json_secret_field" in str(exc_info.value)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": '{"api_key": "live_apikey_abcdef123456"}'})
    assert "json_secret_field" in str(exc_info.value)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": '{"token": "session_token_xyz987654"}'})
    assert "json_secret_field" in str(exc_info.value)


def _live_sized_clean_html() -> str:
    """Representative ~8.45 MB synthetic page, matching the live scan budget scale."""
    return '<!doctype html><html><body>' + (
        '<p class="status">ordinary dashboard text and counters 123456</p>' * 130000
    ) + '</body></html>'


def test_scan_cache_key_includes_scanner_version_and_only_caches_clean_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing a scanner rule invalidates a prior clean hash; refusals are never cached."""
    from scripts.publish_scan import SecretPattern
    import re
    cache: dict[str, bool] = {}
    content = "NEW_RULE_CANARY_value"
    ps.scan_pages({"index.html": content}, clean_cache=cache)
    before = set(cache)
    assert before

    monkeypatch.setattr(
        ps, "STANDALONE_PATTERNS",
        (*ps.STANDALONE_PATTERNS, SecretPattern("new_test_rule", re.compile(r"NEW_RULE_CANARY"), "test rule")),
    )
    with pytest.raises(ps.PublicationScanError, match="new_test_rule"):
        ps.scan_pages({"index.html": content}, clean_cache=cache)
    assert set(cache) == before, "a rejected scan must not populate the cache"


def test_scan_invalid_cache_is_treated_as_empty_cache() -> None:
    """Malformed cache contents cannot cause a scan to be skipped."""
    content = "GH_TOKEN=invalid-cache-canary-123456"
    bad_cache = {"not-a-hash": "clean"}
    with pytest.raises(ps.PublicationScanError, match="env_secret_kv"):
        ps.scan_pages({"index.html": content}, clean_cache=bad_cache)
    assert bad_cache == {}, "invalid cache must be discarded before scanning"


def test_publish_state_roundtrip_preserves_clean_scan_hash_cache(tmp_path: Path) -> None:
    from scripts.techtree_autopublish import load_publish_state, save_publish_state
    cache = {"a" * 64 + ":html:" + "b" * 64: True}
    save_publish_state(tmp_path, "digest", 1.0, clean_scan_cache=cache)
    state = load_publish_state(tmp_path)
    assert state["clean_scan_cache"] == cache


def test_publish_state_corrupt_clean_scan_cache_is_discarded(tmp_path: Path) -> None:
    import json
    from scripts.techtree_autopublish import load_publish_state
    path = tmp_path / "publish_state.json"
    path.write_text(json.dumps({
        "digest": "d", "published_at": 1.0,
        "clean_scan_cache": {"garbage": True},
    }), encoding="utf-8")
    assert load_publish_state(tmp_path)["clean_scan_cache"] == {}


def test_adr036_counter_and_numeric_values_do_not_falsely_reject() -> None:
    """ADR-036: Numeric counters and placeholders must not trigger false positives."""
    clean_payload = {
        "index.html": "MAX_TOKENS=4096; max_tokens: 2048; total_tokens: 123456",
        "tokens.html": '{"total_tokens": 123456, "token_count": 42, "token": null}',
        "agent.html": 'const key = loop.confirmed_integration_ratio; passive: false; <API_KEY>; $VAR',
    }
    ps.scan_pages(clean_payload)


def test_scan_large_live_sized_fixture_meets_budget_and_clean_cache_is_fast() -> None:
    """Cold reference-size scan meets budget and clean repeat uses content/version cache."""
    import time
    content = _live_sized_clean_html()
    size = len(content.encode("utf-8"))
    assert 8_000_000 <= size <= 9_000_000
    cache: dict[str, bool] = {}

    start = time.perf_counter()
    ps.scan_pages({"lineage.html": content}, clean_cache=cache)
    cold_seconds = time.perf_counter() - start
    assert cold_seconds < 6.0, f"cold scan took {cold_seconds:.3f}s for {size} bytes"

    start = time.perf_counter()
    ps.scan_pages({"lineage.html": content}, clean_cache=cache)
    warm_seconds = time.perf_counter() - start
    assert warm_seconds < 0.25, f"cached repeat took {warm_seconds:.3f}s"


def test_json_scanning_unescapes_values_without_html_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON artifacts scan every decoded string but do not instantiate the HTML parser."""
    monkeypatch.setattr(ps, "_html_scan_variants", lambda _value: (_ for _ in ()).throw(AssertionError("HTML parser called for JSON")))
    payload = '{"excerpt":"{&quot;messages&quot;: []}"}'
    with pytest.raises(ps.PublicationScanError, match="structural_messages"):
        ps.scan_pages({"cycles-archive-1.json": payload})


def test_scan_version_changes_when_inherited_blob_decoder_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.techtree_viewer as viewer
    monkeypatch.setattr(viewer, "_INHERITED_BLOB_DECODER_VERSION", "test-decoder-v2", raising=False)
    before = ps.scanner_version(extra_version=viewer._INHERITED_BLOB_DECODER_VERSION)
    monkeypatch.setattr(viewer, "_INHERITED_BLOB_DECODER_VERSION", "test-decoder-v3", raising=False)
    after = ps.scanner_version(extra_version=viewer._INHERITED_BLOB_DECODER_VERSION)
    assert after != before


def test_inherited_blob_cache_keys_by_blob_sha_and_scanner_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """A previously clean inherited blob SHA reuses approval only for same scanner version."""
    cache: dict[str, bool] = {}
    sha = "a" * 40
    ps.scan_pages({"index.html": "safe inherited content"}, clean_cache=cache,
                  inherited_blob_shas={"index.html": sha})
    first_keys = set(cache)
    assert first_keys

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("clean blob cache should skip repeated byte scan")
    monkeypatch.setattr(ps, "scan_text", fail_scan)
    ps.scan_pages({"index.html": "same blob bytes"}, clean_cache=cache,
                  inherited_blob_shas={"index.html": sha})


def test_adr036_structural_call_markers_trigger_rejection() -> None:
    """ADR-036: Structural LLM call markers must trigger rejection."""
    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": '{"reasoning_content": "thinking..."}'})
    assert "structural_reasoning_content" in str(exc_info.value)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": '{"messages": [{"role": "user"}]}'})
    assert "structural_messages" in str(exc_info.value)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": '{"prompt": {"content": "..."}}'})
    assert "structural_prompt" in str(exc_info.value)


def test_adr036_secret_value_never_appears_in_exception_text() -> None:
    """ADR-036: Rejection exception message must never print the secret value itself."""
    secret_value = "sk-super_secret_canary_value_9988776655"
    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"agent.html": f"Authorization: {secret_value}"})
    err_msg = str(exc_info.value)
    assert secret_value not in err_msg
    assert "agent.html" in err_msg
    assert "openai_secret_key" in err_msg


def test_adr036_rendered_fixture_pages_pass_scan_cleanly() -> None:
    """ADR-036: Current public pages rendered from fixture must pass scan without error."""
    pages = tv.render_pages(_fixture(), host="eeepc", generated_at="2026-08-18 12:00:00")
    ps.scan_pages(pages)


def test_adr036_publish_to_pages_direct_call_rejects_leak() -> None:
    """ADR-036: Direct call to publish_to_pages must enforce scanner gate."""
    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": "Leaked /etc/eeepc-agent config"})
    assert "eeepc_agent_path" in str(exc_info.value)


def test_adr036_publish_to_pages_rejects_inherited_base_tree_leak(monkeypatch) -> None:
    """ADR-036: Contaminated inherited file in subdirectory (e.g. sub/leak.html) causes refusal."""
    import base64
    import json

    leaked_blob = base64.b64encode(b'{"reasoning_content": "private text"}').decode("ascii")

    def fake_gh(args, input_text=None):
        joined = " ".join(args)
        def cp(out, rc=0):
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr="")
        if "branches/gh-pages" in joined:
            return cp('{"commit":{"sha":"oldparent","commit":{"tree":{"sha":"oldtree"}}}}')
        if "git/trees/oldtree" in joined:
            return cp(json.dumps({"tree": [{"path": "tokens.html", "type": "blob", "sha": "leakblob1"}], "truncated": False}))
        if "blobs/leakblob1" in joined:
            return cp(json.dumps({"content": leaked_blob, "encoding": "base64"}))
        if "git/blobs" in joined:
            return cp("newblobsha")
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": "<html>clean page</html>"})
    assert "tokens.html" in str(exc_info.value)
    assert "structural_reasoning_content" in str(exc_info.value)


def test_adr036_autopublish_dry_run_scans_and_rejects_leak(monkeypatch, tmp_path) -> None:
    """ADR-036: techtree_autopublish --dry-run must scan and reject sensitive content."""
    import scripts.techtree_autopublish as ap

    root = tmp_path / "state"
    root.mkdir()
    state_dir = tmp_path / "lib"
    state_dir.mkdir()

    # Monkeypatch render_pages to emit a contaminated page
    monkeypatch.setattr(ap.tv, "read_local_state", lambda *a, **kw: {})
    monkeypatch.setattr(ap.tv, "render_pages", lambda *a, **kw: {"index.html": "/etc/eeepc-agent/secret.env"})

    args = ap.parse_args(["--state-root", str(root), "--state-dir", str(state_dir), "--dry-run"])
    with pytest.raises(ps.PublicationScanError) as exc_info:
        ap.run(args)
    assert "eeepc_agent_path" in str(exc_info.value)


def test_adr036_env_kv_with_quotes_triggers_rejection() -> None:
    """ADR-036: Quoted env/kv tokens like API_KEY="..." or GH_TOKEN='...' must be caught."""
    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": 'API_KEY="mysecrettoken12345"'})
    assert "env_secret_kv" in str(exc_info.value)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        ps.scan_pages({"index.html": "GH_TOKEN='mysecrettoken12345'"})
    assert "env_secret_kv" in str(exc_info.value)


def test_adr036_publish_to_pages_dry_run_scans_inherited_tree(monkeypatch) -> None:
    """ADR-036: publish_to_pages(dry_run=True) must inspect inherited tree before returning."""
    import base64
    import json

    leaked_blob = base64.b64encode(b'{"reasoning_content": "private text"}').decode("ascii")

    def fake_gh(args, input_text=None):
        joined = " ".join(args)
        def cp(out, rc=0):
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr="")
        if "branches/gh-pages" in joined:
            return cp('{"commit":{"sha":"oldparent","commit":{"tree":{"sha":"oldtree"}}}}')
        if "git/trees/oldtree" in joined:
            return cp(json.dumps({"tree": [{"path": "tokens.html", "type": "blob", "sha": "leakblob1"}], "truncated": False}))
        if "blobs/leakblob1" in joined:
            return cp(json.dumps({"content": leaked_blob, "encoding": "base64"}))
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": "<html>clean page</html>"}, dry_run=True)
    assert "tokens.html" in str(exc_info.value)
    assert "structural_reasoning_content" in str(exc_info.value)


def test_adr036_publish_to_pages_fail_closed_on_tree_api_error(monkeypatch) -> None:
    """ADR-036: Fail-closed on network/API failure when inspecting inherited tree."""
    def fake_gh(args, input_text=None):
        joined = " ".join(args)
        def cp(out, rc=0, err=""):
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr=err)
        if "branches/gh-pages" in joined:
            return cp('{"commit":{"sha":"oldparent","commit":{"tree":{"sha":"oldtree"}}}}')
        if "git/trees/oldtree" in joined:
            return cp("", rc=1, err="HTTP 502 Bad Gateway")
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": "<html>clean page</html>"})
    assert "cannot inspect inherited tree" in str(exc_info.value)


def test_adr036_publish_to_pages_fail_closed_on_blob_fetch_error(monkeypatch) -> None:
    """ADR-036: Fail-closed when an inherited blob cannot be downloaded for verification."""
    import json

    def fake_gh(args, input_text=None):
        joined = " ".join(args)
        def cp(out, rc=0, err=""):
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr=err)
        if "branches/gh-pages" in joined:
            return cp('{"commit":{"sha":"oldparent","commit":{"tree":{"sha":"oldtree"}}}}')
        if "git/trees/oldtree" in joined:
            return cp(json.dumps({"tree": [{"path": "tokens.html", "type": "blob", "sha": "blob1"}], "truncated": False}))
        if "blobs/blob1" in joined:
            return cp("", rc=1, err="HTTP 404 Blob not found")
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": "<html>clean page</html>"})
    assert "cannot fetch inherited blob" in str(exc_info.value)


def test_adr036_publish_to_pages_rejects_truncated_tree(monkeypatch) -> None:
    """ADR-036: Fail-closed if git/trees response is marked truncated."""
    import json

    def fake_gh(args, input_text=None):
        joined = " ".join(args)
        def cp(out, rc=0, err=""):
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr=err)
        if "branches/gh-pages" in joined:
            return cp('{"commit":{"sha":"oldparent","commit":{"tree":{"sha":"oldtree"}}}}')
        if "git/trees/oldtree" in joined:
            return cp(json.dumps({"tree": [], "truncated": True}))
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": "<html>clean page</html>"})
    assert "truncated" in str(exc_info.value)


def test_adr036_publish_to_pages_rejects_null_tree_object(monkeypatch) -> None:
    """ADR-036: Fail-closed if tree response has null tree object."""
    import json

    def fake_gh(args, input_text=None):
        joined = " ".join(args)
        def cp(out, rc=0, err=""):
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr=err)
        if "branches/gh-pages" in joined:
            return cp('{"commit":{"sha":"oldparent","commit":{"tree":{"sha":"oldtree"}}}}')
        if "git/trees/oldtree" in joined:
            return cp(json.dumps({"tree": None, "truncated": False}))
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": "<html>clean page</html>"})
    assert "null or missing" in str(exc_info.value)


def test_adr036_publish_to_pages_unparseable_branch_probe_refuses_publish(monkeypatch) -> None:
    """ADR-036: Fail-closed if branch probe returns unparseable JSON, no tree creation."""
    tree_created = []

    def fake_gh(args, input_text=None):
        joined = " ".join(args)
        def cp(out, rc=0, err=""):
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr=err)
        if "branches/gh-pages" in joined:
            return cp("invalid-non-json-output")
        if "git/trees" in joined and ("-X" in args or input_text is not None):
            tree_created.append(args)
            return cp("newtree")
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError):
        tv.publish_to_pages({"index.html": "<html>clean page</html>"})
    assert not tree_created


def test_adr036_publish_to_pages_scans_moved_head_on_attempt_one(monkeypatch) -> None:
    """ADR-036: Base tree read in the loop on attempt 1 must be scanned unconditionally."""
    import base64
    import json

    leaked_blob = base64.b64encode(b'{"reasoning_content": "private text"}').decode("ascii")
    commits_made = []
    calls_branch = 0

    def fake_gh(args, input_text=None):
        nonlocal calls_branch
        joined = " ".join(args)
        def cp(out, rc=0, err=""):
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr=err)
        if "branches/gh-pages" in joined:
            calls_branch += 1
            if calls_branch == 1:
                # Probe sees clean tree
                return cp('{"commit":{"sha":"probe-parent","commit":{"tree":{"sha":"clean-tree"}}}}')
            # Attempt 1 sees moved branch tip with a leak
            return cp('{"commit":{"sha":"moved-parent","commit":{"tree":{"sha":"moved-tree"}}}}')
        if "git/trees/clean-tree" in joined:
            return cp(json.dumps({"tree": [], "truncated": False}))
        if "git/trees/moved-tree" in joined:
            return cp(json.dumps({"tree": [{"path": "sub/leak.html", "type": "blob", "sha": "leak2"}], "truncated": False}))
        if "blobs/leak2" in joined:
            return cp(json.dumps({"content": leaked_blob, "encoding": "base64"}))
        if "git/commits" in joined:
            commits_made.append(args)
            return cp("comsha")
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": "<html>clean page</html>"})
    assert "sub/leak.html" in str(exc_info.value)
    assert not commits_made


def test_adr036_html_entity_encoded_call_markers_and_secrets_trigger_rejection() -> None:
    """ADR-036 rule 3: HTML entity encoded call markers and json keys must be rejected."""
    encoded_messages = "<div>{&quot;messages&quot;: [{&quot;role&quot;: &quot;user&quot;}]}</div>"
    encoded_reasoning = "<pre>{&quot;reasoning_content&quot;: &quot;thinking&quot;}</pre>"
    encoded_password = "<code>{&quot;password&quot;: &quot;secret12345&quot;}</code>"

    with pytest.raises(ps.PublicationScanError, match="structural_messages"):
        ps.scan_pages({"index.html": encoded_messages})

    with pytest.raises(ps.PublicationScanError, match="structural_reasoning_content"):
        ps.scan_pages({"index.html": encoded_reasoning})

    with pytest.raises(ps.PublicationScanError, match="json_secret_field"):
        ps.scan_pages({"index.html": encoded_password})


def test_adr036_quoted_values_in_secret_assignments_trigger_rejection() -> None:
    """ADR-036 rule 3: Double-quoted and single-quoted values in secret assignments must be rejected."""
    quoted_double = 'DB_PASSWORD="actual-secret-123"'
    quoted_single = "API_KEY='actual-secret-123'"
    quoted_with_spaces = 'ADMIN_TOKEN="actual secret with space 123"'

    with pytest.raises(ps.PublicationScanError, match="env_secret_kv"):
        ps.scan_pages({"index.html": quoted_double})

    with pytest.raises(ps.PublicationScanError, match="env_secret_kv"):
        ps.scan_pages({"index.html": quoted_single})

    with pytest.raises(ps.PublicationScanError, match="env_secret_kv"):
        ps.scan_pages({"index.html": quoted_with_spaces})


def test_adr036_punctuation_bearing_credentials_trigger_rejection() -> None:
    """ADR-036 rule 3: Credentials with base64 padding or password punctuation must not be exempted."""
    base64_padded = "API_KEY=abcde==fghij"
    semicolon_password = '{"password":"Tr0ub4dor;correct"}'

    with pytest.raises(ps.PublicationScanError, match="env_secret_kv"):
        ps.scan_pages({"index.html": base64_padded})

    with pytest.raises(ps.PublicationScanError, match="json_secret_field"):
        ps.scan_pages({"index.html": semicolon_password})


def test_adr036_lowercase_bearer_token_triggers_rejection() -> None:
    """ADR-036 rule 3: Bearer authentication schemes must be matched case-insensitively."""
    lowercase_bearer = "authorization: bearer abcdefghijklmnop"

    with pytest.raises(ps.PublicationScanError, match="bearer_token"):
        ps.scan_pages({"index.html": lowercase_bearer})


def test_adr036_dry_run_never_creates_gh_pages_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036 rule 3: dry_run must never create gh-pages or call any mutating POST endpoints."""
    post_calls = []

    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        if "-X" in args and "POST" in args:
            post_calls.append(args)
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout="{}", stderr="")
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            # Branch does not exist
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=1, stdout="", stderr="Branch not found")
        return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    rc, fp = tv.publish_to_pages({"index.html": "<html>clean content</html>"}, dry_run=True)
    assert rc == 0
    assert not post_calls, f"dry_run must not make POST calls, but made: {post_calls}"


def test_adr036_bootstrap_gh_pages_from_clean_tree_never_inherits_master(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036 rule 3: When gh-pages does not exist, bootstrap from a clean tree, never inheriting master."""
    calls = []
    tree_payloads = []
    commit_payloads = []

    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        calls.append(joined)
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=1, stdout="", stderr="Not found")
        if "git/blobs" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout='{"sha":"blob1"}', stderr="")
        if "git/trees" in joined:
            payload = kwargs.get("input_text") or kwargs.get("input")
            if payload:
                tree_payloads.append(payload)
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout='{"sha":"tree1"}', stderr="")
        if "git/commits" in joined:
            payload = kwargs.get("input_text") or kwargs.get("input")
            if payload:
                commit_payloads.append(payload)
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout='{"sha":"com1"}', stderr="")
        if "git/refs" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout="{}", stderr="")
        return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    rc, fp = tv.publish_to_pages({"index.html": "<html>clean content</html>"})
    assert rc == 0
    assert not any("heads/master" in c or "branches/master" in c for c in calls)
    assert tree_payloads
    import json
    tree_data = json.loads(tree_payloads[0])
    assert "base_tree" not in tree_data or tree_data["base_tree"] is None
    assert commit_payloads
    com_data = json.loads(commit_payloads[0])
    assert com_data.get("parents") == []


def test_adr036_remote_blob_scanned_when_local_page_is_fingerprint_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036 rule 3: Unchanged pages skipped by fingerprint must have remote blobs scanned."""
    import base64
    import json
    clean_index = "<html>clean index</html>"
    leaked_remote_index = base64.b64encode(b"<div>sk-proj-supersecretkey1234567890abcdef</div>").decode("ascii")

    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        cp = lambda out="", rc=0: subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr="")
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return cp('{"commit":{"sha":"parent1","commit":{"tree":{"sha":"base-tree-1"}}}}')
        if "git/trees/base-tree-1" in joined:
            return cp(json.dumps({"tree": [
                {"path": "index.html", "type": "blob", "sha": "remote-index-sha"},
            ], "truncated": False}))
        if "blobs/remote-index-sha" in joined:
            return cp(json.dumps({"content": leaked_remote_index, "encoding": "base64"}))
        if "git/blobs" in joined:
            return cp('{"sha":"new-cycles-sha"}')
        if "git/trees" in joined:
            return cp('{"sha":"new-tree-sha"}')
        if "git/commits" in joined:
            return cp('{"sha":"new-commit-sha"}')
        if "git/refs" in joined:
            return cp("{}")
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    prev_fps = {"index.html": tv._page_fingerprint(clean_index)}
    pages = {
        "index.html": clean_index,
        "cycles.html": "<html>new cycles</html>",
    }
    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages(pages, previous_fingerprints=prev_fps)
    assert "index.html" in str(exc_info.value)
    assert "openai_secret_key" in str(exc_info.value)


def test_inherited_clean_blob_cache_skips_remote_blob_download(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    cache = {
        ps.clean_cache_key(
            "a" * 40, extra_version=tv._INHERITED_BLOB_DECODER_VERSION, mode="html"
        ): True
    }
    calls = []

    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(" ".join(args))
        return subprocess.CompletedProcess(
            args=["gh"] + list(args), returncode=0,
            stdout=json.dumps({"tree": [{"path": "tokens.html", "type": "blob", "sha": "a" * 40}], "truncated": False}),
            stderr="",
        )

    monkeypatch.setattr(tv, "_gh", fake_gh)
    tv._inspect_and_scan_inherited_tree("tree-sha", set(), scan_cache=cache)
    assert len(calls) == 1 and "git/trees/tree-sha" in calls[0]


def test_adr036_inherited_tree_unlisted_path_rejected_by_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036 rule 3: Any inherited path not in allowlist must be rejected, even without secret markers."""
    import json

    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        cp = lambda out="", rc=0: subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr="")
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return cp('{"commit":{"sha":"parent1","commit":{"tree":{"sha":"base-tree-1"}}}}')
        if "git/trees/base-tree-1" in joined:
            return cp(json.dumps({"tree": [
                {"path": "src/leak.py", "type": "blob", "sha": "innocent-sha"},
            ], "truncated": False}))
        if "blobs/innocent-sha" in joined:
            return cp(json.dumps({"content": "print('hello world')", "encoding": "utf-8"}))
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError, match="unlisted.*allowlist.*src/leak.py"):
        tv.publish_to_pages({"index.html": "<html>clean index</html>"})


def test_adr036_rejects_entities_still_changing_after_round_limit() -> None:
    """ADR-036: If a bounded unescape still changes, do not scan a partially decoded value as clean."""
    import html
    deeply_nested = '"messages": []'
    for _ in range(6):
        deeply_nested = html.escape(deeply_nested, quote=True)
    # Verify the test input itself has six real escaping layers and is not merely
    # a run of adjacent entities that stabilizes early.
    decoded = deeply_nested
    for _ in range(5):
        decoded = html.unescape(decoded)
    assert html.unescape(decoded) != decoded
    with pytest.raises(ps.PublicationScanError, match="entity.*limit|unescape.*limit|decode.*limit"):
        ps.scan_pages({"index.html": deeply_nested})


def test_adr036_tag_stripped_secrets_and_markers_trigger_rejection() -> None:
    """ADR-036 rule 3: Secrets and markers split across HTML tags must be detected."""
    split_token = "<code>ghp_<span>abcdefghijklmnop123456</span></code>"
    split_marker = "<div>{&quot;<span>messages</span>&quot;: []}</div>"

    with pytest.raises(ps.PublicationScanError, match="github_token"):
        ps.scan_pages({"index.html": split_token})

    with pytest.raises(ps.PublicationScanError, match="structural_messages"):
        ps.scan_pages({"index.html": split_marker})


def test_adr036_account_password_not_exempted_by_count_substring() -> None:
    """ADR-036 rule 3: ACCOUNT_PASSWORD must not be exempted merely because it contains 'count'."""
    account_pass = "ACCOUNT_PASSWORD=abcdefghijklmnop"
    account_key = "ACCOUNT_KEY=abcdefghijklmnop"

    with pytest.raises(ps.PublicationScanError, match="env_secret_kv"):
        ps.scan_pages({"index.html": account_pass})

    with pytest.raises(ps.PublicationScanError, match="env_secret_kv"):
        ps.scan_pages({"index.html": account_key})


def test_adr036_inherited_tree_refuses_malformed_schemas_and_scans_compressed_blobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036 rule 3: Malformed tree schemas ({}, {"tree":{}}) and compressed blobs must be refused/scanned."""
    import base64
    import gzip
    import json

    # Test 1: tree is a dict {"tree": {}} instead of a list
    def fake_gh_dict_tree(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        cp = lambda out="", rc=0: subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr="")
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return cp('{"commit":{"sha":"parent1","commit":{"tree":{"sha":"tree1"}}}}')
        if "git/trees/tree1" in joined:
            return cp('{"tree": {}}')
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh_dict_tree)
    with pytest.raises(ps.PublicationScanError, match="schema|list|invalid"):
        tv.publish_to_pages({"index.html": "<html>clean</html>"})

    # Test 2: blob response is missing "content" key ({})
    def fake_gh_missing_content(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        cp = lambda out="", rc=0: subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr="")
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return cp('{"commit":{"sha":"parent1","commit":{"tree":{"sha":"tree1"}}}}')
        if "git/trees/tree1" in joined:
            return cp('{"tree": [{"path": "tokens.html", "type": "blob", "sha": "blob1"}]}')
        if "blobs/blob1" in joined:
            return cp('{}')
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh_missing_content)
    with pytest.raises(ps.PublicationScanError, match="missing content|malformed"):
        tv.publish_to_pages({"index.html": "<html>clean</html>"})

    # Test 3: blob contains gzip compressed sensitive data
    gz_secret = gzip.compress(b'{"reasoning_content": "compressed secret"}')
    gz_b64 = base64.b64encode(gz_secret).decode("ascii")

    def fake_gh_gzip_blob(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        cp = lambda out="", rc=0: subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr="")
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return cp('{"commit":{"sha":"parent1","commit":{"tree":{"sha":"tree1"}}}}')
        if "git/trees/tree1" in joined:
            return cp('{"tree": [{"path": "tokens.html", "type": "blob", "sha": "blob_gz"}]}')
        if "blobs/blob_gz" in joined:
            return cp(json.dumps({"content": gz_b64, "encoding": "base64"}))
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh_gzip_blob)
    with pytest.raises(ps.PublicationScanError):
        tv.publish_to_pages({"index.html": "<html>clean</html>"})


def test_adr036_single_quoted_keys_and_values_trigger_rejection() -> None:
    """ADR-036 rule 3: Single-quoted keys and values in JSON-like structures must be detected."""
    single_json_1 = "{'api_key': 'secret123456'}"
    single_json_2 = "'auth_token': 'mysecretvalue'"
    single_json_3 = "{'password': 'secret123456'}"

    with pytest.raises(ps.PublicationScanError, match="json_secret_field"):
        ps.scan_pages({"index.html": single_json_1})

    with pytest.raises(ps.PublicationScanError, match="json_secret_field"):
        ps.scan_pages({"index.html": single_json_2})

    with pytest.raises(ps.PublicationScanError, match="json_secret_field"):
        ps.scan_pages({"index.html": single_json_3})


def test_adr036_encoded_html_markup_is_parsed_before_scanning() -> None:
    """HTML-escaped tag delimiters must be unescaped before parser tokenization."""
    encoded_markup = "ghp_&lt;span&gt;abcdefghijklmnop123456&lt;/span&gt;"
    with pytest.raises(ps.PublicationScanError, match="github_token"):
        ps.scan_pages({"index.html": encoded_markup})


def test_adr036_html_parser_detects_tokens_split_through_tags_and_attributes() -> None:
    """ADR-036: Scanner inspects parsed text and attributes without merging the streams."""
    split_github_token = '<div>ghp_<span class="red box">abcdefghijklmnop123456</span></div>'
    split_assignment = '<p>API_KEY=abc<span></span>def12345</p>'
    token_in_attribute = '<span data-token="ghp_abcdefghijklmnop123456"></span>'

    with pytest.raises(ps.PublicationScanError, match="github_token"):
        ps.scan_pages({"index.html": split_github_token})

    with pytest.raises(ps.PublicationScanError, match="env_secret_kv"):
        ps.scan_pages({"index.html": split_assignment})

    with pytest.raises(ps.PublicationScanError, match="github_token"):
        ps.scan_pages({"index.html": token_in_attribute})


def test_adr036_raw_text_element_markup_is_scanned_as_html_fragment() -> None:
    """Tag-split credentials in raw-text elements are parsed with text and attributes separate."""
    for element in ("script", "style", "textarea", "title"):
        page = f"<{element}>document.body.innerHTML='ghp_<span>abcdefghijklmnop123456</span>'</{element}>"
        with pytest.raises(ps.PublicationScanError, match="github_token"):
            ps.scan_pages({"index.html": page})

    attributed_fragment = '<script>document.body.innerHTML=\'ghp_<span class="red box">abcdefghijklmnop123456</span>\'</script>'
    with pytest.raises(ps.PublicationScanError, match="github_token"):
        ps.scan_pages({"index.html": attributed_fragment})


def test_adr036_single_quoted_structural_markers_trigger_rejection() -> None:
    """ADR-036: Single-quoted call structures are still call markers and must be rejected."""
    with pytest.raises(ps.PublicationScanError, match="structural_messages"):
        ps.scan_pages({"index.html": "{'messages': [{'role': 'user'}]}"})
    with pytest.raises(ps.PublicationScanError, match="structural_reasoning_content"):
        ps.scan_pages({"index.html": "{'reasoning_content': 'private'}"})


def test_adr036_iterative_html_unescape_double_encoded_entities() -> None:
    """ADR-036 rule 3: Double-encoded HTML entities must be decoded until stabilization."""
    double_encoded_msg = "<div>{&amp;quot;messages&amp;quot;: []}</div>"
    double_encoded_pass = "<code>{&amp;#34;password&amp;#34;: &amp;#34;secret123456&amp;#34;}</code>"

    with pytest.raises(ps.PublicationScanError, match="structural_messages"):
        ps.scan_pages({"index.html": double_encoded_msg})

    with pytest.raises(ps.PublicationScanError, match="json_secret_field"):
        ps.scan_pages({"index.html": double_encoded_pass})


def test_adr036_multiline_quoted_secrets() -> None:
    """ADR-036 rule 3: Multiline secrets inside quotes must be detected."""
    multiline_env = 'PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\nMIIEogIBAAKCAQEA0Y\n-----END RSA PRIVATE KEY-----"'

    with pytest.raises(ps.PublicationScanError):
        ps.scan_pages({"index.html": multiline_env})


def test_adr036_branch_probe_transient_error_refuses_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036 rule 3: Transient probe errors (500, 401, timeout) must refuse fail-closed, not bootstrap."""
    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return subprocess.CompletedProcess(
                args=["gh"] + list(args), returncode=1, stdout="", stderr="HTTP 500 Internal Server Error"
            )
        return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError, match="500|cannot probe|probe error|failed"):
        tv.publish_to_pages({"index.html": "<html>clean</html>"}, dry_run=True)

    rc, fp = tv.publish_to_pages({"index.html": "<html>clean</html>"})
    assert rc == 1
    assert fp == {}


def test_adr036_nothing_to_publish_scans_inherited_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036 rule 3: Even when 0 pages changed ('nothing to publish'), inherited tree must be scanned."""
    import base64
    import json
    clean_index = "<html>clean index</html>"
    leaked_remote_index = base64.b64encode(b"<div>sk-proj-supersecretkey1234567890abcdef</div>").decode("ascii")

    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        cp = lambda out="", rc=0: subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr="")
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return cp('{"commit":{"sha":"parent1","commit":{"tree":{"sha":"base-tree-1"}}}}')
        if "git/trees/base-tree-1" in joined:
            return cp(json.dumps({"tree": [
                {"path": "index.html", "type": "blob", "sha": "remote-index-sha"},
            ], "truncated": False}))
        if "blobs/remote-index-sha" in joined:
            return cp(json.dumps({"content": leaked_remote_index, "encoding": "base64"}))
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    prev_fps = {"index.html": tv._page_fingerprint(clean_index)}
    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": clean_index}, previous_fingerprints=prev_fps)
    assert "openai_secret_key" in str(exc_info.value)


def test_adr036_bootstrap_clean_branch_enables_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036 rule 3: Bootstrap clean branch must enable GitHub Pages."""
    pages_calls = []

    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=1, stdout="", stderr="404 Not Found")
        if "git/blobs" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout='{"sha":"blob1"}', stderr="")
        if "git/trees" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout='{"sha":"tree1"}', stderr="")
        if "git/commits" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout='{"sha":"com1"}', stderr="")
        if "git/refs" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout="{}", stderr="")
        if "pages" in joined:
            pages_calls.append(args)
            if "-X" in args:
                return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout="{}", stderr="")
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=1, stdout="", stderr="404")
        return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    rc, fp = tv.publish_to_pages({"index.html": "<html>clean</html>"})
    assert rc == 0
    assert any("-X" in call and "POST" in call for call in pages_calls)


def test_adr036_regular_publish_pages_enable_failure_returns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036: Pages activation failure after a regular ref update must fail the publish."""
    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        cp = lambda out="", rc=0, err="": subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr=err)
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return cp('{"commit":{"sha":"parent1","commit":{"tree":{"sha":"tree1"}}}}')
        if "git/trees/tree1" in joined:
            return cp('{"tree": [], "truncated": false}')
        if "git/blobs" in joined:
            return cp('{"sha":"blob1"}')
        if "git/trees" in joined and "-X" in args:
            return cp("tree2")
        if "git/commits" in joined:
            return cp("commit2")
        if "git/refs/heads/gh-pages" in joined and "PATCH" in args:
            return cp("{}")
        if "pages" in joined and "-X" in args:
            return cp("", rc=1, err="HTTP 403 Forbidden")
        if "pages" in joined:
            return cp("", rc=1, err="404 Not Found")
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)
    rc, fingerprints = tv.publish_to_pages({"index.html": "<html>updated</html>"})
    assert rc != 0
    assert fingerprints == {}


def test_adr036_bootstrap_pages_enable_failure_returns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036: A failed Pages activation must not report bootstrap publication success."""
    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=1, stdout="", stderr="404 Not Found")
        if "git/blobs" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout='{"sha":"blob1"}', stderr="")
        if "git/trees" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout='{"sha":"tree1"}', stderr="")
        if "git/commits" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout='{"sha":"com1"}', stderr="")
        if "git/refs" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout="{}", stderr="")
        if "pages" in joined and "-X" in args:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=1, stdout="", stderr="HTTP 403 Forbidden")
        if "pages" in joined:
            return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=1, stdout="", stderr="404 Not Found")
        return subprocess.CompletedProcess(args=["gh"] + list(args), returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(tv, "_gh", fake_gh)
    rc, fingerprints = tv.publish_to_pages({"index.html": "<html>clean</html>"})
    assert rc != 0
    assert fingerprints == {}


def test_adr036_gzip_bomb_exceeds_inherited_blob_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036: Refuse gzip blob exceeding 20 MiB decompressed scan limit."""
    import base64
    import gzip
    import json
    compressed_bomb = base64.b64encode(gzip.compress(b"0" * (50 * 1024 * 1024))).decode("ascii")

    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        cp = lambda out="", rc=0: subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr="")
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return cp('{"commit":{"sha":"parent1","commit":{"tree":{"sha":"tree1"}}}}')
        if "git/trees/tree1" in joined:
            return cp('{"tree": [{"path": "tokens.html", "type": "blob", "sha": "blob1"}]}')
        if "blobs/blob1" in joined:
            return cp(json.dumps({"content": compressed_bomb, "encoding": "base64"}))
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)
    with pytest.raises(ps.PublicationScanError, match="limit|exceed|large"):
        tv.publish_to_pages({"index.html": "<html>clean</html>"})


def test_adr036_scans_all_concatenated_gzip_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036: A secret in a later concatenated gzip member must be scanned."""
    import base64
    import gzip
    import json
    compressed_members = gzip.compress(b"safe first member ") + gzip.compress(
        b"sk-proj-supersecretkey1234567890abcdef"
    )
    encoded = base64.b64encode(compressed_members).decode("ascii")

    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        cp = lambda out="", rc=0: subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr="")
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return cp('{"commit":{"sha":"parent1","commit":{"tree":{"sha":"tree1"}}}}')
        if "git/trees/tree1" in joined:
            return cp('{"tree": [{"path": "tokens.html", "type": "blob", "sha": "blob1"}]}')
        if "blobs/blob1" in joined:
            return cp(json.dumps({"content": encoded, "encoding": "base64"}))
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)
    with pytest.raises(ps.PublicationScanError, match="openai_secret_key"):
        tv.publish_to_pages({"index.html": "<html>clean</html>"})


def test_adr036_detects_secret_split_at_gzip_member_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-036: Concatenate gzip members byte-for-byte so split secret assignments remain detectable."""
    import base64
    import gzip
    import json
    encoded = base64.b64encode(
        gzip.compress(b"API_KEY=abc") + gzip.compress(b"def12345")
    ).decode("ascii")

    def fake_gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        cp = lambda out="", rc=0: subprocess.CompletedProcess(args=["gh"] + list(args), returncode=rc, stdout=out, stderr="")
        if f"branches/{tv.PUBLISH_BRANCH}" in joined:
            return cp('{"commit":{"sha":"parent1","commit":{"tree":{"sha":"tree1"}}}}')
        if "git/trees/tree1" in joined:
            return cp('{"tree": [{"path": "tokens.html", "type": "blob", "sha": "blob1"}]}')
        if "blobs/blob1" in joined:
            return cp(json.dumps({"content": encoded, "encoding": "base64"}))
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)
    with pytest.raises(ps.PublicationScanError, match="env_secret_kv"):
        tv.publish_to_pages({"index.html": "<html>clean</html>"})
