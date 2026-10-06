"""ADR-036 D1 helpers for split rendering and snapshot publication."""
from __future__ import annotations

import argparse
import copy
import datetime
import html
import json
import math
import os
import re
import shutil
import stat
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urlsplit

try:
    from scripts.publish_scan import (
        PUBLIC_PAGE_PATHS as PUBLIC_PAGES,
        PublicationScanError,
        is_allowed_publish_path,
        scan_pages as _publish_scan_pages,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.publish_scan"}:
        raise
    from publish_scan import (
        PUBLIC_PAGE_PATHS as PUBLIC_PAGES,
        PublicationScanError,
        is_allowed_publish_path,
        scan_pages as _publish_scan_pages,
    )  # type: ignore

# #378: the CI vocabulary comes from its writer (techtree_viewer), so the
# public enum cannot drift from what the reader emits.
try:
    from scripts.techtree_viewer import (
        CI_ACTIONS_ENABLED_UNKNOWN, CI_ACTIONS_STATES, CI_FRESHNESS_STATES, CI_LATEST_CONCLUSIONS,
        COMPACTION_STATES, LESSON_SOURCES, MODEL_CLASSES, PROBE_STATES, VIEW_STATES,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.techtree_viewer"}:
        raise
    from techtree_viewer import (  # type: ignore
        CI_ACTIONS_ENABLED_UNKNOWN, CI_ACTIONS_STATES, CI_FRESHNESS_STATES, CI_LATEST_CONCLUSIONS,
        COMPACTION_STATES, LESSON_SOURCES, MODEL_CLASSES, PROBE_STATES, VIEW_STATES,
    )

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


# --- #356 D1.1: the typed public projection ------------------------------------
#
# Every public value is CONSTRUCTED here from named fields; nothing is copied
# from a source record. A node is ``(value, withheld) -> projected | _DROP``:
# a wrong type, a value outside the field's domain, an unknown key and a
# non-dict row all yield _DROP, and the field (or row) is left out. Nothing
# returned shares a mutable object with the input (every dict and list is
# built fresh; scalars are immutable).

_DROP = object()
_Node = Callable[[object, "dict[str, int] | None"], object]

#: ISO-8601 date or date-time (the loop's writers use ...Z and +00:00);
#: the components are range-checked by _ts (#378 review P2).
_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(?:Z|[+-](\d{2}):?(\d{2}))?)?$")
#: the largest epoch a timestamp may carry (9999-12-31T23:59:59Z).
_MAX_EPOCH = 253402300799
#: identifiers: cycle ids, shas, repositories, branches, relative paths.
_IDENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:/@+-]{0,199}$")
#: single-word codes and names (no "/", no whitespace).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:+-]{0,63}$")
#: a file path without whitespace; _relpath narrows it to repo-relative.
_PATH_RE = re.compile(r"^[A-Za-z0-9._/@+-]{1,300}$")
#: #378 review P1-2: the only absolute paths the page shows are systemd
#: unit files (systemd_drift findings) -- a fixed, non-private domain.
_UNIT_PATH_RE = re.compile(
    r"^/(?:etc|lib|usr/lib|run)/systemd/(?:system|user)/[A-Za-z0-9@._:-]{1,128}(?:\.d/[A-Za-z0-9@._:-]{1,128})?$")

#: Bumped whenever the projection's output domain changes; part of the
#: publish digest (techtree_autopublish.compute_tree_digest), so a deployed
#: projection change republishes even when the source tree is quiet.
PROJECTION_VERSION = "d1.1-typed-2"


def _count_withheld(withheld: dict[str, int] | None, category: str) -> None:
    if withheld is not None:
        withheld[category] = withheld.get(category, 0) + 1


def _text(max_len: int = 1000) -> _Node:
    def node(value: object, _w: dict[str, int] | None) -> object:
        return value if isinstance(value, str) and len(value) <= max_len else _DROP
    return node


def _pattern(regex: re.Pattern[str]) -> _Node:
    def node(value: object, _w: dict[str, int] | None) -> object:
        return value if isinstance(value, str) and regex.fullmatch(value) else _DROP
    return node


def _no_traversal(value: str) -> bool:
    return "//" not in value and ".." not in value.split("/")


def _ident(value: object, _w: dict[str, int] | None) -> object:
    return value if isinstance(value, str) and _IDENT_RE.fullmatch(value) and _no_traversal(value) else _DROP


_token = _pattern(_TOKEN_RE)


def _relpath(value: object, _w: dict[str, int] | None) -> object:
    """#378 review P1-2: a REPO-RELATIVE path only -- no leading "/", no
    "..", no "//" (a drive letter or backslash never matches _PATH_RE);
    anything else is omitted."""
    if isinstance(value, str) and _PATH_RE.fullmatch(value) and not value.startswith("/") and _no_traversal(value):
        return value
    return _DROP


def _unit_path(value: object, _w: dict[str, int] | None) -> object:
    return value if isinstance(value, str) and _UNIT_PATH_RE.fullmatch(value) and _no_traversal(value) else _DROP


def _ts(value: object, _w: dict[str, int] | None) -> object:
    """A validated timestamp: an ISO-8601 string whose date, time and
    offset are real (#378 review P2: 2026-02-30 and +25:00 are not), or a
    non-negative epoch no later than year 9999. An int is never converted
    to float (10**400 would raise OverflowError)."""
    if isinstance(value, str):
        match = _TS_RE.fullmatch(value)
        if match is None:
            return _DROP
        year, month, day, hour, minute, second, off_h, off_m = (
            int(part) if part is not None else 0 for part in match.groups())
        try:
            datetime.datetime(year, month, day, hour, minute, second)
        except ValueError:
            return _DROP
        return value if off_h <= 23 and off_m <= 59 else _DROP
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 0 <= value <= _MAX_EPOCH else _DROP
    if isinstance(value, float) and math.isfinite(value) and 0 <= value <= _MAX_EPOCH:
        return value
    return _DROP


def _bool(value: object, _w: dict[str, int] | None) -> object:
    return value if value is True or value is False else _DROP


#: #378 review B-F5: the range every published int must lie in. A larger
#: int cannot be formatted (str(10**5000) raises ValueError, so would the
#: page and json.dumps) -- it is dropped before anything formats it.
_INT_BOUND = 2 ** 63 - 1


def _int(minimum: int | None = 0) -> _Node:
    def node(value: object, _w: dict[str, int] | None) -> object:
        if (isinstance(value, int) and not isinstance(value, bool) and -_INT_BOUND <= value <= _INT_BOUND
                and (minimum is None or value >= minimum)):
            return value
        return _DROP
    return node


def _num(minimum: float | None = None) -> _Node:
    def node(value: object, _w: dict[str, int] | None) -> object:
        # #378 review P2: an int is checked as an int -- math.isfinite(10**400)
        # raises OverflowError, which would wipe the whole section.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _DROP
        if isinstance(value, float) and not math.isfinite(value):
            return _DROP
        if isinstance(value, int) and not -_INT_BOUND <= value <= _INT_BOUND:
            return _DROP
        return value if minimum is None or value >= minimum else _DROP
    return node


_count = _int(0)
_number = _num()


def _enum(*values: str, fallback: str | None = None) -> _Node:
    allowed = frozenset(values)

    def node(value: object, _w: dict[str, int] | None) -> object:
        if isinstance(value, str) and value in allowed:
            return value
        # a present, invalid value reads as the fallback; null is not a value
        return fallback if fallback is not None and value is not None else _DROP
    return node


def _code(category: str) -> _Node:
    """A reason code from _PUBLIC_REASON_CODES, else its size bucket."""
    def node(value: object, withheld: dict[str, int] | None) -> object:
        return _DROP if value is None else _project_reason(value, withheld, category)
    return node


def _flag(label: str) -> _Node:
    """Only the PRESENCE of a private diagnostic is public."""
    def node(value: object, _w: dict[str, int] | None) -> object:
        return label if value else ""
    return node


def _describe(node: _Node, keys: "Iterable[str]" = (), children: "Iterable[_Node]" = ()) -> _Node:
    """#378 (structural test): record the FIELD NAMES a node can publish and
    its child nodes, so tests can compare the renderer's reads with the
    schema (public_schema_fields). No effect on projection."""
    node.public_keys = frozenset(keys)  # type: ignore[attr-defined]
    node.children = tuple(children)  # type: ignore[attr-defined]
    return node


def _produces(*keys: str) -> Callable[[Callable], Callable]:
    """Declare the fields a ``post`` hook adds to its record."""
    def mark(post: Callable) -> Callable:
        post.public_keys = frozenset(keys)  # type: ignore[attr-defined]
        return post
    return mark


def _nullable(inner: _Node) -> _Node:
    """#378 review P2: null is published ONLY where a field is declared
    nullable (the producer writes None there); every other node drops it."""
    def node(value: object, withheld: dict[str, int] | None) -> object:
        return None if value is None else inner(value, withheld)
    return _describe(node, children=(inner,))


def _violation_code(value: object, _w: dict[str, int] | None) -> object:
    """#378 review P1-1: a gate violation is published as its rule code
    from the finite reason allowlist (a bare code, or the ``code:`` prefix
    of the text), else the fixed "violation". The text stays private."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in _PUBLIC_REASON_CODES:
            return stripped
        prefix = re.match(r"^([a-z0-9_.-]{1,64})\s*:", stripped)
        if prefix and prefix.group(1) in _PUBLIC_REASON_CODES:
            return prefix.group(1)
    return "violation"


def _one_of(*nodes: _Node) -> _Node:
    def node(value: object, withheld: dict[str, int] | None) -> object:
        for candidate in nodes:
            projected = candidate(value, withheld)
            if projected is not _DROP:
                return projected
        return _DROP
    return _describe(node, children=nodes)


def _list(item: _Node, max_items: int | None = None) -> _Node:
    def node(value: object, withheld: dict[str, int] | None) -> object:
        if not isinstance(value, list):
            return _DROP
        rows = []
        for entry in value if max_items is None else value[:max_items]:
            projected = item(entry, withheld)
            if projected is not _DROP:
                rows.append(projected)
        return rows
    return _describe(node, children=(item,))


def _map(key: _Node, item: _Node) -> _Node:
    """A dict keyed by DATA (cycle ids, repositories, dates). #378 review
    P1-3: a key is published only when its validator returns it UNCHANGED
    -- a validator that maps a key (an enum fallback, a code bucket) drops
    it instead, so the output key is always the validated source key and
    two source keys can never collide. A null value needs a nullable item."""
    def node(value: object, withheld: dict[str, int] | None) -> object:
        if not isinstance(value, dict):
            return _DROP
        result = {}
        for name, entry in value.items():
            if not isinstance(name, str) or key(name, withheld) != name:
                continue
            projected = item(entry, withheld)
            if projected is not _DROP:
                result[name] = projected
        return result
    return _describe(node, children=(item,))  # map keys are data, not field names


def _obj(
    fields: Mapping[str, _Node],
    *,
    sized_text: Mapping[str, object] | None = None,
    sized_list: tuple[str, ...] = (),
    post: Callable[[dict, dict, "dict[str, int] | None"], None] | None = None,
) -> _Node:
    """A record with NAMED fields only. ``sized_text`` fields are private
    text: published as ``<field>_chars`` (only when the source is a string)
    plus a fresh copy of the given blank value. ``sized_list`` fields, when
    the source is a list, become ``<field>_count`` and ``[]``; anything
    else is dropped. A null value needs a nullable field (#378 review P2)."""
    def node(value: object, withheld: dict[str, int] | None) -> object:
        if not isinstance(value, dict):
            return _DROP
        out: dict[str, Any] = {}
        for name, field in fields.items():
            if name not in value:
                continue
            projected = field(value[name], withheld)
            if projected is not _DROP:
                out[name] = projected
        for name, blank in (sized_text or {}).items():
            if name in value:
                text = value[name]
                saved_chars = value.get(f"{name}_chars")
                if isinstance(text, str) and text:
                    out[f"{name}_chars"] = len(text)
                elif _is_count(saved_chars):
                    # An already-redacted empty string carries no new size
                    # information; retain only the validated saved counter.
                    out[f"{name}_chars"] = saved_chars
                elif isinstance(text, str):
                    out[f"{name}_chars"] = 0
                out[name] = copy.deepcopy(blank)  # never shared between records
        for name in sized_list:
            if isinstance(value.get(name), list):
                out[f"{name}_count"] = len(value[name])
                out[name] = []
        if post is not None:
            post(value, out, withheld)
        return out
    keys = {*fields, *(sized_text or {}), *(f"{name}_chars" for name in (sized_text or {})),
            *sized_list, *(f"{name}_count" for name in sized_list), *getattr(post, "public_keys", ())}
    return _describe(node, keys, fields.values())


# -- enums (#356 R3) -------------------------------------------------------------

#: the writer's vocabulary (techtree_viewer CI_*), plus the values of the
#: bare {repo: {"state": ...}} map D1 accepted.
_CI_CONCLUSIONS = tuple(dict.fromkeys((
    *CI_LATEST_CONCLUSIONS, "in_progress", "queued", "unanswerable", "absent", "unknown",
)))
_CI_STATES = tuple(dict.fromkeys((
    *CI_FRESHNESS_STATES, *CI_ACTIONS_STATES,
    "success", "failure", "cancelled", "skipped", "in_progress", "queued",
    "unanswerable", "absent", "unknown", "fresh", "stale", "pending", "disabled",
)))
#: #378: error_card_recording skip reasons, as eeebot bridge.py writes them
#: (7238 worktree_add_failed, 7260 write_failed, 7300 push_rejected,
#: 7302 diff_touched_more_than_errors_yaml, 7312 exception:<class name>).
_ERROR_CARD_SKIP_REASONS = ("worktree_add_failed", "write_failed", "push_rejected",
                            "diff_touched_more_than_errors_yaml")
_PROBE_STATES = PROBE_STATES
_VIEW_STATES = VIEW_STATES
#: #378: SNAPSHOTS of eeebot writers (another repo, cannot be imported) at
#: eeebot 6d476b71; tests/test_d11_enum_writers.py pins each one with its
#: file:line, so dropping a writer value from the projection turns it red.
#: eeebot nanobot/runtime/local_ci.py:41 (default "ran"), :109 "targets_missing"
_LOCAL_CI_STATES = ("ran", "targets_missing")
#: eeebot nanobot/crash_record.py:262-263 (unit_timeout; completion|failed
#: by outcome), nanobot/runtime/bridge.py:5306-5310 (loop_breaker_abort,
#: wall_clock_abort, progress_watchdog_abort), :7012-7014 (completion|failed).
_RUN_END_CLASSIFICATIONS = ("completion", "failed", "unit_timeout", "loop_breaker_abort", "wall_clock_abort",
                            "progress_watchdog_abort")
#: eeebot nanobot/crash_record.py:323 / :443-445 (record_exit and run_end rows).
_RUN_OUTCOMES = ("success", "failure", "interrupted")
_BRIDGE_CLASSIFICATIONS = (
    *_RUN_END_CLASSIFICATIONS,
    "unit_timeout", "killed", "loop_breaker_abort", "wall_clock_abort", "progress_watchdog_abort",
    "success", "failure", "failed", "error", "crash", "clean", "completed", "timeout", "timed_out",
    "signal", "paused-supplier", "paused_supplier", "supplier_failure", "unknown", "other",
)
_BRIDGE_OUTCOMES = tuple(sorted(_PUBLIC_REASON_CODES | set(_RUN_OUTCOMES) | {
    "success", "failure", "failed", "error", "crash", "completed", "timeout", "killed", "unknown", "other",
    "unit_timeout", "loop_breaker_abort", "wall_clock_abort", "progress_watchdog_abort",
}))
#: systemd $EXIT_STATUS signal names, as eeebot nanobot/crash_record.py:434-445
#: records them (INTERRUPTED_EXIT_STATUSES :73-80 also lists SIG-prefixed forms).
_EXIT_SIGNALS = ("TERM", "KILL", "INT", "HUP", "ABRT", "SEGV", "PIPE", "QUIT", "BUS", "FPE", "ILL",
                 "ALRM", "USR1", "USR2", "XCPU", "XFSZ", "SIGTERM", "SIGINT", "SIGKILL")
#: eeebot nanobot/runtime/cycle_ledger.py:90-93 VALID_OUTCOMES, :355-357
#: VALID_DIARY_OPEN_OUTCOMES, :404-407 VALID_PLANNING_OUTCOMES, plus the
#: literal outcomes other ledger writers record (rg "outcome=" at 6d476b71).
_EEEBOT_LEDGER_OUTCOMES = (
    "success", "partial", "failed", "skipped-duplicate", "promotion_candidate", "push_pending", "pushed_late",
    "superseded", "abandoned", "paused-supplier",
    "integrated", "refused", "malformed", "push_failed", "commit_failed",
    "no_plan", "spawn_failed", "timed_out", "rest", "rest_unchanged", "rejected_duplicate",
    "write_failed", "unchanged", "skipped_supplier_paused", "pass", "miss", "hit", "failure", "completed",
    "blocked", "inconclusive",
)
_LEDGER_OUTCOMES = tuple(sorted(_PUBLIC_REASON_CODES | set(_EEEBOT_LEDGER_OUTCOMES) | {
    "integrated", "success", "succeeded", "ok", "pass", "passed", "fail", "failed", "partial",
    "skipped", "push_pending", "pushed_late", "superseded", "abandoned", "paused-supplier",
    "paused_supplier", "model_call_incomplete", "duplicate", "rejected", "idle", "created",
    "already_recorded", "not_created", "write_failed", "unknown",
}))
_PROVENANCE_OPERATOR = "operator"
_PROVENANCE_SELF_DERIVED = "self-derived"
_DERIVED_VIEW_SORT = "provenance(operator<self-derived), then vector(V1<V2), as demand._priority_items"

_exit_status = _one_of(_int(None), _enum(*_EXIT_SIGNALS))


# -- records ---------------------------------------------------------------------

_LEDGER_ROW = _obj({
    "cycle_id": _ident, "phase": _token, "ts": _ts, "outcome": _enum(*_LEDGER_OUTCOMES, fallback="unknown"),
    "status": _enum(*_LEDGER_OUTCOMES, fallback="unknown"), "sha": _ident, "parent_sha": _ident,
    "task_title": _text(300), "target_path": _relpath, "serves": _ident, "demand_id": _ident, "branch": _ident,
    "reason": _code("ledger_reason"), "decision": _enum(*_PUBLIC_LEDGER_DECISIONS, fallback="[withheld]"),
    "passed": _bool, "smoke_passed": _bool, "duplicate": _bool, "push_attempts": _count,
    "delivered": _bool, "delivery_state": _token, "delta": _number, "metric_delta": _number,
    "files_changed": _list(_relpath, 50), "lessons_context": _list(_ident, 50),
    "violations": _list(_violation_code, 50), "card_commit": _ident, "card_id": _ident,
    "skip_reason": _one_of(_pattern(re.compile(r"^exception:[A-Za-z_][A-Za-z0-9_]{0,63}$")),
                           _enum(*_PUBLIC_REASON_CODES, *_ERROR_CARD_SKIP_REASONS, fallback="[withheld]")),
    "error": _code("ledger_error"), "attempt": _pattern(re.compile(r"^\d{1,3}(?:/\d{1,3})?$")),
    "_ledger_source": _pattern(re.compile(r"^(?:live|archive:\d{4}-\d{2}-\d{2})$")),
    "ledger_blind": _bool, "doc_budget_exceeded": _bool, "doc_only_deferred": _count,
    "doc_only_integrations_24h": _count, "doc_only_budget_24h": _count, "items_considered": _count,
    "iterations_used": _nullable(_count),
})


@_produces("_v2_lesson")
def _lesson_post(source: dict, out: dict, _w: dict[str, int] | None) -> None:
    out["_v2_lesson"] = bool(source.get("problem"))


_LESSON_ROW = _obj({
    "id": _ident, "title": _text(300), "date": _ts, "cycle_id": _ident, "task_id": _ident,
    "source": _enum(*LESSON_SOURCES), "severity": _token, "kind": _token,
    "tags": _one_of(_list(_token, 20), _token), "seen_count": _count,
    "problem_chars": _count, "solution_chars": _count, "insight_chars": _count, "result_chars": _count,
    "hypothesis_chars": _count,
}, sized_text={"problem": "", "solution": "", "insight": "", "result": "", "hypothesis": ""},
    post=_lesson_post)  # #378 review B-F4: hypothesis is model output -- LAN only

_SUBAGENT_ROW = _obj({
    "subagent_id": _ident, "cycle_id": _ident, "goal_id": _ident, "label": _text(200), "status": _token,
    "started_at": _ts, "finished_at": _nullable(_ts), "task_truncated": _bool, "summary_truncated": _bool,
    "result_truncated": _bool, "task_bytes": _count, "iteration_count": _count,
}, sized_text={name: "" for name in (
    "task", "summary", "result", "task_excerpt", "summary_excerpt", "result_excerpt")})

_REFLECTION_ROW = _obj({"cycle_id": _ident, "ts": _ts, "summary_chars": _count,
                        "findings_count": _count, "recommendations_count": _count,
                        "transcript_coverage": _num(0), "partial_view": _bool,
                        "input_fit": _obj({"status": _token, "transcript": _obj({
                            "chars": _count, "recorder_truncated_chars": _count, "dropped_chars": _count})})},
                       sized_text={"summary": ""}, sized_list=("findings", "recommendations"))


@_produces("decision", "refused", "reason", "rationale")
def _strategist_post(source: dict, out: dict, withheld: dict[str, int] | None) -> None:
    if source.get("decision") is not None:
        out["decision"] = _project_reason(source["decision"], withheld, "strategist_decision")
    reason = source.get("reason")
    if reason is not None:
        out["refused"] = (isinstance(reason, str)
                          and (reason in {"refused", "declined"} or reason.startswith(("refused:", "declined:"))))
        out["reason"] = "refused" if out["refused"] else _project_reason(reason, withheld, "strategist_reason")
    out["rationale"] = ""


_STRATEGIST_ROW = _obj({
    "inputs_status": _map(_token, _obj({"status": _token})),
    "counts": _obj({"hypotheses_appended": _count, "advisories_written": _count}),
    "success": _bool, "timestamp": _ts, "ts": _ts, "cycle_id": _ident,
}, post=_strategist_post)

def _diagnostic_flags(**labels: str) -> Callable[[dict, dict, "dict[str, int] | None"], None]:
    """Only the PRESENCE of a private diagnostic field is published, as a
    fixed label, and only when it is set."""
    def post(source: dict, out: dict, _w: dict[str, int] | None) -> None:
        for field, label in labels.items():
            if source.get(field):
                out[field] = label
    return _produces(*labels)(post)


_BRIDGE_EXIT_ROW = _obj({
    "ts": _ts, "cycle_id": _ident, "exit_code": _int(None), "exit_status": _exit_status,
    "classification": _enum(*_BRIDGE_CLASSIFICATIONS, fallback="other"),
    "outcome": _enum(*_BRIDGE_OUTCOMES, fallback="other"),
}, post=_diagnostic_flags(error="error", where="withheld"))

_BRIDGE_RUN_ROW = _obj({
    "run_id": _ident, "cycle_id": _ident, "phase": _token, "started_at": _ts, "finished_at": _nullable(_ts),
    "classification": _enum(*_BRIDGE_CLASSIFICATIONS, fallback="other"),
    "outcome": _enum(*_BRIDGE_OUTCOMES, fallback="other"), "exit_status": _exit_status,
    "error": _flag("error"), "last_where": _flag("withheld"), "reason": _code("bridge_run_reason"),
})

_BRIDGE_ACTIVE_RUN = _obj({
    "run_id": _ident, "cycle_id": _ident, "started_at": _ts, "finished_at": _nullable(_ts),
    "classification": _enum(*_BRIDGE_CLASSIFICATIONS, fallback="other"),
    "outcome": _enum(*_BRIDGE_OUTCOMES, fallback="other"), "exit_status": _exit_status,
}, post=_diagnostic_flags(error="bridge error withheld"))

_BRIDGE_EXIT_STREAK = _obj({"consecutive_failures": _count, "last_ts": _ts, "count": _count},
                           post=_diagnostic_flags(last_error="error"))

_CI_REPO_ROW = _obj({
    "state": _enum(*_CI_STATES, fallback="unknown"),
    "freshness_state": _enum(*_CI_STATES, fallback="unknown"),
    "latest_conclusion": _nullable(_enum(*_CI_CONCLUSIONS, fallback="unknown")),
    "observed_at_utc": _ts,
    "actions_enabled": _one_of(_bool, _enum(*CI_ACTIONS_ENABLED_UNKNOWN)),
    "actions": _obj({"state": _enum(*_CI_STATES, fallback="unknown"),
                     "enabled": _one_of(_bool, _enum(*CI_ACTIONS_ENABLED_UNKNOWN)),
                     "observed_at_utc": _ts}),
    "freshness": _obj({"state": _enum(*_CI_STATES, fallback="unknown"),
                       "latest_conclusion": _nullable(_enum(*_CI_CONCLUSIONS, fallback="unknown")),
                       "observed_at_utc": _ts, "latest_completed_at_utc": _nullable(_ts),
                       "latest_run_id": _nullable(_count), "latest_run_number": _nullable(_count),
                       "age_seconds": _nullable(_num(0)),
                       "pending_count": _count, "run_count_returned": _count}),
})


def _project_ci_freshness(value: object, withheld: dict[str, int] | None) -> object:
    """``read_ci_freshness`` shape ``{schema_version, observed_at_utc,
    repositories: {repo: row}}``; a bare ``{repo: row}`` map (the D1 shape)
    is projected as the repository map itself. Every row has a ``state``."""
    if not isinstance(value, dict):
        return _DROP
    repos = _map(_ident, _CI_REPO_ROW)

    def with_state(rows: object) -> object:
        if rows is _DROP:
            return rows
        for row in rows.values():
            row.setdefault("state", "unknown")
        return rows

    if isinstance(value.get("repositories"), dict):
        rows = with_state(repos(value["repositories"], withheld))
        if rows is _DROP:
            return _DROP
        result = {"repositories": rows}
        for name, node in (("schema_version", _count), ("observed_at_utc", _ts)):
            projected = node(value[name], withheld) if name in value else _DROP
            if projected is not _DROP:
                result[name] = projected
        return result
    return with_state(repos(value, withheld))


_describe(_project_ci_freshness, ("repositories", "schema_version", "observed_at_utc", "state"), (_CI_REPO_ROW,))


def _priority_label(row: dict, provenance: object, raw_label: object) -> str | None:
    """#356 R5: ONE provenance policy for derived_priorities and
    priority_items. An operator priority is published as its number only
    (its wording is private goal_text); a label is public only under an
    EXPLICIT self-derived provenance (#378 review B-F1: absent or any other
    provenance withholds it). ``row`` is the VALIDATED row, so its number is
    already a bounded int (B-F5)."""
    if provenance == _PROVENANCE_OPERATOR:
        number = row.get("number")
        return f"Priority #{number}" if _is_count(number) else "Operator priority"
    if provenance == _PROVENANCE_SELF_DERIVED:
        label = _text(300)(raw_label, None)
        return None if label is _DROP else label
    return None


#: #378 review B-F2: fields published only under an explicit self-derived
#: provenance (they are the loop's own wording, never the operator's).
_SELF_DERIVED_ONLY = {"direction": _text(120), "id": _ident}


def _priority_row(fields: Mapping[str, _Node]) -> _Node:
    base = _obj(fields)

    def node(value: object, withheld: dict[str, int] | None) -> object:
        row = base(value, withheld)
        if row is _DROP:
            return row
        provenance = row.get("provenance")  # validated; absent is unknown (B-F1)
        label = _priority_label(row, provenance, value.get("label"))
        if label is not None:
            row["label"] = label
        if provenance == _PROVENANCE_SELF_DERIVED:
            for name, field in _SELF_DERIVED_ONLY.items():
                projected = field(value[name], withheld) if name in value else _DROP
                if projected is not _DROP:
                    row[name] = projected
        elif value.get("label"):
            _count_withheld(withheld, "priority_label")
        if isinstance(value.get("evidence"), str) and value["evidence"]:
            _count_withheld(withheld, "priority_evidence")
        return row
    return _describe(node, {*base.public_keys, "label", *_SELF_DERIVED_ONLY}, (base, *_SELF_DERIVED_ONLY.values()))


_DERIVED_PRIORITY = _priority_row({
    "number": _nullable(_int(None)), "vector": _token, "added_utc": _ts,
    "provenance": _enum(_PROVENANCE_OPERATOR, _PROVENANCE_SELF_DERIVED),
})

_PRIORITY_ITEM = _priority_row({
    "rank": _count, "kind": _token, "vector": _token, "number": _nullable(_int(None)),
    "provenance": _enum(_PROVENANCE_OPERATOR, _PROVENANCE_SELF_DERIVED), "state": _token,
})


@_produces("reason", "charter", "source", "merged", "text")
def _derived_view_post(source: dict, out: dict, withheld: dict[str, int] | None) -> None:
    status = out.get("status")
    if status in _VIEW_STATES:
        out["reason"] = {"absent": "derived view absent", "probe_unavailable": "derived view unavailable",
                         "present": "derived view present"}[status]
    charter = source.get("charter")
    if isinstance(charter, dict):
        projected = {}
        for name, field in (("source", _token), ("merged", _bool)):
            result = field(charter[name], withheld) if name in charter else _DROP
            if result is not _DROP:
                projected[name] = result
        text = _text(50000)(charter.get("text"), withheld)
        # #378 review B-F3: the text is public ONLY as the release goals.md
        # charter, untouched. The writer (eeebot demand._charter_as_loop_sees_it)
        # tags "release_goals_md" only on text resolve_charter(RELEASE_ROOT)
        # read, and build_derived_view always writes merged=False; anything
        # else -- merged, untagged, goal_text -- fails closed.
        if projected.get("source") == "release_goals_md" and projected.get("merged") is False and text is not _DROP:
            projected["text"] = text
        elif isinstance(charter.get("text"), str) and charter["text"]:
            _count_withheld(withheld, "derived_charter_text")
        out["charter"] = projected


_DERIVED_VIEW = _obj({
    "status": _enum(*_VIEW_STATES), "derived_status": _enum(*_VIEW_STATES),
    "schema_version": _one_of(_count, _token), "generated_at_utc": _ts, "sort": _enum(_DERIVED_VIEW_SORT),
    "derived_priorities": _list(_DERIVED_PRIORITY), "priority_items": _list(_PRIORITY_ITEM),
}, post=_derived_view_post)


@_produces("summary")
def _local_ci_post(source: dict, out: dict, _w: dict[str, int] | None) -> None:
    state, probe = out.get("state"), out.get("probe")
    if state == "targets_missing":
        out["summary"] = "targets missing"
    elif state == "ran":
        # #315 R2: only the VALIDATED exit code (an int, never a bool --
        # False == 0 would read "passed") may name the result.
        code = out.get("exit_code")
        out["summary"] = "result unavailable" if code is None else "passed" if code == 0 else f"failed (exit {code})"
    elif probe == "absent":
        out["summary"] = "local CI status absent"
    elif probe == "probe_unavailable":
        out["summary"] = "local CI status unavailable"


_LOCAL_CI = _obj({
    "probe": _enum(*_PROBE_STATES), "state": _enum(*_LOCAL_CI_STATES), "ts_utc": _ts, "created_at_utc": _ts,
    "exit_code": _int(None), "targets_checked": _count, "ok": _bool,
}, post=_local_ci_post)

_EXECUTOR_MODEL_STATUS = _obj({
    "probe": _enum(*_PROBE_STATES), "reason": _code("executor_model_reason"), "latest_model": _ident,
    "latest_class": _enum(*MODEL_CLASSES), "fallback_seen_recent": _bool,
    "checked_calls": _count,
})

_EXECUTOR_LLM_STATS = _obj({"cycle_id": _ident, "prompt_tokens": _nullable(_count), "ts": _ts,
                            "context_window": _nullable(_count)})

_COMPACTION = _obj({
    "status": _enum(*COMPACTION_STATES),
    "rows": _list(_obj({"cycle_id": _ident, "reason": _token, "ts": _ts})),
})

_SKILL_READS = _obj({"reads": _list(_obj({"skill": _ident, "confirmed": _bool, "cycle_id": _ident, "ts": _ts}))})
_SKILL_EVALS = _list(_obj({"skill": _ident, "delta": _number, "cycle_id": _ident, "ts": _ts}))

_NAMED_ENTRY = _one_of(_token, _obj({"name": _ident, "file": _ident, "chars": _count}))
_DROP_SUMMARY = _obj({"status": _token, "count": _count, "chars": _count, "sections": _list(_token)})

_PROMPT_FIT = _obj({
    "source_status": _token, "reader_status": _token, "window_kind": _token, "window_days": _num(0),
    "window_rows": _count, "rows_considered": _count, "rows_with_drops": _count, "rows_with_trims": _count,
    "prompt_covered_from": _ts, "prompt_covered_to": _ts,
    "latest": _obj({"ts": _ts, "rung": _token, "dropped": _DROP_SUMMARY, "trimmed": _DROP_SUMMARY}),
})

_SYSTEM_PROMPT = _obj({
    "cycle_id": _ident, "ts": _ts, "phase": _token, "chars": _count, "cap": _count, "over_by": _number,
    "overflow": _bool, "rung": _token, "sections": _map(_token, _count),
    "missing": _list(_NAMED_ENTRY), "truncated": _list(_NAMED_ENTRY), "dropped": _list(_NAMED_ENTRY),
    "release_pool_chars": _obj({"limit": _count, "used": _count}),
    "release_pool": _obj({"cap": _count, "used": _count}), "operating_reserve_chars": _count,
    "rule_owners": _map(_ident, _token),
    "skills_catalogue": _obj({"omitted_names": _list(_ident), "truncated": _bool, "budget": _count,
                              "retained_count": _count, "total_count": _count}),
    "memory_index": _obj({"resident_missing": _list(_ident), "status": _token,
                          "resident_matched": _one_of(_count, _list(_ident))}),
})


def _project_system_prompt(value: object, withheld: dict[str, int] | None) -> object:
    """Project whole system_prompt records and reject partial sections maps."""
    if not isinstance(value, dict):
        return _DROP
    sections = value.get("sections", _DROP)
    if sections is not _DROP and sections is not None and (
        not isinstance(sections, dict)
        or any(not isinstance(name, str) or not name.strip() or not _is_count(count)
               for name, count in sections.items())
    ):
        if withheld is not None:
            withheld["projection_shape"] = withheld.get("projection_shape", 0) + 1
        value = {name: item for name, item in value.items() if name != "sections"}
    return _SYSTEM_PROMPT(value, withheld)


_project_system_prompt.public_keys = _SYSTEM_PROMPT.public_keys  # type: ignore[attr-defined]
_project_system_prompt.children = (_SYSTEM_PROMPT,)  # type: ignore[attr-defined]


_TIER2_FILE = _obj({"name": _ident, "size_bytes": _count, "content_chars": _count}, sized_text={"content": ""})


@_produces("prompt_text", "task_text", "prompt_text_chars", "task_text_chars")
def _agent_context_post(source: dict, out: dict, _w: dict[str, int] | None) -> None:
    # #356 R4: the prompt and task are private whatever their type; only a
    # string's size is published.
    for field in ("prompt_text", "task_text"):
        if field in source:
            saved_chars = source.get(f"{field}_chars")
            if isinstance(source[field], str) and source[field]:
                out[f"{field}_chars"] = len(source[field])
            elif _is_count(saved_chars):
                out[f"{field}_chars"] = saved_chars
            elif isinstance(source[field], str):
                out[f"{field}_chars"] = 0
            out[field] = None


_AGENT_CONTEXT = _obj({
    "prompt_text_chars": _count, "task_text_chars": _count,
    "system_prompt": _project_system_prompt,
    "truncation_streak": _obj({"status": _token, "total_rows": _count, "entries": _list(_obj({
        "kind": _enum("truncated", "dropped"), "name": _ident, "streak": _count}))}),
    "tier2_skills": _list(_obj({"name": _ident, "size_bytes": _count, "content_chars": _count, "desc_chars": _count},
                               sized_text={"content": "", "desc": ""})),
    "tier2_skills_status": _token,
    "tier2_lessons": _obj({"index_status": _token, "corpus_status": _token, "corpus_count": _count,
                           "total_size_bytes": _count, "files": _list(_TIER2_FILE)}),
    "tier2_memory": _obj({"index_status": _token, "corpus_status": _token, "total_files": _count,
                          "total_size_bytes": _count, "files": _list(_TIER2_FILE)}),
    "skill_reads": _SKILL_READS, "skill_evals": _SKILL_EVALS, "executor_llm_stats": _EXECUTOR_LLM_STATS,
    "compaction": _COMPACTION,
    "window_pressure": _obj({"status": _token, "rows_in_window": _count, "known_rows": _count,
                             "unknown_rows": _count, "p99_pct": _nullable(_num(0)),
                             "threshold_pct": _nullable(_num(0))}),
    "prompt_fit": _PROMPT_FIT,
}, post=_agent_context_post)

#: #378 review P1-4: every scorecard metric is a NAMED field with its type
#: (the fields the public renderer reads); a ratio is null when its
#: denominator is zero.
_RATIO = _nullable(_number)
_SCORECARD_UNAVAILABLE = "unavailable"
_FAILURE_CAUSES = ("execution_failure", "model_unavailable", "model_call_incomplete",
                   "unknown_failure_cause", "self_dedup")
_LOOP_METRICS = _obj({
    "integrations": _count, "confirmed_integration_ratio": _RATIO, "repeat_failure_rate": _RATIO,
    "repeat_failure_rate_new": _RATIO, "hypothesis_selection_rate": _RATIO, "hypothesis_served_cycles": _count,
    "paused_supplier_outcomes": _one_of(_count, _enum(_SCORECARD_UNAVAILABLE)),  # scorecard.py:890
    "paused_supplier_seconds": _one_of(_num(0), _enum(_SCORECARD_UNAVAILABLE)),  # scorecard.py:891
    # eeebot nanobot/runtime/scorecard.py:852-867: "unavailable" when the
    # ledger is (or, for model_call_incomplete/unknown_failure_cause, always)
    **{f"{cause}_{kind}": _one_of(_count, _enum(_SCORECARD_UNAVAILABLE))
       for cause in _FAILURE_CAUSES for kind in ("events", "tasks")},
    **{f"{cause}_share": _one_of(_RATIO, _enum(_SCORECARD_UNAVAILABLE)) for cause in _FAILURE_CAUSES},
})
_COST_METRICS = _obj({"tokens_per_integration": _nullable(_num(0))})
_HELDOUT_METRICS = _obj({"passed": _count, "checked": _count})
_TARGET_METRICS = _obj({name: _number for name in (
    "integrations", "confirmed_integration_ratio", "repeat_failure_rate", "repeat_failure_rate_new",
    "tokens_per_integration", "heldout")})
_HYPOTHESIS_LOOP_METRICS = _obj({
    **{name: _count for name in (
        "total", "active", "answered", "supported", "refuted", "inconclusive", "inconclusive_within_window",
        "inconclusive_aged", "inconclusive_undatable", "inconclusive_undatable_no_qualifying_artifact",
        "inconclusive_undatable_no_completion", "inconclusive_undatable_invalid_timestamp")},
    "inconclusive_split_status": _token,
})
_ARTIFACT_COUNTS = _obj({"artifacts": _count, "components": _count, "leaves": _count})
_QUANTILES = _obj({name: _one_of(_number, _list(_number, 64)) for name in ("gateway", "local", "total")})

_DAY_KEY = _pattern(re.compile(r"^\d{4}-\d{2}-\d{2}$"))
_CALL_STATS = _obj({"calls": _count, "total_tokens": _num(0), "duration_ms": _num(0)})
_HEATMAP_CELL = _list(_one_of(_number, _token), 8)

_TOKEN_HEATMAP = _obj({
    "dates": _list(_DAY_KEY),
    "hourly": _map(_DAY_KEY, _nullable(_list(_HEATMAP_CELL, 24))),  # null: no data that day
    "five_min": _map(_DAY_KEY, _nullable(_map(_pattern(re.compile(r"^\d{1,3}$")), _HEATMAP_CELL))),
    "summary": _obj({
        **{name: _num(0) for name in (
            "total_tokens", "total_calls", "self_hosted_tokens", "vendor_tokens", "other_tokens",
            "local_tokens", "gateway_tokens", "days_span", "days_present", "days_missing")},
        "timezone": _ident, "source_timezone": _ident, "timezone_offset_hours": _number,
        "quantiles_hourly": _QUANTILES, "quantiles_5min": _QUANTILES,
    }),
})

_FEED_ROW = _obj({"status": _token, "age_seconds": _num(0), "max_age_seconds": _num(0)})

_SCORECARD = _obj({
    "computed_at_utc": _ts, "window_days": _num(0), "gaps_status": _token,
    "loop": _LOOP_METRICS, "cost": _COST_METRICS, "targets": _TARGET_METRICS, "heldout": _HELDOUT_METRICS,
    "reader_status": _obj({
        **{reader: _obj({"status": _token}) for reader in ("ledger", "completed", "heldout", "history")},
        "feeds": _map(_ident, _FEED_ROW)}),
    "feeds": _obj({"feeds": _map(_ident, _FEED_ROW)}),
    "quality": _obj({"artifact_graph": _obj({
        "status": _token, "counts": _ARTIFACT_COUNTS, "unit_scan_status": _token,
        "oldest_leaves": _list(_obj({"path": _relpath}), 20)})}),
    "control_plane": _obj({"hypothesis_loop": _HYPOTHESIS_LOOP_METRICS}),
    "gaps": _list(_obj({"metric": _ident})),
    "prompt_fit": _PROMPT_FIT,
})

_HYPOTHESIS_ENTRY = _obj({
    "status": _token, "verdict": _nullable(_one_of(_bool, _token)), "answered_at": _nullable(_ts), "first_seen": _ts,
    "last_touched": _ts, "title": _text(300),
    # #378 review B-F4: the writers (hypothesis_backlog) record a cycle id here
    "answered_evidence": _ident,
})

#: #378 review B-F4: a durable hypothesis's texts are model output -- LAN
#: only; the public page gets its title and each text's size.
_DURABLE_TEXTS = ("hypothesis", "action", "insight_criterion", "success_criterion")
_DURABLE_ENTRY = _obj({
    "hypothesis_id": _ident, "id": _ident, "title": _text(300), "selection_status": _token,
    "wsjf": _one_of(_number, _obj({"score": _number})),
    "hadi": _obj({"hypothesis_chars": _count, "action_chars": _count},
                 sized_text={"hypothesis": "", "action": ""}),
    **{f"{name}_chars": _count for name in _DURABLE_TEXTS},
    "created_at": _ts, "created_ts": _ts,
}, sized_text={name: "" for name in _DURABLE_TEXTS})

_FUTILITY_GAP = _obj({"attempt_count": _count, "threshold": _num(0), "attempt_unit": _token,
                      "surface": _list(_relpath), "metric": _ident})


def _project_demand_futility(value: object, withheld: dict[str, int] | None) -> object:
    if not isinstance(value, dict):
        return _DROP
    if isinstance(value.get("gaps"), dict):
        gaps = _map(_ident, _FUTILITY_GAP)(value["gaps"], withheld)
        return _DROP if gaps is _DROP else {"gaps": gaps}
    return _map(_ident, _FUTILITY_GAP)(value, withheld)


_describe(_project_demand_futility, ("gaps",), (_FUTILITY_GAP,))

_SYSTEMD_DRIFT = _obj({
    "status": _enum(*_VIEW_STATES), "reason": _code("systemd_drift_reason"), "details": _code("systemd_drift_details"),
    "state": _token, "defect_count": _count, "scanned_at": _ts,
    "findings": _obj({
        "installed_not_in_release": _list(_obj({"owner": _token, "path": _unit_path})),
        "release_not_installed": _list(_unit_path),
        "content_differs": _list(_obj({"path": _unit_path, "detail": _code("systemd_drift_detail")})),
        "stray": _list(_unit_path),
    }),
})

#: #356: the typed projection of every public key. A key's node returns the
#: fresh public value, or _DROP when the whole section has the wrong shape
#: (the section then gets _PUBLIC_EMPTY's value).
_PUBLIC_SCHEMA: dict[str, _Node] = {
    "portfolio": _obj({"current": _nullable(_ident), "nodes": _map(_ident, _obj({
        "status": _token, "lever_metric": _ident, "direction": _token, "last_lever_value": _number}))}),
    "scorecard": _SCORECARD,
    "evolution_tree": _obj({"current_sha": _ident, "nodes": _map(_ident, _obj({
        "ts": _ts, "cycle_id": _ident, "parent_sha": _nullable(_ident), "branch": _ident,
        "fitness": _obj({"reward": _number}), "outcome": _enum(*_LEDGER_OUTCOMES, fallback="unknown")}))}),
    "hypotheses": _obj({"entries": _map(_ident, _HYPOTHESIS_ENTRY)}),
    "hypotheses_durable": _obj({
        "entries": _one_of(_list(_DURABLE_ENTRY), _map(_ident, _DURABLE_ENTRY)),
        "model": _ident, "schema": _one_of(_count, _ident), "selected_hypothesis_id": _ident}),
    "ledger_tail": _list(_LEDGER_ROW),
    "ledger_history": _list(_LEDGER_ROW),
    "demand_rotation": _obj({"served": _map(_ident, _ts)}),
    "demand_completed": _obj({"entries": _map(_ident, _obj({
        "cycle_id": _ident, "files_changed": _list(_relpath, 50), "confirmed": _bool}))}),
    "skill_reads": _SKILL_READS,
    "skill_evals": _SKILL_EVALS,
    "ci_freshness": _project_ci_freshness,
    "cycle_titles": _map(_ident, _text(300)),
    "cycle_files": _map(_ident, _list(_relpath, 50)),
    "llm_stats": _map(_ident, _obj({
        "calls": _count, "planner_calls": _count, "total_tokens": _num(0), "duration_ms": _num(0), "last_finish_reason": _nullable(_token),
        "any_length": _bool, "last_ts": _ts})),
    "proposer_stats": _obj({
        "calls": _count, "total_tokens": _num(0), "duration_ms": _num(0), "last_model": _ident, "last_ts": _ts,
        "llm_unavailable": _bool, "days": _map(_DAY_KEY, _CALL_STATS)}),
    "local_ci": _LOCAL_CI,
    "executor_model_status": _EXECUTOR_MODEL_STATUS,
    "executor_llm_stats": _EXECUTOR_LLM_STATS,
    "compaction": _COMPACTION,
    "token_heatmap": _TOKEN_HEATMAP,
    "lessons": _list(_LESSON_ROW),
    "subagent_records": _list(_SUBAGENT_ROW),
    "derived_view": _DERIVED_VIEW,
    "reflections": _list(_REFLECTION_ROW),
    "bridge_exit_streak": _BRIDGE_EXIT_STREAK,
    "bridge_exits": _list(_BRIDGE_EXIT_ROW),
    "bridge_runs": _list(_BRIDGE_RUN_ROW),
    "bridge_active_run": _BRIDGE_ACTIVE_RUN,
    "strategist_decisions": _list(_STRATEGIST_ROW),
    "demand_futility": _project_demand_futility,
    "systemd_drift": _SYSTEMD_DRIFT,
    "agent_context": _AGENT_CONTEXT,
    "generator_sha": _one_of(_ident, _enum("")),
    "_newest_source_age_seconds": _num(0),
}

#: #378 review P2: the sections a producer may leave None (the empty-state
#: defaults of techtree_viewer.fetch_remote_state / read_json of a missing
#: file). Any other section given None gets its _PUBLIC_EMPTY value.
_NULLABLE_SECTIONS = frozenset({
    "portfolio", "scorecard", "evolution_tree", "hypotheses", "hypotheses_durable", "ledger_tail",
    "demand_rotation", "demand_completed", "skill_reads", "proposer_stats", "executor_llm_stats", "compaction",
    "local_ci", "executor_model_status", "token_heatmap", "bridge_exit_streak", "bridge_exits", "bridge_runs",
    "bridge_active_run", "strategist_decisions", "demand_futility", "systemd_drift", "ci_freshness",
    "cycle_titles", "agent_context", "_newest_source_age_seconds",
})

#: the value a section gets when its source has the wrong shape. A list
#: section reads as empty; a dict section as "no data" ({} or None, as the
#: renderer's own absent state expects).
_PUBLIC_EMPTY: dict[str, object] = {
    **{key: [] for key in ("ledger_tail", "ledger_history", "skill_evals", "lessons", "subagent_records",
                           "reflections", "bridge_exits", "bridge_runs", "strategist_decisions")},
    "bridge_exit_streak": {},
    **{key: {} for key in ("llm_stats", "cycle_files", "derived_view")},
    "generator_sha": "",
}


#: fields of the two metadata sections split_render_inputs builds itself
_META_FIELDS = frozenset({"present", "lines", "chars", "count", "priority_count", "state"})


def public_schema_fields() -> frozenset[str]:
    """#378: every FIELD NAME the public projection can publish, at any
    depth (map keys are data and are not included). Used by the structural
    renderer-reads test (tests/test_d11_renderer_reads.py)."""
    names: set[str] = set(PUBLIC_DATA_KEYS) | _META_FIELDS
    seen: set[int] = set()
    stack: list[object] = list(_PUBLIC_SCHEMA.values())
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        names |= getattr(node, "public_keys", frozenset())
        stack.extend(getattr(node, "children", ()))
    return frozenset(names)


def _sanitize_public_value(key: str, value: object, withheld: dict[str, int] | None = None) -> object:
    """#356: the typed projection of ONE public key (fresh objects only)."""
    if key in {"agents_meta", "goal_meta"}:
        return _project_meta(key, value)
    if key == "_error":
        # #315 R1: the transport/state-read error text can embed host paths
        # or stderr; only its presence is public.
        return "state_read_failed" if value else None
    if key in {"cycle_titles_error", "probe_error"}:
        if value:
            _count_withheld(withheld, "probe_error")
            return "probe_unavailable"
        return "absent"
    node = _PUBLIC_SCHEMA.get(key)
    if node is None:
        return None
    if value is None and key in _NULLABLE_SECTIONS:
        return None
    projected = _DROP if value is None else node(value, withheld)
    if projected is _DROP:
        _count_withheld(withheld, "projection_shape")
        return copy.deepcopy(_PUBLIC_EMPTY.get(key))
    return projected


def split_render_inputs(data: dict) -> tuple[dict, dict]:
    """Allowlist public fields and replace private text with derived metadata."""
    withheld: dict[str, int] = {}
    if not isinstance(data, dict):
        # #378 review P2: a non-dict input is "no data", never a crash.
        withheld["input_shape"] = 1
        data = {}
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


def render_private_pages(
    private_data: dict, host: str, state_root: Path | None = None,
) -> dict[str, str]:
    """Render bounded D2 cycle details for the host snapshot only.

    The private renderer reads the host-authority state tree directly; its
    output is passed only to ``publish_ordered``'s host snapshot sink.
    """
    del host
    if state_root is None or not isinstance(private_data, dict):
        return {}
    ledger = private_data.get("ledger_history")
    if not isinstance(ledger, list):
        return {}
    cycle_ids = {
        row.get("cycle_id") for row in ledger
        if isinstance(row, dict) and isinstance(row.get("cycle_id"), str)
        and row.get("cycle_id")
    }
    if not cycle_ids:
        return {}
    try:
        from scripts.cycle_detail import build_cycle_index, render_cycle_page
    except ImportError:
        from cycle_detail import build_cycle_index, render_cycle_page
    index = build_cycle_index(Path(state_root), days=7)
    pages = {}
    for cycle_id in sorted(cycle_ids & index.keys()):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", cycle_id):
            continue
        page_name = f"cycles/{cycle_id}.html"
        _validate_page_name(page_name)
        record = index[cycle_id]
        details = private_data.get("cycle_details")
        if isinstance(details, dict) and isinstance(details.get(cycle_id), dict):
            reflection = details[cycle_id].get("reflection")
            if isinstance(reflection, dict):
                record = {**record, "reflection": reflection}
        pages[page_name] = render_cycle_page(cycle_id, record)
    return pages


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


def _add_private_cycle_navigation(host_pages: dict[str, str], private_pages: Mapping[str, str]) -> None:
    """Route the host cycle-detail query to an indexed LAN-only page."""
    cycle_ids = sorted(
        name[len("cycles/"):-len(".html")]
        for name in private_pages
        if name.startswith("cycles/") and name.endswith(".html")
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name[len("cycles/"):-len(".html")])
    )
    cycle_page = host_pages.get("cycle.html")
    if not isinstance(cycle_page, str):
        return
    allowed_json = json.dumps(cycle_ids, ensure_ascii=True).replace("</", "<\\/")
    router = f'''<script id="lan-private-cycle-router">
(function() {{
  var allowed = new Set({allowed_json});
  var id = new URLSearchParams(window.location.search).get('id') || '';
  if (allowed.has(id)) {{
    window.location.replace('cycles/' + encodeURIComponent(id) + '.html');
  }} else {{
    document.body.innerHTML = '<main><h1>Cycle detail unavailable</h1><p class="unavailable-note">No retained LAN-only detail page exists for this cycle.</p></main>';
  }}
}})();
</script>'''
    marker = "</body>"
    host_pages["cycle.html"] = cycle_page.replace(marker, router + marker, 1) if marker in cycle_page else cycle_page + router


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
    host_warnings: list[str] | None = None,
    host_outcome: dict[str, str] | None = None,
) -> tuple[int, dict[str, str]]:
    """#356: a host error whose ``activated`` is true (SnapshotCleanupError:
    ``current`` already serves the new snapshot, only pruning older ones
    failed) is NOT a host failure. It is appended to ``host_warnings`` (when
    given) and the call returns the publisher's result as on success.

    The host sink is NOT CONFIGURED while ``site_root`` itself does not exist
    (ENOENT on the root -- the D4 tmpfiles step creates it): the snapshot is
    skipped, never created here, and it is not a host failure. A root that
    exists but cannot be written, or any other error in the swap, still is.
    ``host_outcome["status"]`` (when given) is ``host_sink_unconfigured``,
    ``host_sink_active`` or ``host_snapshot_failed``."""
    versioned_public = add_snapshot_version(public_pages, version, generated_at=generated_at)
    versioned_private = add_snapshot_version(private_pages, version, generated_at=generated_at)

    validate_publish_allowlist(versioned_public)
    scan_pages(versioned_public)

    host_pages = {
        **versioned_public,
        **versioned_private,
    }
    _add_private_cycle_navigation(host_pages, versioned_private)
    host_error = None
    try:
        os.lstat(site_root)
        configured = True
    except FileNotFoundError:
        configured = False
    except OSError:
        configured = True  # present but unreadable: the swap reports the failure
    if configured:
        try:
            atomic_snapshot_swap(site_root, host_pages, version)
        except Exception as exc:
            host_error = exc
        if host_error is not None and getattr(host_error, "activated", False) is True:
            warning = f"ADR-036 host snapshot cleanup warning: {host_error}"
            if host_warnings is not None:
                host_warnings.append(warning)
            else:
                print(f"two_sinks: WARNING: {warning}", file=sys.stderr)
            host_error = None
    if host_outcome is not None:
        host_outcome["status"] = (
            "host_sink_unconfigured" if not configured
            else "host_snapshot_failed" if host_error is not None else "host_sink_active")

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
