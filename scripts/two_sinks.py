"""ADR-036 D1 helpers for split rendering and snapshot publication."""
from __future__ import annotations

import argparse
import copy
import json
import os
import posixpath
import re
import shutil
import tempfile
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


class SnapshotHTTPRequestHandler(SimpleHTTPRequestHandler):
    site_root: Path

    def list_directory(self, path: str | os.PathLike) -> Any:
        self.send_error(404, "Directory listing disabled")
        return None

    def _canonical_target(self) -> tuple[str, str] | None:
        raw = self.path
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc or raw.startswith("//"):
            return None
        try:
            decoded = unquote(parts.path, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return None
        if "\x00" in decoded or "\\" in decoded or "#" in raw:
            return None
        if not parts.path.startswith("/"):
            return None
        return posixpath.normpath("/" + decoded.lstrip("/")), ("?" + parts.query if parts.query else "")

    def _redirect(self, path: str, query: str, method: str = "GET") -> bool:
        if path not in {"/", "/index.html", "/current"} and not path.startswith("/current/"):
            return False
        current = self.site_root / "current"
        try:
            target = current.resolve(strict=True)
        except OSError:
            self.send_error(503, "Current snapshot unavailable")
            return True
        if (not current.is_symlink() or target.parent != self.site_root.resolve()
                or not _VERSION_RE.fullmatch(target.name) or target.name.startswith(".")
                or not _complete_snapshot(target)):
            self.send_error(503, "Current snapshot unavailable")
            return True
        version = target.name
        if not _VERSION_RE.fullmatch(version):
            self.send_error(503, "Current snapshot unavailable")
            return True
        rest = path[len("/current/"):] if path.startswith("/current/") else ""
        location = f"/{version}/{rest}" if rest else f"/{version}/index.html"
        self.send_response(302)
        self.send_header("Location", location + query)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _serve_snapshot(self, method: str) -> None:
        request = self._canonical_target()
        if request is None:
            self.send_error(400, "Invalid request target")
            return
        path, query = request
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/") + "/index.html"
        if path in {"/", "/index.html", "/current"} or path.startswith("/current/"):
            self._redirect(path, query, method)
            return
        target = self._validate_existing_snapshot(path)
        if target is None:
            return
        if method == "HEAD":
            self.send_response(200)
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(target.stat().st_size))
            self.send_header("Content-Type", self.guess_type(str(target)))
            self.end_headers()
            with target.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile)

    def do_GET(self) -> None:
        self._serve_snapshot("GET")

    def do_HEAD(self) -> None:
        self._serve_snapshot("HEAD")

    def send_response(self, code: int, message: str | None = None) -> None:
        super().send_response(code, message)
        self._last_response_code = code

    def _validate_existing_snapshot(self, path: str) -> Path | None:
        relative = Path(path.lstrip("/"))
        if (not relative.parts or not _VERSION_RE.fullmatch(relative.parts[0])
                or relative.parts[0].startswith(".")):
            self.send_error(404)
            return None
        if path.endswith("/"):
            relative = Path(relative.parts[0], *relative.parts[1:], "index.html")
        root = self.site_root.resolve()
        version_root = root / relative.parts[0]
        parts = relative.parts[1:]
        target = version_root.joinpath(*parts)
        if (not _complete_snapshot(version_root)
                or any((root / Path(*relative.parts[:index])).is_symlink()
                       for index in range(1, len(relative.parts) + 1))
                or not target.resolve().is_relative_to(version_root.resolve())):
            self.send_error(404)
            return None
        if not target.resolve().is_relative_to(version_root.resolve()) or not target.is_file():
            self.send_error(404)
            return None
        return target


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
    current = site_root / "current"
    try:
        target = current.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError(f"ADR-036 current snapshot unavailable: {current}") from exc
    if (not current.is_symlink() or target.parent != site_root
            or not _VERSION_RE.fullmatch(target.name) or target.name.startswith(".")
            or not _complete_snapshot(target)):
        raise FileNotFoundError(f"ADR-036 current snapshot unavailable: {current}")

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


def _drop_source_counters(value: object, generated: set[str] | None = None) -> object:
    if isinstance(value, dict):
        return {
            key: _drop_source_counters(item, generated)
            for key, item in value.items()
            if not (isinstance(key, str) and key.endswith(_COUNTER_SUFFIXES)
                    and (generated is None or key not in generated))
        }
    if isinstance(value, list):
        return [_drop_source_counters(item, generated) for item in value]
    return value


def _drop_invalid_counters(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _drop_invalid_counters(item)
            for key, item in value.items()
            if not (isinstance(key, str) and key.endswith(_COUNTER_SUFFIXES)
                    and (isinstance(item, bool) or not isinstance(item, int) or item < 0))
        }
    if isinstance(value, list):
        return [_drop_invalid_counters(item) for item in value]
    return value


def _sanitize_public_value(key: str, value: object, withheld: dict[str, int] | None = None) -> object:
    if key in {"cycle_titles_error", "probe_error"}:
        if value:
            if withheld is not None:
                withheld["probe_error"] = withheld.get("probe_error", 0) + 1
            return _reason_bucket(str(value))
        return ""
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
                "state": state if state in allowed else "unknown",
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
        for skill in ctx.get("tier2_skills") or []:
            if isinstance(skill, dict):
                for field in ("content", "desc"):
                    if isinstance(skill.get(field), str):
                        skill[f"{field}_chars"] = len(skill[field])
                        skill[field] = ""
        fit = ctx.get("prompt_fit")
        if isinstance(fit, dict):
            fit.pop("reason", None)
            fit.pop("summary", None)
        for mem in ctx.get("tier2_memory", {}).get("files") or []:
            if isinstance(mem, dict) and isinstance(mem.get("content"), str):
                mem["content_chars"] = len(mem["content"])
                mem["content"] = ""
        return _drop_invalid_counters(ctx)
    if key == "bridge_exit_streak" and isinstance(value, dict):
        result = {k: value[k] for k in ("consecutive_failures", "last_ts", "count")
                  if k in value and isinstance(value[k], int) and not isinstance(value[k], bool) and value[k] >= 0}
        if value.get("last_error"):
            result["last_error"] = "bridge error withheld"
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
                item["error"] = "bridge error withheld"
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
                    r["refused"] = (reason in {"refused", "declined"} if isinstance(reason, str)
                                     else False)
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
            code = value.get("exit_code")
            l_proj["summary"] = "passed" if code == 0 else (f"failed (exit {code})" if code is not None else "failed")
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
    if status in {"absent", "probe_unavailable", "present"}:
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
    public = {
        key: _sanitize_public_value(key, value, withheld)
        for key, value in data.items()
        if key in PUBLIC_DATA_KEYS and key not in PRIVATE_DATA_KEYS
    }
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
    if raw_goal is None:
        public["goal_meta"] = {
            "state": "absent",
            "present": False,
            "lines": None,
            "chars": None,
            "priority_count": None,
        }
    elif not isinstance(raw_goal, dict):
        public["goal_meta"] = {
            "state": "unexpected_shape",
            "present": False,
            "lines": None,
            "chars": None,
            "priority_count": None,
        }
    else:
        # goal_text.json is operator-private. Public metadata comes only from
        # the repository goals.md charter consumed by the release pipeline.
        g_text = ""
        p_list = raw_goal.get("priorities")
        priority_count = len(p_list) if isinstance(p_list, list) else None
        public["goal_meta"] = {
            "state": "present",
            "present": True,
            "lines": None,
            "chars": None,
            "priority_count": priority_count,
        }

    return public, private


def _safe_component(value: object) -> str:
    return str(value).replace("\\", "/")


def scan_pages(pages: Mapping[str, str]) -> None:
    """Validate pages against leak scanner (delegates to scripts/publish_scan.py)."""
    _publish_scan_pages(dict(pages))


def scan_built_tree(root: Path) -> None:
    """Scan all files on disk under root using scripts/publish_scan.py."""
    root = root.resolve()
    pages = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8", errors="replace")
        for path in root.rglob("*")
        if path.is_file()
    }
    _publish_scan_pages(pages)


def validate_publish_allowlist(pages: Mapping[str, str]) -> None:
    for name in pages:
        _validate_page_name(name)
        if not is_allowed_publish_path(name):
            raise PublicationScanError(f"ADR-036 unlisted publish path: {name}")


def add_snapshot_version(pages: dict[str, str], version: str, generated_at: str | None = None) -> dict[str, str]:
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid snapshot version: {version!r}")
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


def atomic_snapshot_swap(site_root: Path, pages: dict[str, str], version: str) -> Path:
    """Build immutable version dir, then atomically replace current symlink."""
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid snapshot version: {version!r}")
    for name in pages:
        _validate_page_name(name)
    site_root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(site_root, 0o755)
    except OSError:
        pass
    destination = site_root / version
    current_link = site_root / "current"
    previous = current_link.resolve() if current_link.is_symlink() else None
    if destination.exists():
        raise FileExistsError(destination)
    staging = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=site_root.parent)).resolve()
    if staging.parent != site_root.parent.resolve():
        shutil.rmtree(staging, ignore_errors=True)
        raise OSError("snapshot staging must be a sibling of site_root for atomic rename")
    try:
        try:
            os.chmod(staging, 0o755)
        except OSError:
            pass
        for name, contents in pages.items():
            _validate_page_name(name)
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(target.parent, 0o755)
            except OSError:
                pass
            target.write_text(contents, encoding="utf-8")
            try:
                os.chmod(target, 0o644)
            except OSError:
                pass
        os.replace(staging, destination)
        link_tmp = site_root / f".current-{version}"
        link_tmp.symlink_to(version, target_is_directory=True)
        try:
            os.replace(link_tmp, current_link)
        except OSError:
            if os.name == "nt":
                if current_link.is_symlink():
                    current_link.unlink()
                os.replace(link_tmp, current_link)
            else:
                raise
        keep_dirs = {destination.resolve()}
        if previous and previous.is_dir():
            keep_dirs.add(previous)
        cleanup_failures = []
        for old in site_root.iterdir():
            if old.is_dir() and not old.is_symlink() and old.resolve() not in keep_dirs and _VERSION_RE.fullmatch(old.name):
                try:
                    shutil.rmtree(old)
                except Exception as exc:
                    cleanup_failures.append(f"{old.name}: {exc}")
        if cleanup_failures:
            raise HostSnapshotError(f"Snapshot cleanup failed: {'; '.join(cleanup_failures)}")
        return destination
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


class HostSnapshotError(RuntimeError):
    def __init__(self, message: str, publish_result: tuple[int, dict[str, str]] | None = None):
        super().__init__(message)
        self.publish_result = publish_result


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

    publish_result = publisher(versioned_public)
    if host_error is not None:
        raise HostSnapshotError(f"ADR-036 host snapshot failed: {type(host_error).__name__}", publish_result)
    return publish_result
