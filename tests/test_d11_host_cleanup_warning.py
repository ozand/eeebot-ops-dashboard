"""#356 (architect delta review of D1 #315 @568ed218): a SnapshotCleanupError
carries ``activated=True`` -- ``current`` already serves the new snapshot and
only pruning an older one failed. publish_ordered and the autopublisher treat
it as a cleanup WARNING, not as ``host_snapshot_failed``. A failed ACTIVATION
is still a host failure.

Both cases run the real atomic_snapshot_swap on a real site root; only
``shutil.rmtree`` (prune) or ``os.replace`` (activation) is made to fail.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import scripts.two_sinks as sinks
from scripts import techtree_autopublish as ap
from scripts.two_sinks import atomic_snapshot_swap, publish_ordered


def _refuse_prune_of(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real_rmtree = sinks.shutil.rmtree

    def refuse_prune(path, *args, **kwargs):
        if Path(path).name == name:
            raise OSError("prune refused")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(sinks.shutil, "rmtree", refuse_prune)


def _refuse_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    real_replace = os.replace

    def refuse(source, destination):
        if Path(destination).name == "current":
            raise OSError("activation refused")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", refuse)


def _two_old_snapshots(root: Path) -> None:
    atomic_snapshot_swap(root, {"index.html": "ONE"}, "v1")
    atomic_snapshot_swap(root, {"index.html": "TWO"}, "v2")


def test_cleanup_failure_after_activation_is_a_warning_not_a_host_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "site"
    _two_old_snapshots(root)
    _refuse_prune_of(monkeypatch, "v1")
    published = []
    warnings: list[str] = []

    result = publish_ordered(root, {"index.html": "THREE"}, {}, "v3",
                             lambda pages: published.append(pages) or (0, {"index.html": "fp"}),
                             host_warnings=warnings)

    assert result == (0, {"index.html": "fp"})  # no HostSnapshotError
    assert published, "the public sink ran"
    assert (root / "current").resolve().name == "v3"  # activation happened
    assert len(warnings) == 1 and "cleanup" in warnings[0] and "v1" in warnings[0]


def test_failed_activation_is_still_a_host_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "site"
    _two_old_snapshots(root)
    _refuse_activation(monkeypatch)
    warnings: list[str] = []

    with pytest.raises(sinks.HostSnapshotError) as info:
        publish_ordered(root, {"index.html": "THREE"}, {}, "v3", lambda _pages: (0, {}),
                        host_warnings=warnings)

    assert isinstance(info.value.host_error, sinks.SnapshotActivationError)
    assert info.value.publish_result == (0, {})
    assert warnings == []


def test_publisher_exception_after_cleanup_warning_carries_no_host_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "site"
    _two_old_snapshots(root)
    _refuse_prune_of(monkeypatch, "v1")

    def failing_publisher(_pages):
        raise RuntimeError("gh-pages down")

    with pytest.raises(RuntimeError) as info:
        publish_ordered(root, {"index.html": "THREE"}, {}, "v3", failing_publisher)
    assert info.value.host_error is None  # activated: not a host failure


def _autopublish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site_root: Path) -> tuple[int, dict]:
    state_dir = tmp_path / "publisher"
    ap.save_publish_state(state_dir, "old", 1.0)
    monkeypatch.setenv("GH_TOKEN", "mock-token")
    monkeypatch.setattr(ap, "compute_tree_digest", lambda *_: "new")
    monkeypatch.setattr(ap, "_unreadable_tree_source", lambda *_: None)
    monkeypatch.setattr(ap.tv, "read_local_state", lambda *_a, **_kw: {"_error": None})
    monkeypatch.setattr(ap.tv, "read_ci_freshness", lambda: {})
    monkeypatch.setattr(ap.tv, "render_public_pages", lambda *_a, **_kw: {"index.html": "safe"})
    monkeypatch.setattr(ap.tv, "publish_to_pages",
                        lambda pages, **_kw: (0, {name: "fp" for name in pages}, True))
    args = ap.parse_args(["--state-root", str(tmp_path / "input"), "--state-dir", str(state_dir),
                          "--site-root", str(site_root)])
    rc = ap.run(args)
    return rc, ap.load_publish_state(state_dir)


def test_autopublish_records_cleanup_warning_not_host_snapshot_failed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "site"
    _two_old_snapshots(root)
    _refuse_prune_of(monkeypatch, "v1")

    rc, saved = _autopublish(tmp_path, monkeypatch, root)

    assert rc == 0
    assert "host_snapshot_failed_since" not in saved and "last_host_error" not in saved
    assert saved["digest"] == "new"  # the publication counted
    err = capsys.readouterr().err
    assert "WARNING" in err and "cleanup" in err
    assert (root / "current").resolve().name not in {"v1", "v2"}


def test_autopublish_failed_activation_is_still_host_snapshot_failed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "site"
    _two_old_snapshots(root)
    _refuse_activation(monkeypatch)

    rc, saved = _autopublish(tmp_path, monkeypatch, root)

    assert rc == 1
    assert saved["host_snapshot_failed_since"] is not None
    assert "SnapshotActivationError" in saved["last_host_error"]
