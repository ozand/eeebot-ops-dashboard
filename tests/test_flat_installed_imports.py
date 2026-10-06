from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_cycle_detail_imports_from_flat_installed_siblings(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    flat_dir = tmp_path / "installed"
    flat_dir.mkdir()
    for module in ("cycle_detail.py", "publish_scan.py", "two_sinks.py", "techtree_viewer.py", "agent_context.py", "about_page.py"):
        shutil.copy2(repo_root / "scripts" / module, flat_dir / module)

    script = "import cycle_detail, two_sinks, techtree_viewer; print('flat-import-ok')"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(flat_dir)!r}); {script}"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "flat-import-ok"


def test_repository_package_imports_remain_supported() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "from scripts import cycle_detail, two_sinks; print('package-import-ok')"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "package-import-ok"
