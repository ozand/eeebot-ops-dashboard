"""ADR-036 D1 helpers for split rendering and snapshot publication."""
from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path
from typing import Callable
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PUBLIC_PAGES = frozenset({
    "index.html", "lineage.html", "cycles.html", "tokens.html", "lessons.html",
    "agent.html", "hypotheses.html", "about.html", "techtree.html", "cycle.html",
    "cycles-archive-index.json", "lineage-cycle-details.json",
})
PRIVATE_INPUT_KEYS = frozenset({"cycle_prompts", "goal_text"})
PRIVATE_MARKERS = (re.compile(rb"sk-[A-Za-z0-9_-]{12,}"), re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"))
DEFAULT_BIND_ADDRESS = "0.0.0.0"
DEFAULT_BIND_PORT = 8080


def parse_bind_settings(address: str = DEFAULT_BIND_ADDRESS, port: int = DEFAULT_BIND_PORT) -> tuple[str, int]:
    if not address or not 1 <= int(port) <= 65535:
        raise ValueError("ADR-036 host bind settings require an address and port 1..65535")
    return address, int(port)


def serve_site(site_root: Path, address: str = DEFAULT_BIND_ADDRESS, port: int = DEFAULT_BIND_PORT) -> None:
    address, port = parse_bind_settings(address, port)
    current = site_root / "current"
    if not current.is_dir():
        raise FileNotFoundError(f"ADR-036 current snapshot unavailable: {current}")
    handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(*args, directory=str(current), **kwargs)
    ThreadingHTTPServer((address, port), handler).serve_forever()


def split_render_inputs(data: dict) -> tuple[dict, dict]:
    """Public renderer gets no call/priority text; private gets the original."""
    return ({key: value for key, value in data.items() if key not in PRIVATE_INPUT_KEYS}, dict(data))


def scan_built_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            payload = path.read_bytes()
            if any(marker.search(payload) for marker in PRIVATE_MARKERS):
                raise ValueError(f"ADR-036 private marker in built publish tree: {path.name}")


def validate_publish_allowlist(pages: dict[str, str]) -> None:
    unlisted = sorted(
        name for name in pages
        if name not in PUBLIC_PAGES
        and not re.fullmatch(r"cycles-archive-[0-9]+\.json", name)
    )
    if unlisted:
        raise ValueError(f"ADR-036 unlisted publish paths: {', '.join(unlisted)}")


def add_snapshot_version(pages: dict[str, str], version: str) -> dict[str, str]:
    tag = f'<meta name="snapshot-version" content="{version}">'
    return {name: text.replace("</head>", tag + "</head>", 1) if name.endswith(".html") else text for name, text in pages.items()}


def render_private_pages(private_data: dict, host: str) -> dict[str, str]:
    """ADR-036 D2 private-only cycle renderer; never included in gh-pages."""
    del host
    from cycle_detail import render_cycle_page

    raw = private_data.get("private_cycle_details")
    known = private_data.get("cycle_details")
    cycle_ids = set(raw) if isinstance(raw, dict) else set()
    if isinstance(known, dict):
        cycle_ids.update(known)
    return {
        f"cycles/{cycle_id}.html": render_cycle_page(
            str(cycle_id), raw.get(cycle_id) if isinstance(raw, dict) else None,
        )
        for cycle_id in sorted(cycle_ids)
    }


def build_private_cycle_pages(private_data: dict, state_root: Path, host: str, *, cycle_ids: set[str] | None = None) -> dict[str, str]:
    """Bind private pages to observed IDs and read their host-local sources."""
    from cycle_detail import load_cycle_detail

    known = set(cycle_ids or ())
    ledger = private_data.get("ledger_tail")
    if isinstance(ledger, list):
        known.update(str(row["cycle_id"]) for row in ledger if isinstance(row, dict) and row.get("cycle_id"))
    if isinstance(private_data.get("cycle_details"), dict):
        known.update(map(str, private_data["cycle_details"]))
    return {
        f"cycles/{cycle_id}.html": render_private_pages(
            {"private_cycle_details": {cycle_id: load_cycle_detail(state_root, cycle_id)}}, host,
        )[f"cycles/{cycle_id}.html"]
        for cycle_id in sorted(known)
    }


def atomic_snapshot_swap(site_root: Path, pages: dict[str, str], version: str) -> Path:
    """Build immutable version dir, then atomically replace current symlink."""
    site_root.mkdir(parents=True, exist_ok=True)
    destination = site_root / version
    if destination.exists():
        raise FileExistsError(destination)
    staging = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=site_root))
    try:
        for name, contents in pages.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        for path in staging.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            path.write_text(text, encoding="utf-8")
        os.replace(staging, destination)
        link_tmp = site_root / f".current-{version}"
        link_tmp.symlink_to(version, target_is_directory=True)
        os.replace(link_tmp, site_root / "current")
        return destination
    except BaseException:
        if staging.exists():
            import shutil
            shutil.rmtree(staging)
        raise


def publish_ordered(site_root: Path, public_pages: dict[str, str], private_pages: dict[str, str], version: str, publisher: Callable[[dict[str, str]], object]) -> None:
    validate_publish_allowlist(public_pages)
    # Scan exactly the publish payload, in a private staging tree, before it
    # can reach the remote sink. Host receives both page sets afterward.
    site_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".gh-pages-scan-", dir=site_root.parent))
    try:
        for name, contents in public_pages.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        scan_built_tree(staging)
    finally:
        import shutil
        shutil.rmtree(staging)
    host_pages = {
        **add_snapshot_version(public_pages, version),
        **add_snapshot_version(private_pages, version),
    }
    atomic_snapshot_swap(site_root, host_pages, version)
    return publisher(public_pages)
