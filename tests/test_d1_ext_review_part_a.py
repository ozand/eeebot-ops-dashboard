"""#315 external review, PART A: snapshot server, snapshot swap, publish_ordered.

Every server test starts the real ``SnapshotHTTPRequestHandler`` on a
ThreadingHTTPServer bound to loopback and sends real HTTP requests.

Design (architect decision): versions are not addressable in the URL and
nothing redirects; each request reads ``current`` once and serves only
inside that target; the file is opened before headers are sent.
"""
from __future__ import annotations

import http.client
import os
import socket
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import scripts.two_sinks as sinks
from scripts.publish_scan import PublicationScanError
from scripts.two_sinks import SnapshotHTTPRequestHandler, atomic_snapshot_swap, publish_ordered


@contextmanager
def served(site_root: Path):
    handler = type("Handler", (SnapshotHTTPRequestHandler,), {"site_root": site_root.resolve()})

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), lambda *a, **kw: handler(*a, directory=str(site_root), **kw))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def fetch(port: int, target: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    """One real request. The target is sent byte-for-byte (a raw socket, so
    http.client's own URL validation cannot mask what the server does)."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode("latin-1"))
        response = http.client.HTTPResponse(sock, method=method)
        response.begin()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, headers, body


def _two_snapshots(root: Path) -> None:
    atomic_snapshot_swap(root, {"index.html": "OLD", "private.txt": "OLD-PRIVATE"}, "v-old")
    atomic_snapshot_swap(root, {"index.html": "ACTIVE", "page.html": "PAGE"}, "v-active")


# --- P1-1: an explicit version is not addressable -------------------------------

def test_p1_1_explicit_versions_are_not_addressable(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _two_snapshots(root)
    with served(root) as port:
        assert fetch(port, "/v-old/private.txt")[0] == 404
        assert fetch(port, "/v-old/index.html")[0] == 404
        assert fetch(port, "/v-active/index.html")[0] == 404
        status, headers, body = fetch(port, "/")
        assert (status, body) == (200, b"ACTIVE") and "location" not in headers
        (root / "current").unlink()
        assert fetch(port, "/")[0] == 503
        assert fetch(port, "/v-active/index.html")[0] in (404, 503)  # never 200 without current


# --- P1-2: no Location is ever built from the decoded path ------------------------

@pytest.mark.parametrize("target", [
    "/current/report%3Fx.html",
    "/current/%252e%252e/v-old/private.txt",
    "/%63urrent/",
    "/./current/",
    "/current",
])
def test_p1_2_current_alias_is_404_and_never_redirects(tmp_path: Path, target: str) -> None:
    root = tmp_path / "site"
    _two_snapshots(root)
    with served(root) as port:
        status, headers, body = fetch(port, target)
        assert status == 404 and "location" not in headers
        assert b"OLD-PRIVATE" not in body


def test_p1_2_crlf_in_path_is_400_and_injects_nothing(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _two_snapshots(root)
    with served(root) as port:
        status, headers, _body = fetch(port, "/current/x%0d%0aInjected:%20yes")
        assert status == 400
        assert "injected" not in headers and "location" not in headers


# --- the server reads current once and serves inside it (F8/C1/F9) ---------------

def test_files_are_served_from_the_current_target_with_fstat_length(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _two_snapshots(root)
    with served(root) as port:
        status, headers, body = fetch(port, "/page.html")
        assert (status, body) == (200, b"PAGE")
        assert headers["content-length"] == str(len(b"PAGE"))


def test_a_symlink_as_last_component_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "site"
    _two_snapshots(root)
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
    (root / "v-active" / "leak.txt").symlink_to(outside)
    with served(root) as port:
        status, _headers, body = fetch(port, "/leak.txt")
        assert status == 404 and b"OUTSIDE-SECRET" not in body
        assert fetch(port, "/page.html")[0] == 200  # the good path still serves


def test_f9_directory_serves_its_index_and_trailing_slash_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "ROOT", "docs/index.html": "DOCS"}, "v1")
    with served(root) as port:
        assert fetch(port, "/docs/")[:3:2] == (200, b"DOCS")
        assert fetch(port, "/docs")[:3:2] == (200, b"DOCS")
        assert fetch(port, "/")[:3:2] == (200, b"ROOT")


@pytest.mark.parametrize("target", ["/a%01b.html", "/index.html%7f", "/x%09y"])
def test_control_characters_in_the_decoded_path_are_400(tmp_path: Path, target: str) -> None:
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "ROOT"}, "v1")
    with served(root) as port:
        assert fetch(port, target)[0] == 400


# --- F10 / F11 --------------------------------------------------------------------

def test_f10_malformed_uri_is_a_400_not_a_dropped_connection(tmp_path: Path) -> None:
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "ROOT"}, "v1")
    with served(root) as port:
        assert fetch(port, "http://[bad/x")[0] == 400  # urlsplit: Invalid IPv6 URL
        assert fetch(port, "/")[0] == 200


def test_f11_head_returns_the_same_headers_as_get(tmp_path: Path) -> None:
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "<html>ROOT</html>"}, "v1")
    with served(root) as port:
        get_status, get_headers, get_body = fetch(port, "/", "GET")
        head_status, head_headers, head_body = fetch(port, "/", "HEAD")
    assert get_status == head_status == 200 and head_body == b""
    for header in ("content-type", "content-length"):
        assert head_headers.get(header) == get_headers.get(header), header
    assert get_headers["content-length"] == str(len(get_body))


# --- F4: one shared validator; the candidate is complete before activation -------

@pytest.mark.parametrize("version", ["current", ".hidden", "..", "v1\r\nX: y"])
def test_f4_reserved_and_invalid_versions_are_refused_by_writer(tmp_path: Path, version: str) -> None:
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "ROOT"}, "v1")
    with pytest.raises(ValueError):
        atomic_snapshot_swap(root, {"index.html": "BAD"}, version)
    with pytest.raises(ValueError):
        sinks.validate_version_name(version)
    with served(root) as port:
        assert fetch(port, "/")[:3:2] == (200, b"ROOT")


@pytest.mark.parametrize("pages", [{}, {"page.html": "no index"}])
def test_f4_incomplete_candidate_is_never_activated(tmp_path: Path, pages: dict) -> None:
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "ROOT"}, "v1")
    with pytest.raises(ValueError):
        atomic_snapshot_swap(root, pages, "v2")
    assert not (root / "v2").exists()
    with served(root) as port:
        assert fetch(port, "/")[:3:2] == (200, b"ROOT")


# --- F5: every created directory is chmod-ed; a chmod error fails the swap -------

def test_f5_every_created_directory_is_chmodded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    real_chmod = os.chmod

    def record(path, mode, *args, **kwargs):
        calls.append((Path(path).name, mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", record)
    atomic_snapshot_swap(tmp_path / "site", {"index.html": "ROOT", "assets/vendor/lib.js": "js"}, "v1")
    assert ("assets", 0o755) in calls and ("vendor", 0o755) in calls


def test_f5_a_chmod_error_fails_the_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "ROOT"}, "v1")
    real_chmod = os.chmod

    def fail_on_assets(path, mode, *args, **kwargs):
        if Path(path).name == "assets":
            raise PermissionError("chmod refused")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", fail_on_assets)
    with pytest.raises(PermissionError):
        atomic_snapshot_swap(root, {"index.html": "NEW", "assets/vendor/lib.js": "js"}, "v2")
    assert not (root / "v2").exists()
    with served(root) as port:
        assert fetch(port, "/")[:3:2] == (200, b"ROOT")


# --- F6: a failed activation removes the candidate --------------------------------

def test_f6_failed_activation_removes_the_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "ROOT"}, "v1")
    real_replace = os.replace

    def refuse_activation(source, destination):
        if Path(destination).name == "current":
            raise OSError("activation refused")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", refuse_activation)
    with pytest.raises(Exception) as info:
        atomic_snapshot_swap(root, {"index.html": "NEW"}, "v2")
    monkeypatch.setattr(os, "replace", real_replace)
    # behavior first: the non-activated candidate is gone, the old snapshot serves
    assert not (root / "v2").exists() and not (root / ".current-v2").exists()
    with served(root) as port:
        assert fetch(port, "/")[:3:2] == (200, b"ROOT")
    assert isinstance(info.value, sinks.SnapshotActivationError) and info.value.activated is False


def test_f6_cleanup_failure_after_activation_is_distinguished(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "ONE"}, "v1")
    atomic_snapshot_swap(root, {"index.html": "TWO"}, "v2")
    real_rmtree = sinks.shutil.rmtree

    def refuse_prune(path, *args, **kwargs):
        if Path(path).name == "v1":
            raise OSError("prune refused")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(sinks.shutil, "rmtree", refuse_prune)
    with pytest.raises(Exception) as info:
        atomic_snapshot_swap(root, {"index.html": "THREE"}, "v3")
    with served(root) as port:
        assert fetch(port, "/")[:3:2] == (200, b"THREE")  # activation DID happen
    # ...and the error says so, distinct from a failed activation
    assert getattr(info.value, "activated", None) is True
    assert isinstance(info.value, sinks.SnapshotCleanupError)


# --- F7: parallel publishers never delete the active snapshot --------------------

def test_f7_parallel_publishers_never_delete_the_active_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "site"
    atomic_snapshot_swap(root, {"index.html": "BASE"}, "v0")
    first_at_activation = threading.Event()
    release_first = threading.Event()
    real_symlink_to = Path.symlink_to

    def pausing_symlink_to(self, target, target_is_directory=False):
        if str(target) == "v1":
            first_at_activation.set()
            release_first.wait(timeout=10)
        return real_symlink_to(self, target, target_is_directory=target_is_directory)

    monkeypatch.setattr(Path, "symlink_to", pausing_symlink_to)
    errors = []

    def publish(version: str, body: str) -> None:
        try:
            atomic_snapshot_swap(root, {"index.html": body}, version)
        except Exception as exc:  # recorded, asserted below
            errors.append(exc)

    first = threading.Thread(target=publish, args=("v1", "ONE"))
    first.start()
    assert first_at_activation.wait(timeout=10)
    second = threading.Thread(target=publish, args=("v2", "TWO"))
    second.start()
    second.join(timeout=2)  # without the lock it completes here and prunes v1
    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not errors, errors
    with served(root) as port:
        assert fetch(port, "/")[0] == 200, "current must point at a complete snapshot"
    assert (root / "current").resolve().is_dir()


# --- F3: the publisher's exception keeps the host outcome ------------------------

def test_f3_publisher_exception_carries_host_success(tmp_path: Path) -> None:
    def failing_publisher(_pages):
        raise RuntimeError("gh-pages down")

    with pytest.raises(RuntimeError) as info:
        publish_ordered(tmp_path / "site", {"index.html": "ROOT"}, {}, "v1", failing_publisher)
    assert info.value.host_error is None  # host snapshot installed and activated
    with served(tmp_path / "site") as port:
        assert fetch(port, "/")[0] == 200


def test_f3_publisher_exception_carries_host_failure(tmp_path: Path) -> None:
    bad_root = tmp_path / "not-a-dir"
    bad_root.write_text("file", encoding="utf-8")

    def failing_publisher(_pages):
        raise RuntimeError("gh-pages down")

    with pytest.raises(RuntimeError) as info:
        publish_ordered(bad_root, {"index.html": "ROOT"}, {}, "v1", failing_publisher)
    assert info.value.host_error is not None


# --- C2: the built-tree scan fails closed on non-regular entries -----------------

def test_c2_symlink_in_built_tree_is_refused(tmp_path: Path) -> None:
    tree = tmp_path / "built"
    tree.mkdir()
    (tree / "index.html").write_text("clean", encoding="utf-8")
    (tmp_path / "elsewhere.txt").write_text("also clean", encoding="utf-8")
    (tree / "link.html").symlink_to(tmp_path / "elsewhere.txt")
    with pytest.raises(PublicationScanError, match="non-regular"):
        sinks.scan_built_tree(tree)
