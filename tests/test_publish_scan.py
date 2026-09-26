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


def test_adr036_counter_and_numeric_values_do_not_falsely_reject() -> None:
    """ADR-036: Numeric counters and placeholders must not trigger false positives."""
    clean_payload = {
        "index.html": "MAX_TOKENS=4096; max_tokens: 2048; total_tokens: 123456",
        "tokens.html": '{"total_tokens": 123456, "token_count": 42, "token": null}',
        "agent.html": 'const key = loop.confirmed_integration_ratio; passive: false; <API_KEY>; $VAR',
    }
    ps.scan_pages(clean_payload)


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
            return cp(json.dumps({"tree": [{"path": "sub/leak.html", "type": "blob", "sha": "leakblob1"}], "truncated": False}))
        if "blobs/leakblob1" in joined:
            return cp(json.dumps({"content": leaked_blob, "encoding": "base64"}))
        if "git/blobs" in joined:
            return cp("newblobsha")
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": "<html>clean page</html>"})
    assert "sub/leak.html" in str(exc_info.value)
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
            return cp(json.dumps({"tree": [{"path": "sub/leak.html", "type": "blob", "sha": "leakblob1"}], "truncated": False}))
        if "blobs/leakblob1" in joined:
            return cp(json.dumps({"content": leaked_blob, "encoding": "base64"}))
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": "<html>clean page</html>"}, dry_run=True)
    assert "sub/leak.html" in str(exc_info.value)
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
            return cp(json.dumps({"tree": [{"path": "file.txt", "type": "blob", "sha": "blob1"}], "truncated": False}))
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


def test_adr036_publish_to_pages_bootstrap_scans_master_tree(monkeypatch) -> None:
    """ADR-036: Bootstrap path must scan master's inherited tree before committing."""
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
                return cp("", rc=1, err="branch not found")
            return cp('{"commit":{"sha":"master-head-sha","commit":{"tree":{"sha":"master-tree"}}}}')
        if "ref/heads/master" in joined:
            return cp("master-head-sha")
        if "refs/heads/gh-pages" in joined and "-X" in args:
            return cp("")
        if "git/trees/master-tree" in joined:
            return cp(json.dumps({"tree": [{"path": "src/leak.py", "type": "blob", "sha": "leak1"}], "truncated": False}))
        if "blobs/leak1" in joined:
            return cp(json.dumps({"content": leaked_blob, "encoding": "base64"}))
        if "git/commits" in joined:
            commits_made.append(args)
            return cp("comsha")
        return cp("{}")

    monkeypatch.setattr(tv, "_gh", fake_gh)

    with pytest.raises(ps.PublicationScanError) as exc_info:
        tv.publish_to_pages({"index.html": "<html>clean page</html>"})
    assert "src/leak.py" in str(exc_info.value)
    assert not commits_made


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
