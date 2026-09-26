"""ADR-036 D1 helpers for split rendering and snapshot publication."""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping

PUBLIC_PAGES = frozenset({
    "index.html", "lineage.html", "cycles.html", "tokens.html", "lessons.html",
    "agent.html", "hypotheses.html", "about.html", "techtree.html", "cycle.html",
    "cycles-archive-index.json", "lineage-cycle-details.json",
})
# TODO: Switch to scripts/publish_scan.py once merged in master (pF task)
PRIVATE_MARKERS = (
    re.compile(rb"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?i)\b(?:ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_]+"),
    re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(rb"(?i)\bxox[baprs]-[A-Za-z0-9-]+"),
    re.compile(rb"(?i)(?:api[_-]?key|secret|password)\s*[:=]"),
    re.compile(rb'(?i)reasoning_content|"messages"\s*:|"prompt"\s*:\s*\{'),
)

PUBLIC_DATA_KEYS = frozenset({
    "portfolio", "scorecard", "evolution_tree", "hypotheses", "hypotheses_durable",
    "ledger_tail", "ledger_history", "demand_rotation", "demand_completed", "skill_reads",
    "skill_evals", "ci_freshness", "cycle_titles", "cycle_files", "cycle_titles_error",
    "llm_stats", "proposer_stats", "local_ci", "executor_model_status", "executor_llm_stats",
    "compaction", "token_heatmap", "lessons", "subagent_records", "derived_view",
    "reflections", "bridge_exit_streak", "bridge_exits", "strategist_decisions",
    "demand_futility", "systemd_drift", "goal_text", "agents_md", "agent_context",
    "generator_sha", "_newest_source_age_seconds", "_error",
})

PRIVATE_DATA_KEYS = frozenset({
    "cycle_prompts",
})
DEFAULT_BIND_ADDRESS = "0.0.0.0"
DEFAULT_BIND_PORT = 8080


def parse_bind_settings(address: str = DEFAULT_BIND_ADDRESS, port: int = DEFAULT_BIND_PORT) -> tuple[str, int]:
    if not address or not 1 <= int(port) <= 65535:
        raise ValueError("ADR-036 host bind settings require an address and port 1..65535")
    return address, int(port)


class SnapshotHTTPRequestHandler(SimpleHTTPRequestHandler):
    site_root: Path

    def do_GET(self) -> None:
        clean_path = self.path.split("?", 1)[0].split("#", 1)[0]
        if clean_path in {"", "/", "/index.html"}:
            current = self.site_root / "current"
            if current.is_symlink():
                try:
                    target_version = current.resolve().name
                except OSError:
                    target_version = "current"
            else:
                versions = [p.name for p in self.site_root.iterdir() if p.is_dir() and not p.is_symlink()]
                target_version = sorted(versions)[-1] if versions else "current"
            self.send_response(302)
            self.send_header("Location", f"/{target_version}/")
            self.end_headers()
            return
        super().do_GET()


def serve_site(site_root: Path, address: str = DEFAULT_BIND_ADDRESS, port: int = DEFAULT_BIND_PORT) -> None:
    address, port = parse_bind_settings(address, port)
    site_root = site_root.resolve()
    current = site_root / "current"
    if not current.is_dir() and not current.is_symlink():
        raise FileNotFoundError(f"ADR-036 current snapshot unavailable: {current}")

    class Handler(SnapshotHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site_root), **kwargs)

    Handler.site_root = site_root
    ThreadingHTTPServer((address, port), Handler).serve_forever()


def _blank_lines(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines:
        return ""
    return "".join(" " * (len(line) - 1) + "\n" if line.endswith("\n") else " " * len(line) for line in lines)


def _sanitize_public_value(key: str, value: object) -> object:
    if key == "agents_md" and isinstance(value, str):
        return _blank_lines(value)
    if key == "goal_text" and isinstance(value, dict):
        sanitized = dict(value)
        for field in ("charter", "goal_text", "text"):
            if isinstance(sanitized.get(field), str):
                sanitized[field] = _blank_lines(sanitized[field])
        return sanitized
    if key == "agent_context" and isinstance(value, dict):
        ctx = copy.deepcopy(value)
        ctx["prompt_text"] = None
        ctx["task_text"] = None
        for skill in ctx.get("tier2_skills") or []:
            if isinstance(skill, dict):
                skill["content"] = ""
                skill["desc"] = ""
        for mem in ctx.get("tier2_memory", {}).get("files") or []:
            if isinstance(mem, dict):
                mem["content"] = ""
        return ctx
    if key == "subagent_records" and isinstance(value, list):
        records = []
        for rec in value:
            if isinstance(rec, dict):
                r = dict(rec)
                for field in ("task", "summary", "result", "task_excerpt", "summary_excerpt", "result_excerpt"):
                    if field in r:
                        r[f"{field}_chars"] = len(r[field]) if isinstance(r[field], str) else 0
                        r[field] = ""
                records.append(r)
            else:
                records.append(rec)
        return records
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
        return refs
    if key == "strategist_decisions" and isinstance(value, list):
        decs = []
        for rec in value:
            if isinstance(rec, dict):
                r = dict(rec)
                r["decision"] = ""
                r["rationale"] = ""
                decs.append(r)
            else:
                decs.append(rec)
        return decs
    if key == "lessons" and isinstance(value, list):
        les = []
        for rec in value:
            if isinstance(rec, dict):
                r = dict(rec)
                for field in ("problem", "solution", "insight", "result"):
                    if field in r:
                        r[f"{field}_chars"] = len(r[field]) if isinstance(r[field], str) else 0
                        r[field] = ""
                les.append(r)
            else:
                les.append(rec)
        return les
    return value


def split_render_inputs(data: dict) -> tuple[dict, dict]:
    """Allowlist public fields and reduce text-bearing values to metadata."""
    public = {
        key: _sanitize_public_value(key, value)
        for key, value in data.items()
        if key in PUBLIC_DATA_KEYS and key not in PRIVATE_DATA_KEYS
    }
    private = dict(data)
    return public, private


def scan_bytes(payload: bytes, filename: str = "payload") -> None:
    for marker in PRIVATE_MARKERS:
        if marker.search(payload):
            raise ValueError(f"ADR-036 private marker in built publish tree: {filename}")


def scan_pages(pages: Mapping[str, str]) -> None:
    for name, content in pages.items():
        scan_bytes(content.encode("utf-8"), filename=name)


def scan_built_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            scan_bytes(path.read_bytes(), filename=path.name)


def validate_publish_allowlist(pages: Mapping[str, str]) -> None:
    unlisted = sorted(
        name for name in pages
        if name not in PUBLIC_PAGES
        and not re.fullmatch(r"cycles-archive-[0-9]+\.json", name)
    )
    if unlisted:
        raise ValueError(f"ADR-036 unlisted publish paths: {', '.join(unlisted)}")


def add_snapshot_version(pages: dict[str, str], version: str, generated_at: str | None = None) -> dict[str, str]:
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
    site_root.mkdir(parents=True, exist_ok=True)
    destination = site_root / version
    current_link = site_root / "current"
    previous = current_link.resolve() if current_link.is_symlink() else None
    if destination.exists():
        raise FileExistsError(destination)
    staging = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=site_root))
    try:
        for name, contents in pages.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        os.replace(staging, destination)
        link_tmp = site_root / f".current-{version}"
        link_tmp.symlink_to(version, target_is_directory=True)
        try:
            os.replace(link_tmp, current_link)
        except OSError:
            if current_link.is_symlink():
                current_link.unlink()
            os.replace(link_tmp, current_link)
        keep_dirs = {destination.resolve()}
        if previous and previous.is_dir():
            keep_dirs.add(previous)
        for old in site_root.iterdir():
            if old.is_dir() and not old.is_symlink() and old.resolve() not in keep_dirs:
                try:
                    shutil.rmtree(old)
                except OSError:
                    pass
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
) -> tuple[int, dict[str, str]]:
    validate_publish_allowlist(public_pages)
    scan_pages(public_pages)

    host_pages = {
        **add_snapshot_version(public_pages, version),
        **add_snapshot_version(private_pages, version),
    }
    host_error = None
    try:
        atomic_snapshot_swap(site_root, host_pages, version)
    except OSError as exc:
        host_error = exc

    publish_result = publisher(public_pages)
    if host_error is not None:
        raise HostSnapshotError(f"ADR-036 host snapshot failed: {type(host_error).__name__}", publish_result)
    return publish_result
