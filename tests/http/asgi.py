"""Tests for ``localpost.http.asgi`` — :func:`to_asgi` adapter and the
underlying :class:`_ASGIReqCtx` Protocol implementation.

Drives the ASGI app directly via :class:`httpx.ASGITransport` (no real
ASGI server needed); also exercises the ctx in isolation by handing it
captured ``send`` / mocked ``receive`` channels.

Symmetric with ``tests/http/wsgi_to.py`` for the sync side.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from localpost.http import (
    AsyncHTTPReqCtx,
    Response,
    to_asgi,
)
from localpost.http._types import Request
from localpost.http.asgi import _ASGIReqCtx, addrs_from_scope, build_request_from_scope

# --- Helpers ------------------------------------------------------------


def _build_ctx(
    *,
    request: Request | None = None,
    body: bytes = b"",
    send=None,  # type: ignore[no-untyped-def]
) -> _ASGIReqCtx:
    """Build a minimal _ASGIReqCtx for unit-level checks."""

    async def _swallow(_event: dict[str, Any]) -> None:
        pass

    return _ASGIReqCtx(
        request=request or Request(b"GET", b"/", b"/", b"", []),
        body=body,
        remote_addr=None,
        local_addr="0.0.0.0:0",
        scheme="http",
        _send=send or _swallow,
        _disconnected=threading.Event(),
    )


# --- Protocol conformance -----------------------------------------------


def test_asgi_ctx_satisfies_async_protocol() -> None:
    """Concrete ``_ASGIReqCtx`` should satisfy ``AsyncHTTPReqCtx`` via
    structural typing — guard against drift."""
    ctx = _build_ctx()
    assert isinstance(ctx, AsyncHTTPReqCtx)


# --- Scope translation --------------------------------------------------


class TestScopeTranslation:
    def test_request_built_from_scope(self) -> None:
        scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": "/items/42",
            "raw_path": b"/items/42",
            "query_string": b"q=1",
            "headers": [(b"content-type", b"application/json")],
            "http_version": "1.1",
        }
        req = build_request_from_scope(scope)
        assert req.method == b"POST"
        assert req.path == b"/items/42"
        assert req.query_string == b"q=1"
        assert req.target == b"/items/42?q=1"
        assert (b"content-type", b"application/json") in req.headers

    def test_addrs_from_scope_with_client_and_server(self) -> None:
        scope = {"client": ["1.2.3.4", 5555], "server": ["10.0.0.1", 8000]}
        remote, local = addrs_from_scope(scope)
        assert remote == "1.2.3.4:5555"
        assert local == "10.0.0.1:8000"

    def test_addrs_from_scope_no_client(self) -> None:
        scope: dict[str, Any] = {"server": ["10.0.0.1", 8000]}
        remote, local = addrs_from_scope(scope)
        assert remote is None
        assert local == "10.0.0.1:8000"


# --- Ctx behaviour ------------------------------------------------------


class TestCtxComplete:
    @pytest.mark.anyio
    async def test_complete_emits_start_then_body(self) -> None:
        events: list[dict[str, Any]] = []

        async def send(event: dict[str, Any]) -> None:
            events.append(event)

        ctx = _build_ctx(send=send)
        await ctx.complete(Response(status_code=200, headers=[(b"x-y", b"z")]), b"hello")
        assert [e["type"] for e in events] == ["http.response.start", "http.response.body"]
        assert events[0]["status"] == 200
        assert (b"x-y", b"z") in events[0]["headers"]
        assert events[1]["body"] == b"hello"
        assert events[1]["more_body"] is False
        assert ctx.response_status == 200

    @pytest.mark.anyio
    async def test_complete_twice_raises(self) -> None:
        ctx = _build_ctx()
        await ctx.complete(Response(200), b"x")
        with pytest.raises(RuntimeError, match="already started"):
            await ctx.complete(Response(200), b"y")


class TestCtxStream:
    @pytest.mark.anyio
    async def test_stream_drains_chunks_and_finalises(self) -> None:
        events: list[dict[str, Any]] = []

        async def send(event: dict[str, Any]) -> None:
            events.append(event)

        async def chunks() -> AsyncIterator[bytes]:
            yield b"a"
            yield b"b"

        ctx = _build_ctx(send=send)
        await ctx.stream(Response(200), chunks())
        types = [e["type"] for e in events]
        assert types[0] == "http.response.start"
        bodies = [e for e in events if e["type"] == "http.response.body"]
        assert [e["body"] for e in bodies[:2]] == [b"a", b"b"]
        # Final terminator: empty body, more_body=False.
        assert bodies[-1] == {"type": "http.response.body", "body": b"", "more_body": False}

    @pytest.mark.anyio
    async def test_stream_short_circuits_on_disconnect(self) -> None:
        events: list[dict[str, Any]] = []

        async def send(event: dict[str, Any]) -> None:
            events.append(event)

        ctx = _build_ctx(send=send)

        async def chunks() -> AsyncIterator[bytes]:
            yield b"a"
            ctx._disconnected.set()
            yield b"b"  # should not reach the wire

        await ctx.stream(Response(200), chunks())
        bodies = [e for e in events if e["type"] == "http.response.body"]
        # Only the first chunk lands; no terminator (peer is gone).
        assert [e["body"] for e in bodies] == [b"a"]


class TestCtxReceive:
    """``ctx.receive(size)`` slices the pre-buffered body — same shape
    as the sync :class:`_WSGIReqCtx.receive` helper."""

    @pytest.mark.anyio
    async def test_receive_slices_buffer(self) -> None:
        ctx = _build_ctx(body=b"abcdef")
        assert await ctx.receive(2) == b"ab"
        assert await ctx.receive(3) == b"cde"
        assert await ctx.receive(10) == b"f"
        assert await ctx.receive(10) == b""


# --- to_asgi end-to-end (via httpx.ASGITransport) -----------------------


def _client(asgi_app) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="http://t")


class TestToAsgi:
    @pytest.mark.anyio
    async def test_simple_complete_round_trip(self) -> None:
        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            await ctx.complete(Response(200), b"hi")

        asgi_app = to_asgi(handler)
        async with _client(asgi_app) as c:
            resp = await c.get("/anything")
        assert resp.status_code == 200
        assert resp.content == b"hi"

    @pytest.mark.anyio
    async def test_body_pre_buffered(self) -> None:
        captured: dict[str, bytes] = {}

        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            captured["body"] = ctx.body
            await ctx.complete(Response(200), b"ok")

        asgi_app = to_asgi(handler)
        async with _client(asgi_app) as c:
            await c.post("/", content=b"payload")
        assert captured["body"] == b"payload"

    @pytest.mark.anyio
    async def test_body_too_large_returns_413(self) -> None:
        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            await ctx.complete(Response(200), b"unreached")

        asgi_app = to_asgi(handler, max_body_size=4)
        async with _client(asgi_app) as c:
            resp = await c.post("/", content=b"more-than-four-bytes")
        assert resp.status_code == 413

    @pytest.mark.anyio
    async def test_stream_round_trip(self) -> None:
        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            async def chunks() -> AsyncIterator[bytes]:
                yield b"a"
                yield b"b"
                yield b"c"

            await ctx.stream(Response(200, headers=[(b"content-type", b"text/plain")]), chunks())

        asgi_app = to_asgi(handler)
        async with _client(asgi_app) as c:
            async with c.stream("GET", "/") as resp:
                assert resp.status_code == 200
                wire = b"".join([chunk async for chunk in resp.aiter_bytes()])
        assert wire == b"abc"

    @pytest.mark.anyio
    async def test_unsupported_scope_raises(self) -> None:
        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            pass

        asgi_app = to_asgi(handler)
        scope = {"type": "websocket"}

        async def receive() -> dict[str, Any]:
            return {"type": "websocket.disconnect"}

        async def send(_event: dict[str, Any]) -> None:
            pass

        with pytest.raises(ValueError, match="unsupported ASGI scope"):
            await asgi_app(scope, receive, send)


class TestToAsgiStreaming:
    """``to_asgi(handler, streaming=True)`` skips the pre-buffer; the
    handler reads body chunks via ``await ctx.receive(size)``."""

    @pytest.mark.anyio
    async def test_body_reads_chunks_in_order(self) -> None:
        captured: list[bytes] = []

        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            assert ctx.body == b""  # streaming mode: no pre-buffer
            while True:
                chunk = await ctx.receive(64)
                if not chunk:
                    break
                captured.append(chunk)
            await ctx.complete(Response(200), b"ok")

        asgi_app = to_asgi(handler, streaming=True)
        async with _client(asgi_app) as c:
            resp = await c.post("/", content=b"hello-streaming-world")
        assert resp.status_code == 200
        assert b"".join(captured) == b"hello-streaming-world"

    @pytest.mark.anyio
    async def test_receive_honours_size_with_partial_chunk(self) -> None:
        sizes: list[int] = []

        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            # Read 3 bytes at a time — chunks arriving on the channel
            # may be larger; ctx.receive should split them.
            while True:
                chunk = await ctx.receive(3)
                if not chunk:
                    break
                sizes.append(len(chunk))
            await ctx.complete(Response(200), b"ok")

        asgi_app = to_asgi(handler, streaming=True)
        async with _client(asgi_app) as c:
            await c.post("/", content=b"abcdefghij")
        assert all(s <= 3 for s in sizes)
        assert sum(sizes) == 10

    @pytest.mark.anyio
    async def test_content_length_pre_check_413(self) -> None:
        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            raise AssertionError("handler should not run")

        asgi_app = to_asgi(handler, streaming=True, max_body_size=4)
        async with _client(asgi_app) as c:
            resp = await c.post("/", content=b"too-long-by-far")
        assert resp.status_code == 413

    @pytest.mark.anyio
    async def test_handler_can_skip_body_and_respond(self) -> None:
        """Streaming mode: handler may respond without reading the body —
        the channel pump drains stragglers; the response still completes."""

        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            await ctx.complete(Response(204), b"")

        asgi_app = to_asgi(handler, streaming=True)
        async with _client(asgi_app) as c:
            resp = await c.post("/", content=b"ignored-body")
        assert resp.status_code == 204
