"""Guard against tests that only pass on their author's machine.

Flags machine-local absolute paths (like /home/..., /Users/..., C:\\..., T:/...)
when passed to filesystem constructor/open calls.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# A Windows drive-letter path (T:/..., C:\...) or a POSIX path under a user's home.
_MACHINE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|/(?:home|Users)/)")

# Callables whose string arguments are filesystem locations.
_PATH_CALLS = {"Path", "PurePath", "open"}
_PATH_MODULES = {"shutil", "os", "io"}


def _is_path_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _PATH_CALLS
    if isinstance(func, ast.Attribute):
        value = func.value
        if isinstance(value, ast.Name) and value.id in _PATH_MODULES:
            return True
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            return value.value.id in _PATH_MODULES
    return False


def _offenders() -> list[str]:
    found = []
    this_file = Path(__file__).resolve()
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        if path.resolve() == this_file:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_path_call(node):
                continue
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                    continue
                if _MACHINE_PATH.match(arg.value):
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    found.append(f"{rel}:{arg.lineno}: {arg.value!r}")
    return found


def test_no_test_opens_a_machine_local_absolute_path() -> None:
    offenders = _offenders()
    assert not offenders, (
        "tests must reach files through relative paths or repo root, "
        "not an absolute path that exists only on one machine:\n  " + "\n  ".join(offenders)
    )
