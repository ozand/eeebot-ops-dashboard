from __future__ import annotations

import json
from pathlib import Path

from scripts import delegated_session_monitor as monitor


def test_snapshot_uses_only_allowlisted_session_metadata(monkeypatch):
    pane_rows = []
    for sid, pane_id in monitor.SESSIONS.items():
        pane_rows.append({"pane_id": pane_id, "agent_status": "working", "revision": 3,
                          "agent_session": {"kind": "path", "value": f"/redacted/{sid}.jsonl"}})
    monkeypatch.setattr(monitor, "run_json", lambda args, timeout: (
        {"result": {"panes": pane_rows}} if args[0] == "herdr" else {"state": "OPEN", "title": "safe"}
    ))
    result = monitor.snapshot()
    assert all(v["status"] == "working" for v in result["sessions"].values())
    assert len(result["issues"]) == 3
    assert all(set(value) == {"state"} for value in result["issues"].values())
    assert all("jsonl" not in json.dumps(value) for value in result.values())


def test_missing_or_mismatched_pane_is_unknown(monkeypatch):
    monkeypatch.setattr(monitor, "run_json", lambda args, timeout: {"result": {"panes": []}}
                        if args[0] == "herdr" else {"state": "OPEN", "title": "x"})
    result = monitor.snapshot()
    assert all(v["status"] == "unknown" for v in result["sessions"].values())


def test_issue_lookup_failure_is_unknown(monkeypatch):
    def fail(args, timeout):
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

    def snapshot():
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
    monkeypatch.setattr(monitor, "snapshot", lambda: {"timestamp": "t", "sessions": {}, "issues": {}})
    monkeypatch.setattr(monitor, "persist", lambda path, value: None)
    assert monitor.main(["--once", "--state", str(tmp_path / "state.json")]) == 0
    assert capsys.readouterr().out.startswith("WATCH_CHANGE ")
