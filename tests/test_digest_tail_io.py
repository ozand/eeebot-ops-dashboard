"""#418: byte equivalence, bounded buffers and fail-soft ledger digest I/O.

Only synthetic temporary ledgers are read; no host or publisher is invoked.
"""
from __future__ import annotations

import builtins
import hashlib
import io
import os
from pathlib import Path

import pytest

from scripts import techtree_autopublish as ap

GENERATOR = 'd' * 40


def _reference(root: Path, payload: bytes | None) -> str:
    """Independent pre-change digest, retaining binary readlines semantics."""
    hasher = hashlib.sha256()
    for name in ap.TREE_DIGEST_SOURCES:
        try:
            hasher.update((root / name).read_bytes())
        except OSError:
            hasher.update(b'<missing>')
        hasher.update(b'\x00')
    if payload is None:
        hasher.update(b'<ledger-missing>')
    else:
        hasher.update(b''.join(io.BytesIO(payload).readlines()[-50:]))
    hasher.update(b'\x00')
    lessons = root.parent / 'eeebot-self-evolving' / 'lessons' / 'lessons.yaml'
    try:
        hasher.update(lessons.read_bytes()[-4096:])
    except OSError:
        hasher.update(b'<lessons-missing>')
    hasher.update(b'\x00')
    hasher.update(f'projection:{ap.sinks.PROJECTION_VERSION}'.encode('utf-8'))
    hasher.update(b'\x00')
    hasher.update(f'generator:{GENERATOR}'.encode('utf-8'))
    hasher.update(b'\x00')
    return hasher.hexdigest()


def _ledger(tmp_path: Path, payload: bytes) -> tuple[Path, Path]:
    root = tmp_path / 'state'
    path = root / ap.LEDGER_DIGEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return root, path


class TrackedLedger(io.BytesIO):
    def __init__(self, payload: bytes):
        super().__init__(payload)
        self.bytes_read = 0
        self.requests: list[int] = []
        self.readlines_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        data = super().read(size)
        self.bytes_read += len(data)
        return data

    def readlines(self, hint: int = -1) -> list[bytes]:
        self.readlines_calls += 1
        lines = super().readlines(hint)
        self.bytes_read += sum(map(len, lines))
        return lines


def _replace_ledger(monkeypatch, path: Path, stream: io.BytesIO) -> None:
    original_open = builtins.open

    def open_file(file, mode='r', *args, **kwargs):
        if Path(file) == path and mode == 'rb':
            return stream
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(ap, 'open', open_file, raising=False)


PAYLOADS = [
    b'', b'one', b'one\n', b'\n', b'\n' * 80,
    b'row\n' * 49, b'row\n' * 50, b'row\n' * 51,
    b'old\n' * 75 + b'partial',
    b'old\r\n' * 75 + b'last\r\n',
    b'one\rtwo\rthree',
    b'old\n' * 55 + b'\x00\xff\xfe\r\n' * 10,
    b'old\n' * 100 + b'no-final-newline-' * 100,
]


@pytest.mark.parametrize('payload', PAYLOADS, ids=[
    'empty', 'single-partial', 'single-lf', 'blank', 'many-blanks',
    '49-lines', '50-lines', '51-lines', 'partial-tail', 'crlf',
    'cr-only', 'binary', 'long-partial',
])
@pytest.mark.parametrize('block_size', [1, 7, 64, 65536])
def test_tail_digest_matches_binary_readlines(tmp_path, monkeypatch, payload, block_size):
    # raising=False lets equivalence controls also run on unchanged master.
    monkeypatch.setattr(ap, 'LEDGER_DIGEST_READ_BYTES', block_size, raising=False)
    root, _ = _ledger(tmp_path, payload)
    assert ap.compute_tree_digest(root, generator_sha=GENERATOR) == _reference(root, payload)


def test_large_history_is_not_read_to_hash_fifty_short_rows(tmp_path, monkeypatch):
    payload = b'discarded historical row\n' * 150000 + b'current row\n' * 50
    root, path = _ledger(tmp_path, payload)
    stream = TrackedLedger(payload)
    _replace_ledger(monkeypatch, path, stream)
    result = ap.compute_tree_digest(root, generator_sha=GENERATOR)
    assert result == _reference(root, payload)
    assert stream.bytes_read <= 2 * 65536, 'discarded history must not be loaded'
    assert stream.readlines_calls == 0
    assert stream.requests and all(0 < size <= 65536 for size in stream.requests)


def test_huge_unterminated_line_is_streamed_without_truncating_digest(tmp_path, monkeypatch):
    payload = b'previous\n' * 55 + b'x' * (2 * 1024 * 1024)
    root, path = _ledger(tmp_path, payload)
    stream = TrackedLedger(payload)
    _replace_ledger(monkeypatch, path, stream)
    assert ap.compute_tree_digest(root, generator_sha=GENERATOR) == _reference(root, payload)
    assert stream.readlines_calls == 0
    assert stream.requests and all(0 < size <= 65536 for size in stream.requests)


@pytest.mark.parametrize('fault', ['seek', 'backward_error', 'backward_short', 'forward_error', 'forward_eof'])
def test_io_failure_uses_only_unavailable_sentinel(tmp_path, monkeypatch, fault):
    payload = b'line with data\n' * 100
    root, path = _ledger(tmp_path, payload)
    monkeypatch.setattr(ap, 'LEDGER_DIGEST_READ_BYTES', 64, raising=False)

    class FaultyLedger(TrackedLedger):
        previous_seek = None
        forward = False
        forward_reads = 0

        def seek(self, offset, whence=os.SEEK_SET):
            if fault == 'seek':
                raise OSError('synthetic seek failure')
            if whence == os.SEEK_SET:
                if self.previous_seek is not None and offset >= self.previous_seek:
                    self.forward = True
                self.previous_seek = offset
            return super().seek(offset, whence)

        def read(self, size=-1):
            if not self.forward:
                if fault == 'backward_error':
                    raise PermissionError('synthetic backward read failure')
                if fault == 'backward_short':
                    return super().read(max(0, size - 1))
            else:
                self.forward_reads += 1
                if self.forward_reads == 2:
                    if fault == 'forward_error':
                        raise OSError('synthetic error after partial hash')
                    if fault == 'forward_eof':
                        return b''
            return super().read(size)

    _replace_ledger(monkeypatch, path, FaultyLedger(payload))
    assert ap.compute_tree_digest(root, generator_sha=GENERATOR) == _reference(root, None)


def test_append_after_eof_capture_is_deferred_to_next_digest(tmp_path, monkeypatch):
    payload = b'original\n' * 80
    root, path = _ledger(tmp_path, payload)

    class AppendingLedger(TrackedLedger):
        appended = False

        def seek(self, offset, whence=os.SEEK_SET):
            result = super().seek(offset, whence)
            if whence == os.SEEK_END and not self.appended:
                self.appended = True
                self.write(b'concurrent appended row\n' * 100)
                super().seek(result)
            return result

    _replace_ledger(monkeypatch, path, AppendingLedger(payload))
    assert ap.compute_tree_digest(root, generator_sha=GENERATOR) == _reference(root, payload)


def test_missing_empty_and_unreadable_ledger_keep_existing_sentinels(tmp_path):
    root, path = _ledger(tmp_path, b'')
    empty = ap.compute_tree_digest(root, generator_sha=GENERATOR)
    path.unlink()
    missing = ap.compute_tree_digest(root, generator_sha=GENERATOR)
    assert empty == _reference(root, b'')
    assert missing == _reference(root, None)
    assert empty != missing
    path.mkdir()
    assert ap.compute_tree_digest(root, generator_sha=GENERATOR) == missing


def test_discarded_prefix_stays_ignored_and_tail_changes_are_detected(tmp_path):
    payload = b'old\n' * 60 + b'kept\n' * 50
    root, path = _ledger(tmp_path, payload)
    before = ap.compute_tree_digest(root, generator_sha=GENERATOR)
    path.write_bytes(b'changed old prefix\n' * 100 + b'kept\n' * 50)
    assert ap.compute_tree_digest(root, generator_sha=GENERATOR) == before
    path.write_bytes(payload + b'new row\n')
    assert ap.compute_tree_digest(root, generator_sha=GENERATOR) != before


def test_non_ledger_digest_components_remain_byte_identical(tmp_path):
    root, _ = _ledger(tmp_path, b'row\n' * 90)
    for name in ap.TREE_DIGEST_SOURCES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"source": "' + name.encode() + b'"}')
    lessons = root.parent / 'eeebot-self-evolving' / 'lessons' / 'lessons.yaml'
    lessons.parent.mkdir(parents=True)
    lessons.write_bytes(b'lesson\n' * 1000)
    assert ap.compute_tree_digest(root, generator_sha=GENERATOR) == _reference(root, b'row\n' * 90)
