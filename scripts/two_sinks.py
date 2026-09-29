"""ADR-036 D1 helpers for split rendering and snapshot publication."""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import stat
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit

try:
    from scripts.publish_scan import (
        PUBLIC_PAGE_PATHS as PUBLIC_PAGES,
        PublicationScanError,
        is_allowed_publish_path,
        scan_pages as _publish_scan_pages,
    )
except ImportError:
    from publish_scan import (
        PUBLIC_PAGE_PATHS as PUBLIC_PAGES,
        PublicationScanError,
        is_allowed_publish_path,
        scan_pages as _publish_scan_pages,
    )  # type: ignore

# Reason enums emitted by the ledger/proposer/strategist writers. Keep these
# allowlists next to their sources: nanobot/runtime/cycle_ledger.py:76-96,217;
# bridge.py:5909-5930; llm_proposer.py:736,1292,2957,3007; strategist.py:31,358,378,381.
_PUBLIC_REASON_CODES = frozenset({
    "no_demand", "recent_duplicate_failure", "empty_context", "sizing_rejected",
    "self_dedup", "error", "futile_surface", "enhancement_without_caller",
    "operator_owned_path", "llm_unavailable", "all_cooled", "inputs_unavailable",
    "valid bounded advisory output applied", "no writes applied; watermark unchanged",
    "no_valuable_task", "already_done", "already_done_tag", "no_plan", "dirty_tree",
    "blocked_filename_pattern", "workspace_unavailable", "success", "partial", "failed",
    "skipped-duplicate", "promotion_candidate", "push_pending", "pushed_late",
    "superseded", "abandoned", "paused-supplier", "existence_index_duplicate",
    "executor_reported_skipped", "test_weakening", "gate_failed", "mutation_surface_violation",
    "blocked_file_present", "out_of_band_main_detected", "switch_base_gate_error",
    "switch_base_gate_blocked", "head_on_main_precondition_failed", "no_commit", "internal_error",
    "executor_llm_error", "refused", "malformed", "push_failed", "commit_failed", "spawn_failed",
    "timed_out", "proceeded", "skipped_duplicate", "skipped_recent_failure", "clean",
    "cannot_ask", "unanswerable", "absent", "present", "probe_unavailable", "targets_missing",
    "integrated", "blocked", "model_call_incomplete",
})
_PUBLIC_LEDGER_DECISIONS = frozenset({"skipped_duplicate", "proceeded", "skipped_recent_failure"})
_COUNTER_SUFFIXES = ("_chars", "_lines", "_count")
#: #315 R9: a counter is a snake_case FIELD NAME ending in a counter suffix.
#: Map keys that are identifiers (a repository "ozand/request_count", a cycle
#: id) are data, not fields, and are never scrubbed as counters.
_COUNTER_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

PUBLIC_DATA_KEYS = frozenset({
    "portfolio", "scorecard", "evolution_tree", "hypotheses", "hypotheses_durable",
    "ledger_tail", "ledger_history", "demand_rotation", "demand_completed", "skill_reads",
    "skill_evals", "ci_freshness", "cycle_titles", "cycle_files",
    "llm_stats", "proposer_stats", "local_ci", "executor_model_status", "executor_llm_stats",
    "compaction", "token_heatmap", "lessons", "subagent_records", "derived_view",
    "reflections", "bridge_exit_streak", "bridge_exits", "bridge_runs", "bridge_active_run", "strategist_decisions",
    "demand_futility", "systemd_drift", "goal_meta", "agents_meta", "agent_context",
    "generator_sha", "_newest_source_age_seconds", "_error",
})

PRIVATE_DATA_KEYS = frozenset({
    "cycle_prompts", "goal_text", "agents_md",
})
DEFAULT_SITE_ROOT = "/var/lib/eeebot-site"
DEFAULT_BIND_ADDRESS = "0.0.0.0"
DEFAULT_BIND_PORT = 8080


def parse_bind_settings(address: str = DEFAULT_BIND_ADDRESS, port: int = DEFAULT_BIND_PORT) -> tuple[str, int]:
    if not address or not 1 <= int(port) <= 65535:
        raise ValueError("ADR-036 host bind settings require an address and port 1..65535")
    try:
        parsed_address = ip_address(address)
    except ValueError:
        parsed_address = None
    if parsed_address is not None and parsed_address.version == 6:
        raise ValueError("ADR-036 dashboard bind address must use IPv4")
    return address, int(port)


#: #315 F4: names a snapshot version may never take -- the activation link
#: itself. Names starting with "." are reserved for staging/link temporaries.
_RESERVED_VERSION_NAMES = frozenset({"current"})


def validate_version_name(version: object) -> str:
    """#315 F4: the ONE version-name validator shared by the writer
    (atomic_snapshot_swap/add_snapshot_version) and the server."""
    if (not isinstance(version, str) or not _VERSION_RE.fullmatch(version)
            or version.startswith(".") or version in _RESERVED_VERSION_NAMES):
        raise ValueError(f"invalid snapshot version: {version!r}")
    return version


def _is_valid_version_name(version: object) -> bool:
    try:
        validate_version_name(version)
    except ValueError:
        return False
    return True


def current_snapshot_target(site_root: Path) -> Path | None:
    """The resolved directory ``site_root/current`` points at, or None when
    it is not a symlink to a complete, validly named sibling version."""
    root = Path(os.path.realpath(site_root))
    current = root / "current"
    try:
        if not current.is_symlink():
            return None
        target = Path(os.path.realpath(current))
    except OSError:
        return None
    if target.parent != root or not _is_valid_version_name(target.name) or not _complete_snapshot(target):
        return None
    return target


def _has_control_character(text: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)


class _RequestRejected(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SnapshotHTTPRequestHandler(SimpleHTTPRequestHandler):
    """#315 part A: serves ONLY the snapshot ``current`` points at.

    Versions are not addressable in the URL and nothing redirects. Each
    request reads ``current`` once and resolves its path inside that one
    target; a path segment naming ``current`` or any entry of the site root
    (a version directory, staging, the lock) is 404. The file is opened
    before any header is sent (os.open + fstat; the last component must not
    be a symlink; the opened file's realpath must lie inside the target and
    be the same inode), so Content-Length is the size of the bytes actually
    sent and a symlink swapped in between check and open is refused.
    """

    site_root: Path

    def list_directory(self, path: str | os.PathLike) -> Any:
        self.send_error(404, "Directory listing disabled")
        return None

    def _request_segments(self) -> tuple[list[str], bool]:
        """(path segments, trailing slash) of the decoded request path."""
        raw = self.path
        try:
            parts = urlsplit(raw)  # F10: a malformed URI is a 400, not a crash
        except ValueError as exc:
            raise _RequestRejected(400, "Invalid request target") from exc
        if parts.scheme or parts.netloc or raw.startswith("//") or not parts.path.startswith("/"):
            raise _RequestRejected(400, "Invalid request target")
        try:
            decoded = unquote(parts.path, errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise _RequestRejected(400, "Invalid request target") from exc
        if _has_control_character(decoded) or "\\" in decoded:
            raise _RequestRejected(400, "Invalid request target")
        segments = [segment for segment in decoded.split("/") if segment]
        if any(segment in {".", ".."} for segment in segments):
            raise _RequestRejected(404, "Not found")
        return segments, decoded.endswith("/")

    def _open_inside(self, target: Path, segments: list[str]) -> tuple[int, os.stat_result, str]:
        """Open ``segments`` inside ``target`` before any header is sent."""
        candidate = os.path.join(str(target), *segments) if segments else str(target)
        for _attempt in range(2):
            try:
                link_stat = os.lstat(candidate)
            except OSError as exc:
                raise _RequestRejected(404, "Not found") from exc
            if stat.S_ISLNK(link_stat.st_mode):
                raise _RequestRejected(404, "Not found")
            if stat.S_ISDIR(link_stat.st_mode):
                candidate = os.path.join(candidate, "index.html")  # F9: explicit index
                continue
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            try:
                fd = os.open(candidate, flags)
            except OSError as exc:
                raise _RequestRejected(404, "Not found") from exc
            try:
                opened = os.fstat(fd)
                real = os.path.realpath(candidate)
                real_stat = os.stat(real)
                if (not stat.S_ISREG(opened.st_mode)
                        or not Path(real).is_relative_to(target)
                        or (opened.st_dev, opened.st_ino) != (real_stat.st_dev, real_stat.st_ino)):
                    raise _RequestRejected(404, "Not found")
            except BaseException:
                os.close(fd)
                raise
            return fd, opened, real
        raise _RequestRejected(404, "Not found")

    def _serve_snapshot(self, method: str) -> None:
        try:
            segments, trailing_slash = self._request_segments()
            site_root = Path(os.path.realpath(self.site_root))
            target = current_snapshot_target(site_root)  # read ONCE per request
            if target is None:
                raise _RequestRejected(503, "Current snapshot unavailable")
            try:
                site_entries = set(os.listdir(site_root))
            except OSError as exc:
                raise _RequestRejected(503, "Current snapshot unavailable") from exc
            if any(segment == "current" or segment.startswith(".") or segment in site_entries
                   for segment in segments):
                raise _RequestRejected(404, "Not found")
            if trailing_slash or not segments:
                segments = [*segments, "index.html"]
            fd, opened, real = self._open_inside(target, segments)
        except _RequestRejected as rejected:
            self.send_error(rejected.code, rejected.message)
            return
        with os.fdopen(fd, "rb") as stream:
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(real))  # F11: same headers for HEAD
            self.send_header("Content-Length", str(opened.st_size))
            self.end_headers()
            if method != "HEAD":
                shutil.copyfileobj(stream, self.wfile)

    def do_GET(self) -> None:
        self._serve_snapshot("GET")

    def do_HEAD(self) -> None:
        self._serve_snapshot("HEAD")

    def send_response(self, code: int, message: str | None = None) -> None:
        super().send_response(code, message)
        self._last_response_code = code


def _complete_snapshot(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and (path / "index.html").is_file()


def _validate_page_name(name: str) -> None:
    normalized = name.replace("\\", "/")
    path = Path(name)
    if (not normalized or normalized.startswith("/") or path.is_absolute()
            or re.match(r"^[A-Za-z]:", normalized)
            or any(part == ".." for part in normalized.split("/"))):
        raise ValueError(f"invalid relative page name: {name!r}")


def serve_site(site_root: Path, address: str = DEFAULT_BIND_ADDRESS, port: int = DEFAULT_BIND_PORT) -> None:
    address, port = parse_bind_settings(address, port)
    site_root = site_root.resolve()
    if current_snapshot_target(site_root) is None:
        raise FileNotFoundError(f"ADR-036 current snapshot unavailable: {site_root / 'current'}")

    class Handler(SnapshotHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site_root), **kwargs)

    Handler.site_root = site_root
    ThreadingHTTPServer((address, port), Handler).serve_forever()


def _reason_bucket(value: str) -> str:
    size = len(value)
    return "withheld (<=16)" if size <= 16 else "withheld (<=64)" if size <= 64 else "withheld (>64)"


def _project_reason(value: object, withheld: dict[str, int] | None, category: str) -> str:
    if isinstance(value, str) and value in _PUBLIC_REASON_CODES:
        return value
    if withheld is not None:
        withheld[category] = withheld.get(category, 0) + 1
    return _reason_bucket(value if isinstance(value, str) else "")


def _is_counter_field(key: object) -> bool:
    return isinstance(key, str) and key.endswith(_COUNTER_SUFFIXES) and bool(_COUNTER_FIELD_RE.fullmatch(key))


def _is_count(value: object) -> bool:
    """#315 R6: a published counter is a non-negative int, never a bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _drop_source_counters(value: object, generated: set[str] | None = None) -> object:
    if isinstance(value, dict):
        return {
            key: _drop_source_counters(item, generated)
            for key, item in value.items()
            if not (_is_counter_field(key) and (generated is None or key not in generated))
        }
    if isinstance(value, list):
        return [_drop_source_counters(item, generated) for item in value]
    return value


def _drop_invalid_counters(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _drop_invalid_counters(item)
            for key, item in value.items()
            if not (_is_counter_field(key) and not _is_count(item))
        }
    if isinstance(value, list):
        return [_drop_invalid_counters(item) for item in value]
    return value


_GOAL_META_STATES = frozenset({"absent", "unexpected_shape", "present"})


def _project_meta(key: str, value: object) -> dict[str, Any]:
    """#315 R6/R7: agents_meta / goal_meta carry only typed fields. The bare
    counters (``lines``, ``chars``, ``priority_count``/``count``) are
    published only as a non-negative int and are OMITTED when unavailable --
    never None, a string or a bool. Applied to input-supplied values AND to
    the metadata split_render_inputs builds itself."""
    if not isinstance(value, dict):
        value = {}
    result: dict[str, Any] = {"present": value.get("present") is True}
    counters = ("lines", "chars", "priority_count") if key == "goal_meta" else ("lines", "chars", "count")
    for counter in counters:
        if _is_count(value.get(counter)):
            result[counter] = value[counter]
    if key == "goal_meta":
        state = value.get("state")
        result["state"] = state if isinstance(state, str) and state in _GOAL_META_STATES else "unexpected_shape"
    return result


def _project_bridge_runs(value: object, withheld: dict[str, int] | None) -> list[dict[str, Any]]:
    """#315 R1 (partial, D1 scope): the free-text fields of a bridge run --
    ``error``, ``reason``, ``last_where`` -- become codes, as in the
    bridge_exit_streak dict branch. (Full row allowlisting is D1.1 #356.)"""
    if not isinstance(value, list):
        return []
    runs = []
    for row in value:
        if not isinstance(row, dict):
            continue
        run = dict(row)
        if "error" in run:
            run["error"] = "error" if run["error"] else ""
        if "last_where" in run:
            run["last_where"] = "withheld" if run["last_where"] else ""
        if "reason" in run:
            run["reason"] = _project_reason(run["reason"], withheld, "bridge_run_reason")
        runs.append(run)
    return runs


def _sanitize_public_value(key: str, value: object, withheld: dict[str, int] | None = None) -> object:
    if key in {"agents_meta", "goal_meta"}:
        return _project_meta(key, value)
    if key == "_error":
        # #315 R1: the transport/state-read error text can embed host paths
        # or stderr; only its presence is public.
        return "state_read_failed" if value else None
    if key == "bridge_runs":
        return _drop_invalid_counters(_project_bridge_runs(_drop_source_counters(value), withheld))
    if key == "bridge_exit_streak" and not isinstance(value, dict):
        # #315 R1: a non-dict streak is never passed through raw.
        return {}
    if key in {"cycle_titles_error", "probe_error"}:
        if value:
            if withheld is not None:
                withheld["probe_error"] = withheld.get("probe_error", 0) + 1
            return "probe_unavailable"
        return "absent"
    if key == "derived_view" and isinstance(value, dict):
        return _drop_invalid_counters(_sanitize_derived_view(value, withheld))
    if isinstance(value, (dict, list)):
        value = _drop_source_counters(value)
    if key == "ci_freshness" and isinstance(value, dict):
        allowed = {"success", "failure", "cancelled", "skipped", "in_progress", "queued", "cannot_ask", "unanswerable", "absent", "unknown"}
        result = {}
        for repo, row in value.items():
            if not isinstance(row, dict):
                continue
            state = row.get("state")
            result[str(repo)] = {
                "state": state if isinstance(state, str) and state in allowed else "unknown",
                **({k: row[k] for k in ("latest_conclusion", "observed_at_utc", "actions_enabled") if k in row and isinstance(row[k], (str, int, bool))}),
            }
        return result
    if key in {"ledger_tail", "ledger_history"} and isinstance(value, list):
        rows = []
        for row in value:
            if not isinstance(row, dict):
                rows.append(row)
                continue
            projected = dict(row)
            if "reason" in projected:
                projected["reason"] = _project_reason(projected["reason"], withheld, "ledger_reason")
            if "decision" in projected:
                decision = projected.get("decision")
                projected["decision"] = decision if isinstance(decision, str) and decision in _PUBLIC_LEDGER_DECISIONS else "[withheld]"
            rows.append(projected)
        return _drop_invalid_counters(rows)
    if key == "agent_context" and isinstance(value, dict):
        ctx = copy.deepcopy(value)
        for field in ("prompt_text", "task_text"):
            if isinstance(ctx.get(field), str):
                ctx[f"{field}_chars"] = len(ctx[field])
                ctx[field] = None
        # #315 R8: shapes the projection and the renderer iterate are
        # normalized first -- a broken one becomes empty, never passes through.
        if "tier2_skills" in ctx and not isinstance(ctx["tier2_skills"], list):
            ctx["tier2_skills"] = []
        if "tier2_memory" in ctx:
            if not isinstance(ctx["tier2_memory"], dict):
                ctx["tier2_memory"] = {}
            elif "files" in ctx["tier2_memory"] and not isinstance(ctx["tier2_memory"]["files"], list):
                ctx["tier2_memory"]["files"] = []
        skills = ctx.get("tier2_skills")
        for skill in skills if isinstance(skills, list) else []:
            if isinstance(skill, dict):
                for field in ("content", "desc"):
                    if isinstance(skill.get(field), str):
                        skill[f"{field}_chars"] = len(skill[field])
                        skill[field] = ""
        fit = ctx.get("prompt_fit")
        if isinstance(fit, dict):
            fit.pop("reason", None)
            fit.pop("summary", None)
        memory = ctx.get("tier2_memory")
        mem_files = memory.get("files") if isinstance(memory, dict) else None
        for mem in mem_files if isinstance(mem_files, list) else []:
            if isinstance(mem, dict) and isinstance(mem.get("content"), str):
                mem["content_chars"] = len(mem["content"])
                mem["content"] = ""
        return _drop_invalid_counters(ctx)
    if key == "bridge_exit_streak" and isinstance(value, dict):
        result = {k: value[k] for k in ("consecutive_failures", "last_ts", "count")
                  if k in value and isinstance(value[k], int) and not isinstance(value[k], bool) and value[k] >= 0}
        if value.get("last_error"):
            result["last_error"] = "error"
        return result
    if key == "bridge_exits" and isinstance(value, list):
        projected = []
        for row in value:
            if not isinstance(row, dict):
                continue
            item = {k: row[k] for k in ("ts", "cycle_id", "exit_code", "classification") if k in row}
            if "exit_code" in item and (isinstance(item["exit_code"], bool) or not isinstance(item["exit_code"], int)):
                item.pop("exit_code")
            if row.get("error"):
                item["error"] = "error"
            projected.append(item)
        return projected
    if key == "bridge_active_run" and isinstance(value, dict):
        allowed = {"run_id", "cycle_id", "started_at", "finished_at", "classification", "exit_status", "outcome"}
        result = {k: value[k] for k in allowed if k in value and isinstance(value[k], (str, int, bool))}
        if value.get("error"):
            result["error"] = "bridge error withheld"
        return result
    if key == "subagent_records" and isinstance(value, list):
        records = []
        for rec in value:
            if isinstance(rec, dict):
                r = dict(rec)
                for field in ("task", "summary", "result", "task_excerpt", "summary_excerpt", "result_excerpt"):
                    if field in r:
                        if isinstance(r[field], str):
                            r[f"{field}_chars"] = len(r[field])
                        r[field] = ""
                records.append(r)
            else:
                records.append(rec)
        return _drop_invalid_counters(records)
    if key == "reflections" and isinstance(value, list):
        refs = []
        for rec in value:
            if isinstance(rec, dict):
                r = dict(rec)
                if "summary" in r:
                    r["summary_chars"] = len(r["summary"]) if isinstance(r["summary"], str) else 0
                    r["summary"] = ""
                if "findings" in r:
                    r["findings_count"] = len(r["findings"]) if isinstance(r["findings"], list) else 0
                    r["findings"] = []
                if "recommendations" in r:
                    r["recommendations_count"] = len(r["recommendations"]) if isinstance(r["recommendations"], list) else 0
                    r["recommendations"] = []
                refs.append(r)
            else:
                refs.append(rec)
        return _drop_invalid_counters(refs)
    if key == "strategist_decisions" and isinstance(value, list):
        decs = []
        for rec in value:
            if isinstance(rec, dict):
                r = dict(rec)
                decision = r.get("decision")
                if decision is not None:
                    r["decision"] = _project_reason(decision, withheld, "strategist_decision")
                r["rationale"] = ""
                r.pop("details", None)
                reason = r.get("reason")
                if reason is not None:
                    r["refused"] = (
                        isinstance(reason, str)
                        and (reason in {"refused", "declined"}
                             or reason.startswith(("refused:", "declined:")))
                    )
                    r["reason"] = "refused" if r["refused"] else _project_reason(
                        reason, withheld, "strategist_reason")
                decs.append(r)
            else:
                decs.append(rec)
        return _drop_invalid_counters(decs)
    if key == "lessons" and isinstance(value, list):
        les = []
        for rec in value:
            if isinstance(rec, dict):
                r = dict(rec)
                for field in ("problem", "solution", "insight", "result"):
                    if field in r:
                        if isinstance(r[field], str):
                            r[f"{field}_chars"] = len(r[field])
                        r[field] = ""
                r["_v2_lesson"] = bool(rec.get("problem"))
                les.append(r)
            else:
                les.append(rec)
        return _drop_invalid_counters(les)
    if key == "local_ci" and isinstance(value, dict):
        l_proj: dict[str, Any] = {k: value[k] for k in ("probe", "state", "ts_utc") if k in value}
        exit_code = value.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            l_proj["exit_code"] = exit_code
        targets_checked = value.get("targets_checked")
        if isinstance(targets_checked, int) and not isinstance(targets_checked, bool) and targets_checked >= 0:
            l_proj["targets_checked"] = targets_checked
        state = value.get("state")
        if state == "targets_missing":
            l_proj["summary"] = "targets missing"
        elif state == "ran":
            # #315 R2: only the VALIDATED exit code (an int, never a bool --
            # False == 0 would read "passed") may name the result.
            code = l_proj.get("exit_code")
            if code is None:
                l_proj["summary"] = "result unavailable"
            else:
                l_proj["summary"] = "passed" if code == 0 else f"failed (exit {code})"
        elif value.get("probe") == "absent":
            l_proj["summary"] = "local CI status absent"
        elif value.get("probe") == "probe_unavailable":
            l_proj["summary"] = "local CI status unavailable"
        return _drop_invalid_counters(l_proj)
    return _drop_invalid_counters(value)
def _sanitize_derived_view(value: dict[str, Any], withheld: dict[str, int] | None) -> dict[str, Any]:
    proj: dict[str, Any] = {}
    for field in ("status", "schema_version", "generated_at_utc", "sort", "derived_status"):
        if field in value:
            proj[field] = value[field]
    status = value.get("status")
    if isinstance(status, str) and status in {"absent", "probe_unavailable", "present"}:
        proj["reason"] = {"absent": "derived view absent", "probe_unavailable": "derived view unavailable", "present": "derived view present"}[status]
    charter = value.get("charter")
    if isinstance(charter, dict):
        source = charter.get("source")
        proj["charter"] = {k: charter[k] for k in ("source", "merged") if k in charter}
        if source == "release_goals_md" and isinstance(charter.get("text"), str):
            proj["charter"]["text"] = charter["text"]
        elif isinstance(charter.get("text"), str) and charter["text"]:
            if withheld is not None:
                withheld["derived_charter_text"] = withheld.get("derived_charter_text", 0) + 1
    priorities = value.get("derived_priorities")
    if isinstance(priorities, list):
        result = []
        for item in priorities:
            if not isinstance(item, dict):
                continue
            row = {k: item[k] for k in ("label", "vector", "direction", "added_utc") if k in item}
            number = item.get("number")
            if isinstance(number, int) and not isinstance(number, bool):
                row["number"] = number
            result.append(row)
        proj["derived_priorities"] = result
    items = value.get("priority_items")
    if isinstance(items, list):
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = {k: item[k] for k in ("rank", "id", "kind", "vector", "provenance", "direction") if k in item}
            number = item.get("number")
            if isinstance(number, int) and not isinstance(number, bool):
                row["number"] = number
            if item.get("provenance") == "operator":
                row["label"] = f"Priority #{number}" if isinstance(number, int) and not isinstance(number, bool) else "Operator priority"
            elif isinstance(item.get("label"), str):
                row["label"] = item["label"]
            evidence = item.get("evidence")
            if isinstance(evidence, str) and evidence and withheld is not None:
                withheld["priority_evidence"] = withheld.get("priority_evidence", 0) + 1
            result.append(row)
        proj["priority_items"] = result
    return proj


def split_render_inputs(data: dict) -> tuple[dict, dict]:
    """Allowlist public fields and replace private text with derived metadata."""
    withheld: dict[str, int] = {}
    public = {}
    for key, value in data.items():
        if key not in PUBLIC_DATA_KEYS or key in PRIVATE_DATA_KEYS:
            continue
        try:
            public[key] = _sanitize_public_value(key, value, withheld)
        except Exception:
            # #315 R8: a broken section gets a fixed safe value for THAT
            # section; it never aborts the projection of the others.
            public[key] = None
            withheld["projection_error"] = withheld.get("projection_error", 0) + 1
    private = dict(data)
    if withheld:
        private["withheld_reason_counts"] = withheld
    else:
        private.pop("withheld_reason_counts", None)
    raw_agents = data.get("agents_md")
    if isinstance(raw_agents, str):
        public["agents_meta"] = {
            "present": True,
            "lines": len(raw_agents.strip().splitlines()),
            "chars": len(raw_agents.strip()),
        }
    elif raw_agents is not None:
        public["agents_meta"] = {"present": False, "lines": 0, "chars": 0}

    raw_goal = data.get("goal_text")
    # #315 R7: unavailable counters are OMITTED, and the finished metadata
    # goes through the same validator as input-supplied metadata (R6).
    if raw_goal is None:
        goal_meta: dict[str, Any] = {"state": "absent", "present": False}
    elif not isinstance(raw_goal, dict):
        goal_meta = {"state": "unexpected_shape", "present": False}
    else:
        # goal_text.json is operator-private. Public metadata comes only from
        # the repository goals.md charter consumed by the release pipeline.
        p_list = raw_goal.get("priorities")
        goal_meta = {"state": "present", "present": True}
        if isinstance(p_list, list):
            goal_meta["priority_count"] = len(p_list)
    public["goal_meta"] = _project_meta("goal_meta", goal_meta)
    if "agents_meta" in public:
        public["agents_meta"] = _project_meta("agents_meta", public["agents_meta"])

    return public, private


def _safe_component(value: object) -> str:
    return str(value).replace("\\", "/")


def scan_pages(pages: Mapping[str, str]) -> None:
    """Validate pages against leak scanner (delegates to scripts/publish_scan.py)."""
    _publish_scan_pages(dict(pages))


def scan_built_tree(root: Path) -> None:
    """Scan all files on disk under root using scripts/publish_scan.py.

    #315 C2: fail closed on anything but directories and regular files. A
    symlink is committed by git as its TARGET PATH, which the content scan
    never sees (it would read the file the link points at); a FIFO/device
    is not page content at all."""
    root = root.resolve()
    pages = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise PublicationScanError(f"ADR-036 built tree contains a non-regular entry: {relative}")
        if stat.S_ISREG(mode):
            pages[relative] = path.read_text(encoding="utf-8", errors="replace")
    _publish_scan_pages(pages)


def validate_publish_allowlist(pages: Mapping[str, str]) -> None:
    for name in pages:
        _validate_page_name(name)
        if not is_allowed_publish_path(name):
            raise PublicationScanError(f"ADR-036 unlisted publish path: {name}")


def add_snapshot_version(pages: dict[str, str], version: str, generated_at: str | None = None) -> dict[str, str]:
    validate_version_name(version)
    for name in pages:
        _validate_page_name(name)
    stamp = generated_at or __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    meta_tag = f'<meta name="snapshot-version" content="{version}">'
    footer_tag = f'<footer class="snapshot-meta">Snapshot {version} · generated {stamp}</footer>'
    updated = {}
    for name, text in pages.items():
        if name.endswith(".html"):
            if meta_tag not in text:
                if "</head>" in text:
                    text = text.replace("</head>", f"{meta_tag}</head>", 1)
                else:
                    text = f"{meta_tag}\n{text}"
            if footer_tag not in text:
                if "</body>" in text:
                    text = text.replace("</body>", f"{footer_tag}</body>", 1)
                else:
                    text = f"{text}\n{footer_tag}"
        updated[name] = text
    return updated


def render_private_pages(private_data: dict, host: str) -> dict[str, str]:
    """D1 boundary seam; private cycle rendering is deliberately deferred to D2."""
    del private_data, host
    return {}


class SnapshotActivationError(RuntimeError):
    """#315 F6: ``current`` was NOT switched; the candidate was removed
    (``cleanup_error`` says if that removal itself failed)."""

    activated = False

    def __init__(self, message: str, cleanup_error: BaseException | None = None):
        super().__init__(message)
        self.cleanup_error = cleanup_error


class SnapshotCleanupError(RuntimeError):
    """#315 F6: ``current`` WAS switched to ``destination``; only pruning
    older snapshots afterwards failed."""

    activated = True

    def __init__(self, message: str, destination: Path):
        super().__init__(message)
        self.destination = destination


class _SiteLock:
    """#315 F7: one publisher at a time for the whole capture, install,
    activate and cleanup cycle -- an exclusive lock on ``.publish.lock`` in
    the site root (the one directory the publisher unit may write; the
    server serves only inside the current target, never a site-root entry,
    and the dotted name is never a version, so it is never pruned)."""

    def __init__(self, site_root: Path):
        self.path = site_root / ".publish.lock"
        self._stream = None

    def __enter__(self) -> "_SiteLock":
        self._stream = open(self.path, "a+b")
        try:
            import fcntl
        except ImportError:  # Windows
            import msvcrt
            import time as _time
            self._stream.seek(0)
            while True:
                try:
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    _time.sleep(0.05)
        else:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc) -> None:
        try:
            try:
                import fcntl
            except ImportError:
                import msvcrt
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()


#: Development-only (Windows cannot rename over an existing directory
#: symlink): unlink-then-rename. On POSIX activation is ONE os.replace, and a
#: failure there is a failed activation -- never retried non-atomically.
_WINDOWS_ACTIVATION_FALLBACK = os.name == "nt"


def _chmod_strict(path: Path, mode: int) -> None:
    """#315 F5: a permission the DynamicUser server needs is never skipped
    silently -- a chmod failure fails the swap."""
    os.chmod(path, mode)


def atomic_snapshot_swap(site_root: Path, pages: dict[str, str], version: str) -> Path:
    """Build an immutable version dir, then atomically replace the current symlink.

    #315: the version name goes through the shared validator (F4); the
    candidate must be complete before activation -- non-empty pages with an
    index.html (F4); every directory created is chmod-ed and a chmod error
    fails the swap (F5); a failed activation removes the non-activated
    candidate and raises SnapshotActivationError, while a failure pruning
    older snapshots after activation raises SnapshotCleanupError (F6); the
    whole cycle runs under an exclusive publisher lock (F7).
    """
    validate_version_name(version)
    if not pages or "index.html" not in pages:
        raise ValueError("ADR-036 snapshot candidate is incomplete: pages must be non-empty and include index.html")
    for name in pages:
        _validate_page_name(name)
    created_root = not site_root.exists()
    site_root.mkdir(parents=True, exist_ok=True)
    if created_root:
        _chmod_strict(site_root, 0o755)
    with _SiteLock(site_root):
        return _swap_locked(site_root, pages, version)


def _swap_locked(site_root: Path, pages: dict[str, str], version: str) -> Path:
    destination = site_root / version
    current_link = site_root / "current"
    previous = current_snapshot_target(site_root)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    # #315 (architect decision): staging lives INSIDE the site root, the one
    # directory the publisher unit may write (ReadWritePaths), under a
    # dot-named directory the server never serves; the final rename is
    # therefore within one filesystem.
    staging_root = site_root / ".staging"
    if not staging_root.exists():
        staging_root.mkdir()
        _chmod_strict(staging_root, 0o755)
    staging = staging_root / version
    if staging.exists() or staging.is_symlink():
        shutil.rmtree(staging)  # left behind by an interrupted run (we hold the lock)
    staging.mkdir()
    try:
        _chmod_strict(staging, 0o755)
        for name, contents in pages.items():
            _validate_page_name(name)
            target = staging / name
            missing = []
            parent = target.parent
            while parent != staging and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            for created in reversed(missing):  # F5: EVERY created directory
                _chmod_strict(created, 0o755)
            target.write_text(contents, encoding="utf-8")
            _chmod_strict(target, 0o644)
        if not _complete_snapshot(staging):
            raise ValueError("ADR-036 snapshot candidate is incomplete: index.html missing after install")
        os.replace(staging, destination)
        try:
            staging_root.rmdir()  # leave no empty .staging behind; kept if an old leftover remains
        except OSError:
            pass
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    link_tmp = site_root / f".current-{version}"
    try:
        if link_tmp.is_symlink() or link_tmp.exists():
            link_tmp.unlink()
        link_tmp.symlink_to(version, target_is_directory=True)
        try:
            os.replace(link_tmp, current_link)
        except OSError:
            if not _WINDOWS_ACTIVATION_FALLBACK:
                raise
            # Windows cannot replace an existing directory symlink in one
            # rename: unlink, then rename -- and put the previous link back
            # if the rename still fails, so a failed activation never leaves
            # the site without `current`.
            if current_link.is_symlink():
                current_link.unlink()
            try:
                os.replace(link_tmp, current_link)
            except OSError:
                if previous is not None and not current_link.is_symlink():
                    current_link.symlink_to(previous.name, target_is_directory=True)
                raise
    except BaseException as exc:
        cleanup_error = None
        for leftover in (link_tmp,):
            try:
                if leftover.is_symlink() or leftover.exists():
                    leftover.unlink()
            except OSError as leftover_exc:
                cleanup_error = leftover_exc
        try:
            shutil.rmtree(destination)
        except OSError as rm_exc:
            cleanup_error = cleanup_error or rm_exc
        raise SnapshotActivationError(
            f"ADR-036 snapshot activation failed ({type(exc).__name__}); candidate {version} removed"
            + (f"; candidate cleanup failed: {type(cleanup_error).__name__}" if cleanup_error else ""),
            cleanup_error=cleanup_error,
        ) from exc

    keep_dirs = {destination.resolve()}
    if previous is not None:
        keep_dirs.add(previous)
    cleanup_failures = []
    for old in site_root.iterdir():
        if (old.is_dir() and not old.is_symlink() and old.resolve() not in keep_dirs
                and _is_valid_version_name(old.name)):
            try:
                shutil.rmtree(old)
            except Exception as exc:
                cleanup_failures.append(f"{old.name}: {type(exc).__name__}")
    if cleanup_failures:
        raise SnapshotCleanupError(
            f"Snapshot activated as {version}; pruning older snapshots failed: {'; '.join(cleanup_failures)}",
            destination,
        )
    return destination


class HostSnapshotError(RuntimeError):
    def __init__(
        self,
        message: str,
        publish_result: tuple[int, dict[str, str]] | None = None,
        host_error: BaseException | None = None,
    ):
        super().__init__(message)
        self.publish_result = publish_result
        self.host_error = host_error


def publish_ordered(
    site_root: Path,
    public_pages: dict[str, str],
    private_pages: dict[str, str],
    version: str,
    publisher: Callable[[dict[str, str]], tuple[int, dict[str, str]]],
    generated_at: str | None = None,
) -> tuple[int, dict[str, str]]:
    versioned_public = add_snapshot_version(public_pages, version, generated_at=generated_at)
    versioned_private = add_snapshot_version(private_pages, version, generated_at=generated_at)

    validate_publish_allowlist(versioned_public)
    scan_pages(versioned_public)

    host_pages = {
        **versioned_public,
        **versioned_private,
    }
    host_error = None
    try:
        atomic_snapshot_swap(site_root, host_pages, version)
    except Exception as exc:
        host_error = exc

    try:
        publish_result = publisher(versioned_public)
    except BaseException as exc:
        # #315 F3: the publisher's failure must not erase the host outcome.
        # The exception carries it: host_error is the host sink's exception
        # (None when the host snapshot was installed and activated).
        exc.host_error = host_error
        raise
    if host_error is not None:
        raise HostSnapshotError(
            f"ADR-036 host snapshot failed: {type(host_error).__name__}", publish_result, host_error=host_error,
        )
    return publish_result
