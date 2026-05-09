"""Tests for ``localpost.http.read_body`` / ``localpost.http.aread_body``."""

from __future__ import annotations

import pytest

from localpost.http import BodyTooLarge, aread_body, read_body

# --- Sync ----------------------------------------------------------------


class _SyncStubCtx:
    """Minimal HTTPReqCtx stub satisfying just ``receive(size)``."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def receive(self, _size: int = 0, /) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class TestReadBody:
    def test_drains_to_eof(self):
        ctx = _SyncStubCtx([b"hello ", b"world"])
        assert read_body(ctx) == b"hello world"  # type: ignore[arg-type]

    def test_empty_body(self):
        ctx = _SyncStubCtx([])
        assert read_body(ctx) == b""  # type: ignore[arg-type]

    def test_max_size_exceeded(self):
        ctx = _SyncStubCtx([b"a" * 5, b"b" * 5])
        with pytest.raises(BodyTooLarge):
            read_body(ctx, max_size=8)  # type: ignore[arg-type]

    def test_max_size_exact_fit(self):
        ctx = _SyncStubCtx([b"a" * 4, b"b" * 4])
        assert read_body(ctx, max_size=8) == b"aaaabbbb"  # type: ignore[arg-type]


# --- Async ---------------------------------------------------------------


class _AsyncStubCtx:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def receive(self, _size: int = 0, /) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class TestAreadBody:
    async def test_drains_to_eof(self):
        ctx = _AsyncStubCtx([b"hello ", b"world"])
        assert await aread_body(ctx) == b"hello world"  # type: ignore[arg-type]

    async def test_empty_body(self):
        ctx = _AsyncStubCtx([])
        assert await aread_body(ctx) == b""  # type: ignore[arg-type]

    async def test_max_size_exceeded(self):
        ctx = _AsyncStubCtx([b"a" * 5, b"b" * 5])
        with pytest.raises(BodyTooLarge):
            await aread_body(ctx, max_size=8)  # type: ignore[arg-type]
