from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "scripts" / "techtree_viewer.py"
AUTOPUBLISH = ROOT / "scripts" / "techtree_autopublish.py"
SYNC = ROOT / "deploy" / "eeebot-techtree-sync.sh"
DROPIN = ROOT / "deploy" / "eeebot-techtree-publish.service.d-sync.conf"
MANIFEST = ROOT / "deploy" / "sync-manifest.txt"


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


def test_manifest_entries_exist_and_cover_viewer_vendor_references() -> None:
    entries = {
        line.strip() for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    expected = {
        "scripts/techtree_viewer.py",
        "scripts/techtree_autopublish.py",
        "assets/vendor/d3.min.js",
        "assets/vendor/d3-dag.iife.min.js",
        "assets/vendor/lineage-renderer.js",
    }
    assert expected <= entries
    assert all((ROOT / entry).is_file() for entry in entries)
    refs = set(re.findall(r"assets/vendor/[A-Za-z0-9._-]+", VIEWER.read_text(encoding="utf-8")))
    assert refs <= entries


def _manifest_entries() -> list[str]:
    return [
        line.strip() for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _sync_dest() -> str:
    match = re.search(r"^DEST=(\S+)$", SYNC.read_text(encoding="utf-8"), re.M)
    assert match, "sync script does not define DEST"
    return match.group(1)


def test_exec_start_runs_the_file_the_manifest_installs() -> None:
    """Issue #155: the sync installed `scripts/techtree_autopublish.py` under
    $DEST/scripts/ while ExecStart ran the flat $DEST/techtree_autopublish.py.
    Both sides reported success and the live generator was never replaced.
    Derive both paths from the manifest so they cannot drift apart again."""
    entries = _manifest_entries()
    autopublish = [e for e in entries if e.endswith("techtree_autopublish.py")]
    assert len(autopublish) == 1, entries
    installed_path = f"{_sync_dest()}/{autopublish[0]}"

    exec_starts = re.findall(
        r"^ExecStart=\S*python3?\s+(\S+)$", DROPIN.read_text(encoding="utf-8"), re.M
    )
    assert exec_starts == [installed_path], (
        f"ExecStart runs {exec_starts}, but the manifest installs {installed_path}"
    )


def test_no_manifest_entry_installs_outside_the_execution_tree() -> None:
    """The generator resolves its vendored assets as
    `Path(__file__).parent.parent / 'assets' / 'vendor'`, so every manifest
    entry has to land inside $DEST, in the same relative layout as the repo."""
    dest = _sync_dest()
    for entry in _manifest_entries():
        assert not entry.startswith("/"), entry
        assert ".." not in entry, entry
        assert f"{dest}/{entry}".startswith(f"{dest}/"), entry


def test_sync_script_bounds_backup_retention() -> None:
    """One permanent .bak per file per run, with the publish unit firing every
    ~5 minutes, reached 86 files / 11 MB in a single directory before this was
    bounded (issue #155)."""
    text = SYNC.read_text(encoding="utf-8")
    match = re.search(r"^BACKUP_KEEP=(\d+)$", text, re.M)
    assert match, "sync script does not define a BACKUP_KEEP retention bound"
    assert 1 <= int(match.group(1)) <= 10, match.group(1)
    assert "prune_backups" in text


def test_sync_script_and_dropin_contract() -> None:
    result = subprocess.run(["bash", "-n", str(SYNC)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    dropin = DROPIN.read_text(encoding="utf-8")
    assert "ExecStartPre=-+/opt/eeebot-techtree/eeebot-techtree-sync.sh" in dropin
    text = SYNC.read_text(encoding="utf-8")
    assert "raw.githubusercontent.com/ozand/eeebot-ops-dashboard/master" in text
    assert "python3 -m py_compile" in text
    assert "[ -s \"$tmp\" ]" in text
    assert "mv -f \"$tmp\" \"$destination\"" in text
    assert "rollback" in text
