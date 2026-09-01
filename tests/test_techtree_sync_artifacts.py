from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "scripts" / "techtree_viewer.py"
AUTOPUBLISH = ROOT / "scripts" / "techtree_autopublish.py"
SYNC = ROOT / "deploy" / "eeebot-techtree-sync.sh"
DROPIN = ROOT / "deploy" / "eeebot-techtree-publish.service.d-sync.conf"


def test_both_repo_generators_compile() -> None:
    result = subprocess.run(
        ["python3", "-m", "py_compile", str(VIEWER), str(AUTOPUBLISH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_autopublish_imports_repo_sibling_viewer() -> None:
    result = subprocess.run(
        ["python3", "-c", "import scripts.techtree_autopublish as ap; assert ap.tv.__file__"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_sync_script_and_dropin_contract() -> None:
    result = subprocess.run(["bash", "-n", str(SYNC)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    dropin = DROPIN.read_text(encoding="utf-8")
    assert dropin == "[Service]\nExecStartPre=-+/opt/eeebot-techtree/eeebot-techtree-sync.sh\n"
    text = SYNC.read_text(encoding="utf-8")
    assert "raw.githubusercontent.com/ozand/eeebot-ops-dashboard/master/scripts" in text
    assert "python3 -m py_compile" in text
    assert "mv -f" in text
