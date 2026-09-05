from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


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
        "assets/vendor/lineage-renderer.js",  # #208: d3 + d3-dag are gone
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


# ---------------------------------------------------------------------------
# Issue #210: the host's own copy of sync-manifest.txt is not itself a
# manifest entry, so it can never update itself. A master change that
# deletes or renames an entry (#209 removed the d3 assets) permanently
# deadlocks every future sync: the stale local list names a file master no
# longer has, and the all-or-nothing download rule aborts before the very
# update that would fix the list. These tests drive the *actual* shipped
# script end-to-end (bash -n only checks syntax, not behaviour) against a
# fake `curl` on PATH, in a throwaway DEST -- never the real
# /opt/eeebot-techtree, which requires root and does not exist in CI.
# ---------------------------------------------------------------------------

_FAKE_CURL_BODY = r"""#!/bin/sh
url=""
outfile=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "-o" ]; then
        outfile="$arg"
        prev=""
        continue
    fi
    case "$arg" in
        -*) prev="$arg"; continue ;;
        *) url="$arg" ;;
    esac
done
mode="$(cat "$FAKE_CURL_MODE_FILE" 2>/dev/null || echo ok)"
case "$url" in
    */deploy/sync-manifest.txt)
        if [ "$mode" = "manifest-404" ]; then
            echo "curl: (22) The requested URL returned error: 404" >&2
            exit 22
        elif [ "$mode" = "manifest-garbage" ]; then
            printf '%s' '<html>404 not found</html>' > "$outfile"
            exit 0
        else
            printf '%s\n' 'scripts/foo.py' 'assets/vendor/bar.js' > "$outfile"
            exit 0
        fi
        ;;
    */scripts/foo.py)
        printf '%s\n' 'print("ok")' > "$outfile"
        exit 0
        ;;
    */assets/vendor/bar.js)
        printf '%s\n' 'console.log(1);' > "$outfile"
        exit 0
        ;;
    *)
        echo "curl: (22) The requested URL returned error: 404" >&2
        exit 22
        ;;
esac
"""


def _sh_path(path: Path) -> str:
    """A path in the form the shell (sh, on Windows via MSYS/Cygwin or WSL,
    on Linux natively) can use directly in the script under test -- a
    Windows backslash path fed into a POSIX shell script is parsed as
    escape sequences and word-split on the backslashes, not a single path."""
    posix = path.as_posix()
    if len(posix) > 1 and posix[1] == ":":
        # C:/Temp/... -> /c/Temp/... (MSYS/Cygwin/WSL mount convention).
        posix = f"/{posix[0].lower()}{posix[2:]}"
    return posix


def _write_fake_curl(bin_dir: Path, mode_file: Path) -> None:
    curl_path = bin_dir / "curl"
    body_without_shebang = _FAKE_CURL_BODY.split("\n", 1)[1]
    curl_path.write_text(
        f'#!/bin/sh\nFAKE_CURL_MODE_FILE="{_sh_path(mode_file)}"\n' + body_without_shebang,
        encoding="utf-8",
    )
    curl_path.chmod(curl_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_test_sync_script(dest: Path) -> Path:
    """A copy of the real, shipped script with only DEST repointed at a
    throwaway directory -- everything else, including the manifest-fetch
    and self-heal logic under test, is byte-identical to what ships."""
    text = SYNC.read_text(encoding="utf-8")
    # A plain string replacement, not re.sub(..., f"DEST={dest}", ...): on
    # Windows, dest contains backslashes, which re.sub's replacement-string
    # parser interprets as escape sequences (\T is not a valid one).
    patched = text.replace("DEST=/opt/eeebot-techtree\n", f"DEST={_sh_path(dest)}\n", 1)
    assert patched != text, "could not locate DEST= line to redirect for the test"
    script_path = dest.parent / "eeebot-techtree-sync-under-test.sh"
    script_path.write_text(patched, encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script_path


def _run_sync(tmp_path: Path, *, mode: str, initial_manifest: str) -> subprocess.CompletedProcess:
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "sync-manifest.txt").write_text(initial_manifest, encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mode_file = tmp_path / "mode.txt"
    mode_file.write_text(mode, encoding="utf-8")
    _write_fake_curl(bin_dir, mode_file)

    script_path = _make_test_sync_script(dest)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        ["sh", str(script_path)],
        capture_output=True, text=True, env=env,
    )
    result.dest = dest  # type: ignore[attr-defined]
    return result


@pytest.fixture()
def sh_available() -> bool:
    return shutil.which("sh") is not None


def test_deleted_manifest_entry_no_longer_deadlocks_future_syncs(
    tmp_path: Path, sh_available: bool
) -> None:
    """The exact #209/#210 scenario: the host's stale local manifest still
    names a file (assets/vendor/old.js) that master has already deleted.
    Before this fix, the sync always used the local copy and the download
    for a deleted entry aborted the whole run (exit 1), forever, since the
    manifest could never fix itself. After this fix, master's manifest --
    which no longer lists the deleted entry -- is fetched and used instead,
    so the deleted entry is never even requested and the remaining files
    install successfully."""
    if not sh_available:
        pytest.skip("sh not available")
    result = _run_sync(
        tmp_path, mode="ok",
        initial_manifest="scripts/foo.py\nassets/vendor/old.js\n",
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "installed 2 manifest file(s)" in result.stdout
    installed = sorted(p.relative_to(result.dest).as_posix() for p in result.dest.rglob("*") if p.is_file())
    assert "assets/vendor/old.js" not in installed, "the deleted entry must never be fetched"
    assert "assets/vendor/bar.js" in installed
    assert "scripts/foo.py" in installed


def test_deleted_manifest_entry_deadlocks_the_pre_fix_script(tmp_path: Path, sh_available: bool) -> None:
    """Non-regression control: reproduces the exact bug this issue reports
    against origin/master's un-patched script, so the fix above is proven
    to fix a real, reproduced failure and not an imagined one."""
    if not sh_available:
        pytest.skip("sh not available")
    orig_text = subprocess.run(
        ["git", "show", "origin/master:deploy/eeebot-techtree-sync.sh"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    if not orig_text.strip():
        pytest.skip("origin/master not available in this checkout")

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "sync-manifest.txt").write_text(
        "scripts/foo.py\nassets/vendor/old.js\n", encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mode_file = tmp_path / "mode.txt"
    mode_file.write_text("ok", encoding="utf-8")
    _write_fake_curl(bin_dir, mode_file)

    patched = orig_text.replace("DEST=/opt/eeebot-techtree\n", f"DEST={_sh_path(dest)}\n", 1)
    script_path = tmp_path / "eeebot-techtree-sync-pre-fix.sh"
    script_path.write_text(patched, encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(["sh", str(script_path)], capture_output=True, text=True, env=env)

    assert result.returncode != 0, (
        f"expected the pre-fix script to deadlock on a deleted manifest entry, but it exited 0:\n{result.stdout}"
    )
    assert "download failed: assets/vendor/old.js" in result.stderr


def test_manifest_fetch_failure_falls_back_visibly_and_still_syncs(
    tmp_path: Path, sh_available: bool
) -> None:
    """Publish must never break because GitHub is unreachable. The fallback
    to the local copy is not silent -- it prints a distinct journal line so
    a run on the stale copy is never mistaken for a run on the fresh one."""
    if not sh_available:
        pytest.skip("sh not available")
    result = _run_sync(
        tmp_path, mode="manifest-404",
        initial_manifest="scripts/foo.py\nassets/vendor/bar.js\n",
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "manifest fetch from master failed, falling back to local copy" in result.stderr
    assert "using local manifest" in result.stdout
    assert "installed 2 manifest file(s)" in result.stdout


def test_manifest_fetch_garbage_response_falls_back_visibly(tmp_path: Path, sh_available: bool) -> None:
    """A 200 response that isn't a manifest (an HTML error page, a JSON
    error body) must be rejected the same way a network failure is --
    fall back to the local copy, log why, and keep publishing."""
    if not sh_available:
        pytest.skip("sh not available")
    result = _run_sync(
        tmp_path, mode="manifest-garbage",
        initial_manifest="scripts/foo.py\nassets/vendor/bar.js\n",
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "manifest fetched from master is empty or unrecognisable, falling back to local copy" in result.stderr
    assert "using local manifest" in result.stdout


def test_successful_master_sync_heals_the_local_manifest_copy(tmp_path: Path, sh_available: bool) -> None:
    """Once master's manifest is fetched, validated, and every named file
    actually installs, it is written back over the local fallback copy --
    the step that stops a single deleted entry from deadlocking every
    future run forever: the fallback now reflects the last manifest that
    was proven to work, not whatever it was initialized with."""
    if not sh_available:
        pytest.skip("sh not available")
    result = _run_sync(
        tmp_path, mode="ok",
        initial_manifest="scripts/foo.py\nassets/vendor/old.js\n",
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "local manifest copy updated from master" in result.stdout
    healed = (result.dest / "sync-manifest.txt").read_text(encoding="utf-8")
    assert healed == "scripts/foo.py\nassets/vendor/bar.js\n"
