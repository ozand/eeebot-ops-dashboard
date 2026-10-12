#!/usr/bin/env python3
"""Bounded read-only monitor for the six delegated Pi sessions."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

SESSIONS = {
    "01a06967": "w17:p15", "01a0c4ed": "w17:p16",
    "01a0e690": "w17:p17", "01a07267": "w17:pG",
    "01a06960": "w17:pE", "01a0d06a": "w17:pF",
}
ISSUES = {
    "ozand/eeebot": [2058],
    "ozand/eeebot-ops-dashboard": [390, 391],
}
INTERVAL = 900
MAX_RUNTIME = 24 * 60 * 60
SAFE_STATUSES = {"idle", "working", "done"}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def run_json(args: list[str], timeout: int, deadline: float | None = None) -> object:
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("monitor deadline reached")
        timeout = min(timeout, max(0.001, remaining))
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {args[0]}")
    return json.loads(result.stdout)


def snapshot(deadline: float | None = None) -> dict:
    pane_error = False
    try:
        panes = run_json(["herdr", "pane", "list", "--workspace", "w17"], 20, deadline)
        rows = panes.get("result", {}).get("panes", []) if isinstance(panes, dict) else []
    except Exception:
        rows = []
        pane_error = True
    sessions = {}
    for sid, expected_pane in SESSIONS.items():
        matches = []
        for pane in rows:
            meta = pane.get("agent_session") or {}
            value = str(meta.get("value", ""))
            if meta.get("kind") == "path" and value.endswith(".jsonl") and sid in value:
                matches.append(pane)
        if len(matches) != 1 or matches[0].get("pane_id") != expected_pane:
            sessions[sid] = {"status": "unknown", "pane": expected_pane}
        else:
            pane = matches[0]
            status = pane.get("agent_status", "unknown")
            sessions[sid] = {"status": status if status in SAFE_STATUSES else "unknown",
                             "pane": expected_pane,
                             "revision": pane.get("revision") if isinstance(pane.get("revision"), int) else None}
    issues = {}
    issue_errors = []
    for repo, numbers in ISSUES.items():
        for number in numbers:
            try:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("monitor deadline reached")
                data = run_json(["gh", "issue", "view", str(number), "--repo", repo,
                                 "--json", "state,title"], 20, deadline)
                issues[f"{repo}#{number}"] = {"state": data.get("state", "unknown")}
            except Exception:
                issues[f"{repo}#{number}"] = {"state": "unknown"}
                issue_errors.append(f"gh_issue_unavailable:{repo}#{number}")
    errors = (["herdr_unavailable"] if pane_error else []) + issue_errors
    return {"timestamp": now(), "sessions": sessions, "issues": issues,
            "errors": errors}


def persist(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="monitor-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if stream.read(1) == "":
                stream.write("\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except (OSError, BlockingIOError):
        stream.close()
        return None


def emit(kind: str, payload: dict) -> None:
    print(kind + " " + json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=INTERVAL)
    parser.add_argument("--state", type=Path,
                        default=Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) /
                                "eeebot-dev-monitor" / "status.json")
    parser.add_argument("--max-runtime", type=int, default=MAX_RUNTIME)
    args = parser.parse_args(argv)
    if args.interval != INTERVAL or not 1 <= args.max_runtime <= MAX_RUNTIME:
        parser.error("interval is fixed at 900 seconds; max-runtime must be 1..86400 seconds")
    lock = acquire_lock(args.state.with_suffix(".lock"))
    if lock is None:
        emit("WATCH_ERROR", {"timestamp": now(), "error": "already_running"})
        return 2
    try:
        previous = None
        try:
            previous = json.loads(args.state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        deadline = time.monotonic() + args.max_runtime
        while time.monotonic() < deadline:
            try:
                current = snapshot(deadline)
                persist(args.state, current)
                if current.get("errors"):
                    emit("WATCH_ERROR", {"timestamp": current["timestamp"], "errors": current["errors"]})
                if previous is None or {k: v for k, v in current.items() if k != "timestamp"} != {k: v for k, v in previous.items() if k != "timestamp"}:
                    emit("WATCH_CHANGE", current)
                else:
                    emit("WATCH_HEARTBEAT", {"timestamp": current["timestamp"]})
                previous = current
            except Exception as exc:
                # Error detail is deliberately reduced to type, never command output.
                emit("WATCH_ERROR", {"timestamp": now(), "error": type(exc).__name__})
            if args.once:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval, remaining))
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
