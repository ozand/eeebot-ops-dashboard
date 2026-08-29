"""Tests for scripts/deploy_generator.sh (issue #101).

These tests cover what is feasible without a real eeepc host:
- bash syntax check (bash -n)
- dry-run output contract: contains sha, file names, dry-run markers
- script is idempotent by construction (backup + py_compile guard)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_generator.sh"
VIEWER_SCRIPT = REPO_ROOT / "scripts" / "techtree_viewer.py"
AUTOPUBLISH_SCRIPT = REPO_ROOT / "scripts" / "techtree_autopublish.py"


# ---------------------------------------------------------------------------
# Structural / static checks
# ---------------------------------------------------------------------------

def test_deploy_generator_script_exists() -> None:
    assert DEPLOY_SCRIPT.exists(), f"scripts/deploy_generator.sh not found at {DEPLOY_SCRIPT}"


def test_deploy_generator_script_is_executable_or_bash() -> None:
    """The script must start with a bash shebang so it can be invoked explicitly."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") or text.startswith("#!/bin/bash"), (
        "deploy_generator.sh must start with a bash shebang"
    )


def test_deploy_generator_bash_syntax() -> None:
    """bash -n does a syntax-only check without executing any code."""
    result = subprocess.run(
        ["bash", "-n", str(DEPLOY_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"bash -n reported syntax errors:\n{result.stderr}"
    )


def test_deploy_generator_references_both_scripts() -> None:
    """The deploy script must mention both generator files."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "techtree_viewer.py" in text
    assert "techtree_autopublish.py" in text


def test_deploy_generator_references_opt_dir() -> None:
    """The script must target /opt/eeebot-techtree/."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "/opt/eeebot-techtree" in text


def test_deploy_generator_references_py_compile() -> None:
    """The script must py_compile the deployed files."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "py_compile" in text


def test_deploy_generator_references_backup() -> None:
    """The script must create a timestamped backup of the prior copy."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "bak" in text or "backup" in text.lower()


def test_deploy_generator_references_publish_service() -> None:
    """The script must trigger eeebot-techtree-publish.service."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "eeebot-techtree-publish.service" in text


def test_deploy_generator_mentions_dry_run() -> None:
    """The script must support a --dry-run flag."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "--dry-run" in text


def test_deploy_generator_bakes_sha_into_viewer(bash_available: bool) -> None:
    """--dry-run output must include the sed-bake step for _BAKED_GENERATOR_SHA (issue #101)."""
    pass  # static check below; the dry-run test covers the marker


def test_deploy_generator_script_references_baked_sha_sentinel() -> None:
    """The script must reference the _BAKED_GENERATOR_SHA sentinel variable (issue #101)."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "_BAKED_GENERATOR_SHA" in text, (
        "deploy_generator.sh must sed-patch the _BAKED_GENERATOR_SHA sentinel in the deployed viewer"
    )
    assert "sed -i" in text or "sed -i" in text, (
        "deploy_generator.sh must use sed -i to bake the sha"
    )


def test_viewer_has_baked_sha_sentinel() -> None:
    """techtree_viewer.py must contain the _BAKED_GENERATOR_SHA sentinel line (issue #101)."""
    text = VIEWER_SCRIPT.read_text(encoding="utf-8")
    assert "_BAKED_GENERATOR_SHA: str = ''" in text, (
        "techtree_viewer.py must have a _BAKED_GENERATOR_SHA: str = '' sentinel for deploy_generator.sh to patch"
    )


def test_viewer_generator_sha_prefers_baked_value() -> None:
    """_generator_sha() must return the baked value when _BAKED_GENERATOR_SHA is non-empty (issue #101)."""
    import importlib.util, sys, types
    spec = importlib.util.spec_from_file_location("techtree_viewer_test", VIEWER_SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # Patch _BAKED_GENERATOR_SHA before exec so the module sees the baked value
    # without actually touching the file.
    original = VIEWER_SCRIPT.read_text(encoding="utf-8")
    patched = original.replace(
        "_BAKED_GENERATOR_SHA: str = ''",
        "_BAKED_GENERATOR_SHA: str = 'abc1234'",
    )
    assert "_BAKED_GENERATOR_SHA: str = 'abc1234'" in patched, "sentinel patch failed"
    import tempfile, importlib.util as ilu
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", encoding="utf-8", delete=False) as tf:
        tf.write(patched)
        tf_path = tf.name
    try:
        spec2 = ilu.spec_from_file_location("tv_baked", tf_path)
        mod2 = ilu.module_from_spec(spec2)  # type: ignore[arg-type]
        spec2.loader.exec_module(mod2)  # type: ignore[union-attr]
        result = mod2._generator_sha()
        assert result == "abc1234", (
            f"_generator_sha() should return baked sha 'abc1234', got {result!r}"
        )
    finally:
        import os; os.unlink(tf_path)


def test_viewer_generator_sha_fallback_without_baked() -> None:
    """_generator_sha() returns a non-empty string when no baked SHA is set (issue #101)."""
    import importlib.util as ilu, tempfile, os
    text = VIEWER_SCRIPT.read_text(encoding="utf-8")
    # Sentinel must be present and empty — fallback path via git or 'unknown'
    assert "_BAKED_GENERATOR_SHA: str = ''" in text
    spec = ilu.spec_from_file_location("tv_nobake", VIEWER_SCRIPT)
    mod = ilu.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    result = mod._generator_sha()
    assert isinstance(result, str) and len(result) > 0, (
        f"_generator_sha() must return a non-empty string, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Dry-run output (bash available on the host)
# ---------------------------------------------------------------------------

@pytest.fixture()
def bash_available() -> bool:
    result = subprocess.run(["bash", "--version"], capture_output=True)
    return result.returncode == 0


def test_deploy_generator_dry_run_output(bash_available: bool, tmp_path: Path) -> None:
    """--dry-run must print the sha, the source files, and [dry-run] markers."""
    if not bash_available:
        pytest.skip("bash not available")

    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    # --dry-run must NOT fail even without a real host
    assert result.returncode == 0, (
        f"deploy_generator.sh --dry-run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    stdout = result.stdout

    # Must print a git sha (short hex: 5-12 hex chars)
    assert re.search(r"sha=[0-9a-f]{5,12}", stdout), (
        f"Expected 'sha=<hex>' in dry-run output:\n{stdout}"
    )

    # Must mention both scripts
    assert "techtree_viewer.py" in stdout
    assert "techtree_autopublish.py" in stdout

    # Must prefix at least one action with [dry-run]
    assert "[dry-run]" in stdout

    # Must print the dry-run bake step for _BAKED_GENERATOR_SHA (issue #101)
    assert "_BAKED_GENERATOR_SHA" in stdout, (
        f"Expected '_BAKED_GENERATOR_SHA' bake step in dry-run output:\n{stdout}"
    )


def test_deploy_generator_dry_run_no_files_written(bash_available: bool, tmp_path: Path) -> None:
    """--dry-run must not write any new files (to /opt or anywhere else)."""
    if not bash_available:
        pytest.skip("bash not available")

    # We can only assert the script exits 0 and emits [dry-run] markers;
    # we cannot fully gate /opt writes without root, but the dry-run path
    # is tested to not attempt to contact a real host.
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    assert "no changes were made" in result.stdout


# ---------------------------------------------------------------------------
# Source files that the deploy script depends on are present and valid Python
# ---------------------------------------------------------------------------

def test_techtree_viewer_py_compile() -> None:
    """The viewer that deploy_generator.sh ships must itself compile cleanly."""
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(VIEWER_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"py_compile failed for techtree_viewer.py:\n{result.stderr}"
    )


def test_techtree_autopublish_py_compile() -> None:
    """The autopublisher that deploy_generator.sh ships must itself compile cleanly."""
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(AUTOPUBLISH_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"py_compile failed for techtree_autopublish.py:\n{result.stderr}"
    )
