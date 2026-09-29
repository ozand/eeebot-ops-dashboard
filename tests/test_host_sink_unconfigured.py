"""The host sink is NOT CONFIGURED while its site root does not exist.

D1 runs on the host before D4 creates /var/lib/eeebot-site. Until then every
publisher run logged "host snapshot failed: OSError" and exited 1 although
the public publication succeeded. Architect decision: a site root that does
not exist (ENOENT on the root itself) means the host sink is not configured
-- skip it with one stderr line, record ``host_sink_unconfigured``, exit 0,
publish publicly as usual. D4 creating the root turns the sink on. A root
that EXISTS but cannot be written, or any other OSError in the swap, is still
a host failure (non-zero exit), as before.

Every case drives the real techtree_autopublish.run() -> publish_ordered ->
atomic_snapshot_swap; only the state reader and the GitHub publisher are
stubbed.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import scripts.two_sinks as sinks
from scripts import techtree_autopublish as ap


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site_root: Path,
         *, recent_unconfigured_publish: bool = False) -> tuple[int, dict, list]:
    """``recent_unconfigured_publish``: the previous run (pre-D4) published
    this SAME digest a minute ago with the host sink unconfigured."""
    state_dir = tmp_path / "publisher"
    digest = "same" if recent_unconfigured_publish else "new"
    if recent_unconfigured_publish:
        ap.save_publish_state(state_dir, "same", time.time() - 60, host_sink="host_sink_unconfigured")
    else:
        ap.save_publish_state(state_dir, "old", 1.0)
    uploads: list = []
    monkeypatch.setenv("GH_TOKEN", "mock-token")
    monkeypatch.setattr(ap, "compute_tree_digest", lambda *_: digest)
    monkeypatch.setattr(ap, "_unreadable_tree_source", lambda *_: None)
    monkeypatch.setattr(ap.tv, "read_local_state", lambda *_a, **_kw: {"_error": None})
    monkeypatch.setattr(ap.tv, "read_ci_freshness", lambda: {})
    monkeypatch.setattr(ap.tv, "render_public_pages", lambda *_a, **_kw: {"index.html": "<html>safe</html>"})
    monkeypatch.setattr(ap.tv, "publish_to_pages", lambda pages, **_kw: (
        uploads.append(pages) or (0, {name: "fp" for name in pages}, True)))
    args = ap.parse_args(["--state-root", str(tmp_path / "input"), "--state-dir", str(state_dir),
                          "--site-root", str(site_root)])
    return ap.run(args), ap.load_publish_state(state_dir), uploads


def test_a_absent_site_root_is_unconfigured_not_a_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    site_root = tmp_path / "var-lib" / "eeebot-site"  # D4 has not created it

    rc, state, uploads = _run(tmp_path, monkeypatch, site_root)

    assert rc == 0
    assert uploads and "index.html" in uploads[0], "the public publication runs as usual"
    assert state["digest"] == "new"
    assert state.get("host_sink") == "host_sink_unconfigured"
    assert "host_snapshot_failed_since" not in state and "last_host_error" not in state
    err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert err_lines == [
        f"techtree-autopublish: host sink not configured (site root {site_root} absent); public publish only"]
    assert not site_root.exists(), "the publisher never creates the site root itself"


def test_a_publish_ordered_reports_unconfigured_without_a_host_error(tmp_path: Path) -> None:
    outcome: dict = {}
    result = sinks.publish_ordered(
        tmp_path / "absent", {"index.html": "<html>x</html>"}, {}, "v1", lambda _p: (0, {}), host_outcome=outcome)
    assert result == (0, {})
    assert not (tmp_path / "absent").exists()
    assert outcome == {"status": "host_sink_unconfigured"}


def _make_unwritable(site_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_root.mkdir(parents=True)
    if os.name == "nt":
        # Windows ignores directory write bits; refuse the first write the
        # swap makes inside the root (its publisher lock) as EACCES would.
        # (run() uses the module techtree_autopublish imported: ap.sinks.)
        real_enter = ap.sinks._SiteLock.__enter__

        def refuse(self):
            if Path(self.path).parent == site_root:
                raise PermissionError(13, "Permission denied", str(self.path))
            return real_enter(self)

        monkeypatch.setattr(ap.sinks._SiteLock, "__enter__", refuse)
    else:
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        site_root.chmod(0o555)


def test_b_existing_unwritable_site_root_is_still_a_host_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_root = tmp_path / "site"
    _make_unwritable(site_root, monkeypatch)
    try:
        rc, state, uploads = _run(tmp_path, monkeypatch, site_root)
    finally:
        if os.name != "nt":
            site_root.chmod(0o755)

    assert rc != 0
    assert uploads, "the public sink still runs"
    assert state["host_snapshot_failed_since"] is not None
    assert "OSError" in state["last_host_error"] or "PermissionError" in state["last_host_error"]
    assert state.get("host_sink") != "host_sink_unconfigured"


def test_c_existing_writable_site_root_swaps_normally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_root = tmp_path / "site"
    site_root.mkdir()

    rc, state, uploads = _run(tmp_path, monkeypatch, site_root)

    assert rc == 0 and uploads
    assert (site_root / "current" / "index.html").is_file()
    assert "host_snapshot_failed_since" not in state


# --- Codex 4136424439: the D4 seed after a pre-D4 (unconfigured) publish ---------
# Architect: the trigger is HOST STATE -- the site root exists AND serves no
# valid current snapshot -- never the previous run's host_sink flag.

def test_1_root_appears_without_current_same_digest_publishes_a_seed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-D4 run published digest "same" with the sink unconfigured; D4 then
    creates the EMPTY site root within the staleness window. The same digest
    must not stop the seed: the run swaps and current/index.html exists."""
    site_root = tmp_path / "site"
    site_root.mkdir()  # D4 tmpfiles: empty root

    rc, state, uploads = _run(tmp_path, monkeypatch, site_root, recent_unconfigured_publish=True)

    assert rc == 0
    assert (site_root / "current" / "index.html").is_file(), "D4 seed (test -s current/index.html) needs this"
    assert state.get("host_sink") != "host_sink_unconfigured"


def test_3_no_seed_while_the_site_root_is_still_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reverse: no root yet -> the ordinary no-publish gate (same digest,
    inside the staleness window) still skips the run."""
    site_root = tmp_path / "site"

    rc, state, uploads = _run(tmp_path, monkeypatch, site_root, recent_unconfigured_publish=True)

    assert rc == 0 and uploads == []
    assert not site_root.exists()
    assert state.get("host_sink") == "host_sink_unconfigured"


def test_2_root_with_complete_current_and_same_digest_skips_as_before(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A served, complete current snapshot and an unchanged digest: the
    ordinary no-publish gate -- even though the previous run's flag still
    says host_sink_unconfigured (the flag is reporting, not the trigger)."""
    site_root = tmp_path / "site"
    site_root.mkdir()
    sinks.atomic_snapshot_swap(site_root, {"index.html": "<html>v0</html>"}, "v0")

    rc, state, uploads = _run(tmp_path, monkeypatch, site_root, recent_unconfigured_publish=True)

    assert rc == 0 and uploads == []
    assert (site_root / "current").resolve().name == "v0"



# --- Codex 4136782725: an existing but unusable root is a host FAILURE ----------

def _refuse_site_lock(monkeypatch: pytest.MonkeyPatch, site_root: Path) -> None:
    real_enter = ap.sinks._SiteLock.__enter__

    def refuse(self):
        if Path(self.path).parent == site_root:
            raise PermissionError(13, "Permission denied", str(self.path))
        return real_enter(self)

    monkeypatch.setattr(ap.sinks._SiteLock, "__enter__", refuse)


def test_4_root_that_is_a_file_with_same_digest_publishes_and_fails_the_host(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_root = tmp_path / "site"
    site_root.write_text("not a directory", encoding="utf-8")

    rc, state, uploads = _run(tmp_path, monkeypatch, site_root, recent_unconfigured_publish=True)

    assert rc != 0, "an existing, unusable root is a host failure, never a silent skip"
    assert uploads, "the run was not stopped at the no-publish gate"
    assert state["host_snapshot_failed_since"] is not None
    assert state.get("host_sink") != "host_sink_unconfigured"


def test_5_root_that_cannot_be_stat_ed_publishes_and_fails_the_host(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_root = tmp_path / "site"
    site_root.mkdir()
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path) == site_root:
            raise PermissionError(13, "Permission denied", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(ap.sinks, "current_snapshot_target",
                        lambda root: (_ for _ in ()).throw(PermissionError(13, "Permission denied", str(root))))
    _refuse_site_lock(monkeypatch, site_root)

    rc, state, uploads = _run(tmp_path, monkeypatch, site_root, recent_unconfigured_publish=True)

    assert rc != 0
    assert uploads, "the run was not stopped at the no-publish gate"
    assert state["host_snapshot_failed_since"] is not None
