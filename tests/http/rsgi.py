"""Tests for ``localpost.http.rsgi`` — :func:`to_rsgi` adapter and the
underlying :class:`_RSGIReqCtx` implementation.

Drives the bridge against a mocked RSGI scope + proto pair (the proto
surface is small enough to fake reliably). End-to-end tests under a
real Granian via ``granian.server.embed`` live in
``tests/openapi/aio_rsgi_integration.py``.

Symmetric with ``tests/http/asgi.py`` for the ASGI side.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

import anyio
import pytest

from localpost.http import AsyncHTTPReqCtx, Response, aread_body, to_rsgi
from localpost.http.rsgi import _RSGIReqCtx, addrs_from_scope, build_request_from_scope

# --- Mocks --------------------------------------------------------------


class _FakeHeaders:
    """Stand-in for ``granian.rsgi.RSGIHeaders``. Iterates ``items()``
    as ``(name, value)`` string pairs — that's the only surface our
    bridge touches."""

    def __init__(self, pairs: Iterable[tuple[str, str]]) -> None:
        self._pairs = list(pairs)

    def items(self) -> list[tuple[str, str]]:
        return list(self._pairs)


@dataclass(slots=True, eq=False)
class _FakeScope:
    """Stand-in for ``granian.rsgi.Scope``. RSGI scope is just a struct
    of strings; the bridge reads only what it needs."""

    method: str = "GET"
    path: str = "/"
    query_string: str = ""
    http_version: str = "1.1"
    scheme: str = "http"
    client: str = "127.0.0.1:54321"
    server: str = "127.0.0.1:8000"
    headers: _FakeHeaders = field(default_factory=lambda: _FakeHeaders([]))


@dataclass(slots=True, eq=False)
class _FakeStreamTransport:
    """Stand-in for ``granian._granian.RSGIHTTPStreamTransport``."""

    sent: list[bytes] = field(default_factory=list)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def send_str(self, data: str) -> None:
        self.sent.append(data.encode("utf-8"))


@dataclass(slots=True, eq=False)
class _FakeProto:
    """Stand-in for ``granian.rsgi.HTTPProtocol``.

    Captures the response side via the ``response_*`` calls; serves the
    request body via ``__call__`` (buffered) or ``__aiter__`` (streamed).
    Exposes a ``disconnect_event`` that ``client_disconnect()`` awaits —
    set it to simulate peer-gone.
    """

    body_chunks: list[bytes] = field(default_factory=list)
    disconnect_event: anyio.Event = field(default_factory=anyio.Event)
    response_kind: str | None = None
    response_status: int | None = None
    response_headers: list[tuple[str, str]] = field(default_factory=list)
    response_body: bytes | str | None = None
    response_file: tuple[str, int, int] | None = None
    transport: _FakeStreamTransport | None = None

    async def __call__(self) -> bytes:
        return b"".join(self.body_chunks)

    def __aiter__(self) -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            for c in self.body_chunks:
                yield c

        return gen()

    async def client_disconnect(self) -> None:
        await self.disconnect_event.wait()

    def response_empty(self, status: int, headers: list[tuple[str, str]]) -> None:
        self.response_kind = "empty"
        self.response_status = status
        self.response_headers = list(headers)

    def response_bytes(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.response_kind = "bytes"
        self.response_status = status
        self.response_headers = list(headers)
        self.response_body = body

    def response_str(self, status: int, headers: list[tuple[str, str]], body: str) -> None:
        self.response_kind = "str"
        self.response_status = status
        self.response_headers = list(headers)
        self.response_body = body

    def response_file_range(
        self,
        status: int,
        headers: list[tuple[str, str]],
        file: str,
        start: int,
        end: int,
    ) -> None:
        self.response_kind = "file_range"
        self.response_status = status
        self.response_headers = list(headers)
        self.response_file = (file, start, end)

    def response_stream(self, status: int, headers: list[tuple[str, str]]) -> _FakeStreamTransport:
        self.response_kind = "stream"
        self.response_status = status
        self.response_headers = list(headers)
        self.transport = _FakeStreamTransport()
        return self.transport


# --- Helpers ------------------------------------------------------------


def _build_ctx(*, body: bytes = b"", proto: _FakeProto | None = None) -> _RSGIReqCtx:
    """Minimal _RSGIReqCtx for ctx-only checks.

    Pre-fills the body channel with ``body`` and closes it — the ctx
    behaves as if the bridge had read exactly those bytes from upstream
    and observed EOM.
    """
    from localpost.http._types import Request

    request = Request(b"GET", b"/", b"/", b"", [])

    body_send, body_recv = anyio.create_memory_object_stream[bytes](max_buffer_size=64)
    if body:
        body_send.send_nowait(body)
    body_send.close()

    return _RSGIReqCtx(
        request=request,
        remote_addr=None,
        local_addr="127.0.0.1:8000",
        scheme="http",
        _proto=proto or _FakeProto(),
        _disconnected=anyio.Event(),
        _body_stream=body_recv,
    )


# --- Protocol conformance -----------------------------------------------


def test_rsgi_ctx_satisfies_async_protocol() -> None:
    """Concrete ``_RSGIReqCtx`` should satisfy ``AsyncHTTPReqCtx`` via
    structural typing — guard against drift."""
    ctx = _build_ctx()
    assert isinstance(ctx, AsyncHTTPReqCtx)


# --- Scope translation --------------------------------------------------


class TestScopeTranslation:
    def test_request_built_from_scope(self) -> None:
        scope = _FakeScope(
            method="POST",
            path="/items/42",
            query_string="q=1",
            headers=_FakeHeaders([("content-type", "application/json")]),
        )
        req = build_request_from_scope(scope)
        assert req.method == b"POST"
        assert req.path == b"/items/42"
        assert req.query_string == b"q=1"
        assert req.target == b"/items/42?q=1"
        assert (b"content-type", b"application/json") in req.headers

    def test_addrs_pass_through(self) -> None:
        scope = _FakeScope(client="1.2.3.4:5555", server="10.0.0.1:8000")
        remote, local = addrs_from_scope(scope)
        assert remote == "1.2.3.4:5555"
        assert local == "10.0.0.1:8000"


# --- Ctx behaviour ------------------------------------------------------


class TestCtxComplete:
    @pytest.mark.anyio
    async def test_complete_with_body_uses_response_bytes(self) -> None:
        proto = _FakeProto()
        ctx = _build_ctx(proto=proto)
        await ctx.complete(Response(status_code=200, headers=[(b"x-y", b"z")]), b"hello")
        assert proto.response_kind == "bytes"
        assert proto.response_status == 200
        assert ("x-y", "z") in proto.response_headers
        assert proto.response_body == b"hello"

    @pytest.mark.anyio
    async def test_complete_empty_uses_response_empty(self) -> None:
        proto = _FakeProto()
        ctx = _build_ctx(proto=proto)
        await ctx.complete(Response(status_code=204))
        assert proto.response_kind == "empty"
        assert proto.response_status == 204

    @pytest.mark.anyio
    async def test_complete_twice_raises(self) -> None:
        ctx = _build_ctx()
        await ctx.complete(Response(200), b"x")
        with pytest.raises(RuntimeError, match="already started"):
            await ctx.complete(Response(200), b"y")


class TestCtxStream:
    @pytest.mark.anyio
    async def test_stream_drains_chunks(self) -> None:
        proto = _FakeProto()
        ctx = _build_ctx(proto=proto)

        async def chunks() -> AsyncIterator[bytes]:
            yield b"a"
            yield b"b"

        await ctx.stream(Response(200), chunks())
        assert proto.response_kind == "stream"
        assert proto.transport is not None
        assert proto.transport.sent == [b"a", b"b"]

    @pytest.mark.anyio
    async def test_stream_short_circuits_on_disconnect(self) -> None:
        proto = _FakeProto()
        ctx = _build_ctx(proto=proto)

        async def chunks() -> AsyncIterator[bytes]:
            yield b"a"
            ctx._disconnected.set()
            yield b"b"  # should not reach the wire

        await ctx.stream(Response(200), chunks())
        assert proto.transport is not None
        assert proto.transport.sent == [b"a"]


class TestCtxSendfile:
    @pytest.mark.anyio
    async def test_sendfile_uses_response_file_range_when_path_present(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        proto = _FakeProto()
        ctx = _build_ctx(proto=proto)
        f = tmp_path / "x.bin"
        f.write_bytes(b"abcdef")
        with f.open("rb") as fh:
            await ctx.sendfile(Response(200), fh, offset=1, count=3)
        assert proto.response_kind == "file_range"
        # Path passed through as-is; range expressed as (start, end).
        assert proto.response_file == (str(f), 1, 4)

    @pytest.mark.anyio
    async def test_sendfile_falls_back_to_stream_when_no_path(self) -> None:
        import io

        proto = _FakeProto()
        ctx = _build_ctx(proto=proto)
        # BytesIO has no ``name`` attribute → fallback path.
        stream = io.BytesIO(b"abcdef")
        await ctx.sendfile(Response(200), stream, offset=1, count=3)
        assert proto.response_kind == "stream"
        assert proto.transport is not None
        assert b"".join(proto.transport.sent) == b"bcd"


class TestCtxReceive:
    """``ctx.receive(size)`` pulls from the in-process body queue,
    splitting upstream chunks down to the caller's requested ``size``.
    """

    @pytest.mark.anyio
    async def test_receive_splits_chunk_across_calls(self) -> None:
        ctx = _build_ctx(body=b"abcdef")
        assert await ctx.receive(2) == b"ab"
        assert await ctx.receive(3) == b"cde"
        assert await ctx.receive(10) == b"f"
        assert await ctx.receive(10) == b""


# --- to_rsgi end-to-end (mocked proto) ---------------------------------


async def _drive(rsgi_app: Any, scope: _FakeScope, proto: _FakeProto) -> None:
    """Invoke the RSGI app's __rsgi__ once."""
    await rsgi_app.__rsgi__(scope, proto)


class TestToRsgi:
    @pytest.mark.anyio
    async def test_simple_round_trip(self) -> None:
        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            await ctx.complete(Response(200), b"hi")

        rsgi_app = to_rsgi(handler)
        proto = _FakeProto()
        await _drive(rsgi_app, _FakeScope(), proto)
        assert proto.response_status == 200
        assert proto.response_body == b"hi"

    @pytest.mark.anyio
    async def test_body_read_via_aread_body(self) -> None:
        captured: dict[str, bytes] = {}

        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            captured["body"] = await aread_body(ctx)
            await ctx.complete(Response(200), b"ok")

        rsgi_app = to_rsgi(handler)
        proto = _FakeProto(body_chunks=[b"hello", b"-world"])
        await _drive(rsgi_app, _FakeScope(method="POST"), proto)
        assert captured["body"] == b"hello-world"

    @pytest.mark.anyio
    async def test_chunks_arrive_in_order(self) -> None:
        captured: list[bytes] = []

        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            while True:
                chunk = await ctx.receive(64)
                if not chunk:
                    break
                captured.append(chunk)
            await ctx.complete(Response(200), b"ok")

        rsgi_app = to_rsgi(handler)
        proto = _FakeProto(body_chunks=[b"first", b"second", b"third"])
        await _drive(rsgi_app, _FakeScope(method="POST"), proto)
        assert b"".join(captured) == b"firstsecondthird"

    @pytest.mark.anyio
    async def test_content_length_pre_check_413(self) -> None:
        async def handler(ctx: AsyncHTTPReqCtx) -> None:
            raise AssertionError("handler should not run")

        rsgi_app = to_rsgi(handler, max_body_size=4)
        proto = _FakeProto()
        scope = _FakeScope(
            method="POST",
            headers=_FakeHeaders([("content-length", "100")]),
        )
        await _drive(rsgi_app, scope, proto)
        assert proto.response_status == 413
