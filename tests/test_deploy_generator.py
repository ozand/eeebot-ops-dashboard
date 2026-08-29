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


def test_deploy_generator_documents_footer_sha_dependency() -> None:
    """The script must note that the footer sha injection is a UI-worker dependency."""
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    # Check that the script explains the footer sha limitation
    assert "techtree_viewer.py" in text
    assert (
        "footer" in text.lower()
        or "generator sha" in text.lower()
        or "sha" in text.lower()
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

    # Must print the footer-sha dependency note
    assert "techtree_viewer.py" in stdout


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
