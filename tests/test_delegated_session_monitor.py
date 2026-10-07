from __future__ import annotations

import json
from pathlib import Path

from scripts import delegated_session_monitor as monitor


def test_snapshot_uses_only_allowlisted_session_metadata(monkeypatch):
    pane_rows = []
    for sid, pane_id in monitor.SESSIONS.items():
        pane_rows.append({"pane_id": pane_id, "agent_status": "working", "revision": 3,
                          "agent_session": {"kind": "path", "value": f"/redacted/{sid}.jsonl"}})
    monkeypatch.setattr(monitor, "run_json", lambda args, timeout, deadline=None: (
        {"result": {"panes": pane_rows}} if args[0] == "herdr" else {"state": "OPEN", "title": "safe"}
    ))
    result = monitor.snapshot()
    assert all(v["status"] == "working" for v in result["sessions"].values())
    assert len(result["issues"]) == 3
    assert all(set(value) == {"state"} for value in result["issues"].values())
    assert all("jsonl" not in json.dumps(value) for value in result.values())


def test_missing_or_mismatched_pane_is_unknown(monkeypatch):
    monkeypatch.setattr(monitor, "run_json", lambda args, timeout, deadline=None: {"result": {"panes": []}}
                        if args[0] == "herdr" else {"state": "OPEN", "title": "x"})
    result = monitor.snapshot()
    assert all(v["status"] == "unknown" for v in result["sessions"].values())


def test_issue_lookup_failure_is_unknown(monkeypatch):
    def fail(args, timeout, deadline=None):
        if args[0] == "herdr":
            return {"result": {"panes": []}}
        raise RuntimeError("private command output must not leak")
    monkeypatch.setattr(monitor, "run_json", fail)
    assert all(v["state"] == "unknown" for v in monitor.snapshot()["issues"].values())


def test_persist_is_json_and_atomic(tmp_path: Path):
    target = tmp_path / "state" / "status.json"
    monitor.persist(target, {"timestamp": "t", "sessions": {}})
    assert json.loads(target.read_text()) == {"timestamp": "t", "sessions": {}}
    assert list(target.parent.glob("*.tmp")) == []


def test_lock_rejects_second_owner(tmp_path: Path):
    path = tmp_path / "monitor.lock"
    first = monitor.acquire_lock(path)
    assert first is not None
    second = monitor.acquire_lock(path)
    assert second is None
    first.close()


def test_bounded_loop_polls_again_at_interval(monkeypatch, tmp_path: Path):
    events = []
    calls = []

    def snapshot(deadline=None):
        calls.append(1)
        return {"timestamp": str(len(calls)), "sessions": {}, "issues": {}}

    monkeypatch.setattr(monitor, "snapshot", snapshot)
    monkeypatch.setattr(monitor, "persist", lambda path, value: None)
    monkeypatch.setattr(monitor, "emit", lambda kind, payload: events.append(kind))
    sleeps = []
    clock = [0.0]

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(monitor.time, "sleep", sleep)
    monkeypatch.setattr(monitor.time, "monotonic", lambda: clock[0])
    rc = monitor.main(["--interval", "900", "--max-runtime", "1800", "--state", str(tmp_path / "s.json")])
    assert rc == 0
    assert sleeps == [900, 900]
    assert len(calls) == 2
    assert events == ["WATCH_CHANGE", "WATCH_HEARTBEAT"]


def test_once_emits_sanitized_status(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(monitor, "snapshot", lambda deadline=None: {"timestamp": "t", "sessions": {}, "issues": {}})
    monkeypatch.setattr(monitor, "persist", lambda path, value: None)
    assert monitor.main(["--once", "--state", str(tmp_path / "state.json")]) == 0
    assert capsys.readouterr().out.startswith("WATCH_CHANGE ")


def test_interval_cannot_be_overridden(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(monitor, "acquire_lock", lambda path: object())
    try:
        monitor.main(["--interval", "899", "--state", str(tmp_path / "state.json")])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("production polling interval must remain fixed")


def test_run_json_timeout_is_bounded_by_deadline(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = '{"state": "OPEN"}'

    def run(*args, **kwargs):
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr(monitor.subprocess, "run", run)
    monkeypatch.setattr(monitor.time, "monotonic", lambda: 100.0)
    monitor.run_json(["gh", "issue"], 20, deadline=100.5)
    assert captured["timeout"] == 0.5


def test_snapshot_stops_when_deadline_expires(monkeypatch):
    calls = []

    def run_json(args, timeout, deadline=None):
        calls.append(args[0])
        return {"result": {"panes": []}} if args[0] == "herdr" else {"state": "OPEN"}

    clock = [100.0]
    monkeypatch.setattr(monitor.time, "monotonic", lambda: clock[0])

    def expire_after_panes(args, timeout, deadline=None):
        calls.append(args[0])
        if args[0] == "herdr":
            clock[0] = 101.0
            return {"result": {"panes": []}}
        return {"state": "OPEN"}

    monkeypatch.setattr(monitor, "run_json", expire_after_panes)
    result = monitor.snapshot(deadline=100.5)
    assert calls == ["herdr"]
    assert all(value["state"] == "unknown" for value in result["issues"].values())


def test_issue_failure_adds_sanitized_error_marker(monkeypatch):
    def fail(args, timeout, deadline=None):
        if args[0] == "herdr":
            return {"result": {"panes": []}}
        raise RuntimeError("private command output must not leak")
    monkeypatch.setattr(monitor, "run_json", fail)
    result = monitor.snapshot()
    assert all(value["state"] == "unknown" for value in result["issues"].values())
    assert result["errors"] == [
        f"gh_issue_unavailable:{repo}#{number}"
        for repo, numbers in monitor.ISSUES.items() for number in numbers
    ]
