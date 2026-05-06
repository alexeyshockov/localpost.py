"""RSGI transport bridge — adapt :data:`AsyncRequestHandler` ⇆ Granian's RSGI app.

Symmetric with :mod:`localpost.http.asgi` and :mod:`localpost.http.wsgi`:
this module owns the translation between the foreign protocol (Granian's
RSGI) and our async request-context shape (:class:`AsyncHTTPReqCtx`).
The handler doesn't know anything about RSGI — it just reads
``ctx.request`` / ``ctx.body`` or ``await ctx.receive(size)``, and
calls ``await ctx.complete(...)`` / ``await ctx.stream(...)`` /
``await ctx.sendfile(...)``.

RSGI's wire surface is richer than ASGI's, so the bridge gets a few
wins for free:

- **Eager responses** are a single sync call (``proto.response_bytes``)
  rather than ASGI's two-event dance.
- **Sendfile** uses ``proto.response_file_range`` for true zero-copy
  when the file has a path; falls back to chunked stream otherwise.
- **Streaming uploads** don't need a pump task — RSGI's ``proto`` is
  directly async-iterable, so :meth:`receive` wraps that iterator.

This module covers Mode A (single HTTP app under Granian); Mode B
(host-as-RSGI for hosted apps with multiple services) lives in
:mod:`localpost.hosting.rsgi`.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, BinaryIO, final

import anyio

from localpost.http._async_base import AsyncHTTPReqCtx, AsyncRequestHandler
from localpost.http._types import Request
from localpost.http._types import Response as _Response
from localpost.http.config import DEFAULT_BUFFER_SIZE

if TYPE_CHECKING:
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

__all__ = [
    "RSGIScope",
    "RSGIProtocol",
    "RSGIApplication",
    "to_rsgi",
    "build_request_from_scope",
    "addrs_from_scope",
]


# RSGI types — kept loose so we don't depend on a particular Granian
# version's TypedDict layout. Granian provides ``Scope`` / ``HTTPProtocol``
# at ``granian.rsgi``; we type against ``Any`` here and trust the wire shape.
type RSGIScope = Any
type RSGIProtocol = Any
type RSGIApplication = Any


# --- Public adapter -----------------------------------------------------


def to_rsgi(
    handler: AsyncRequestHandler,
    *,
    max_body_size: int = 1 << 20,
    streaming: bool = False,
) -> RSGIApplication:
    """Wrap an :data:`AsyncRequestHandler` as an RSGI application.

    Deploy under Granian::

        from localpost.http.rsgi import to_rsgi


        async def my_handler(ctx):
            await ctx.complete(Response(200), b"hi")


        rsgi_app = to_rsgi(my_handler)
        # granian --interface rsgi myapp:rsgi_app

    Args:
        handler: The async request handler.
        max_body_size: Cap on the request body, in bytes. Buffered mode
            raises ``413 Payload Too Large`` before the handler runs;
            streaming mode pre-checks ``Content-Length`` (when present)
            and 413s if it exceeds the cap. ``-1`` disables the cap.
            Defaults to ``1 << 20`` (1 MiB).
        streaming: When ``True``, skip the pre-buffer — ``ctx.body``
            stays empty and the handler pulls body chunks via
            ``await ctx.receive(size)``. Use for large uploads.

    Returns:
        An object with ``__rsgi__`` (and no-op ``__rsgi_init__`` /
        ``__rsgi_del__``) suitable as Granian's RSGI target.
    """
    return _RSGIApp(handler, max_body_size=max_body_size, streaming=streaming)


@final
class _RSGIApp:
    """Concrete RSGI app object exposing ``__rsgi__`` and the lifecycle hooks.

    No-op lifecycle here — :class:`localpost.hosting.rsgi.HostRSGIApp`
    is the variant that runs a full hosting lifecycle alongside.
    """

    __slots__ = ("_handler", "_max_body_size", "_streaming")

    def __init__(
        self,
        handler: AsyncRequestHandler,
        *,
        max_body_size: int,
        streaming: bool,
    ) -> None:
        self._handler = handler
        self._max_body_size = max_body_size
        self._streaming = streaming

    async def __rsgi__(self, scope: RSGIScope, proto: RSGIProtocol) -> None:
        if self._streaming:
            await _dispatch_streaming(self._handler, self._max_body_size, scope, proto)
        else:
            await _dispatch_buffered(self._handler, self._max_body_size, scope, proto)

    def __rsgi_init__(self, loop: Any) -> None:
        # No background services to start.
        pass

    def __rsgi_del__(self, loop: Any) -> None:
        # No background services to stop.
        pass


# --- Dispatch -----------------------------------------------------------


async def _dispatch_buffered(
    handler: AsyncRequestHandler,
    max_body_size: int,
    scope: RSGIScope,
    proto: RSGIProtocol,
) -> None:
    request = build_request_from_scope(scope)
    if max_body_size >= 0:
        cl = _content_length(request)
        if cl is not None and cl > max_body_size:
            await _send_canned(proto, 413, b"Payload Too Large")
            return

    body = await proto()
    if max_body_size >= 0 and len(body) > max_body_size:
        await _send_canned(proto, 413, b"Payload Too Large")
        return

    remote, local = addrs_from_scope(scope)
    disconnected = threading.Event()
    ctx = _RSGIReqCtx(
        request=request,
        body=body,
        remote_addr=remote,
        local_addr=local,
        scheme=str(scope.scheme),
        _proto=proto,
        _disconnected=disconnected,
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(_watch_disconnect, proto, disconnected)
        try:
            await handler(ctx)
        finally:
            tg.cancel_scope.cancel()


async def _dispatch_streaming(
    handler: AsyncRequestHandler,
    max_body_size: int,
    scope: RSGIScope,
    proto: RSGIProtocol,
) -> None:
    request = build_request_from_scope(scope)
    if max_body_size >= 0:
        cl = _content_length(request)
        if cl is not None and cl > max_body_size:
            await _send_canned(proto, 413, b"Payload Too Large")
            return

    remote, local = addrs_from_scope(scope)
    disconnected = threading.Event()
    body_send, body_recv = anyio.create_memory_object_stream[bytes](0)
    ctx = _RSGIReqCtx(
        request=request,
        body=b"",
        remote_addr=remote,
        local_addr=local,
        scheme=str(scope.scheme),
        _proto=proto,
        _disconnected=disconnected,
        _body_stream=body_recv,
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(_pump_body, proto, body_send)
        tg.start_soon(_watch_disconnect, proto, disconnected)
        try:
            await handler(ctx)
        finally:
            tg.cancel_scope.cancel()


# --- Concrete ctx -------------------------------------------------------


class _ResponseAlreadyStarted(RuntimeError):
    """Raised if a handler tries to start two responses on one request."""


@final
@dataclass(slots=True, eq=False)
class _RSGIReqCtx:
    """:class:`AsyncHTTPReqCtx` backed by Granian's RSGI ``proto``.

    Two body-source modes (selected by ``to_rsgi(streaming=...)``):

    - **Buffered**: ``_body_stream`` is ``None``. The whole body has
      been read via ``await proto()`` before this ctx was built;
      :meth:`receive` slices ``body``.
    - **Streaming**: ``_body_stream`` is set; ``body`` is empty.
      :meth:`receive` pulls chunks from the in-process queue that
      :func:`_pump_body` feeds from ``async for chunk in proto``.

    Response paths translate directly to RSGI calls:

    - :meth:`complete` → ``proto.response_bytes`` (or ``response_empty``).
    - :meth:`stream` → ``proto.response_stream`` + ``transport.send_bytes``.
    - :meth:`sendfile` → ``proto.response_file_range`` for true
      zero-copy when the file has a filesystem path; chunked stream
      fallback otherwise.

    ``disconnected`` flips when :func:`_watch_disconnect` resolves the
    awaitable returned by ``proto.client_disconnect()``.
    """

    request: Request
    body: bytes
    remote_addr: str | None
    local_addr: str
    scheme: str
    _proto: RSGIProtocol
    _disconnected: threading.Event
    response_status: int | None = None
    attrs: dict[Any, Any] = field(default_factory=dict)
    _started: bool = False
    _body_cursor: int = 0
    _body_stream: MemoryObjectReceiveStream[bytes] | None = None
    _stream_eof: bool = False
    _stream_leftover: bytes = b""

    @property
    def disconnected(self) -> bool:
        return self._disconnected.is_set()

    async def receive(self, size: int = DEFAULT_BUFFER_SIZE, /) -> bytes:
        if self._body_stream is None:
            return self._slice_buffered(size)
        return await self._receive_streaming(size)

    def _slice_buffered(self, size: int) -> bytes:
        if self._body_cursor >= len(self.body):
            return b""
        end = self._body_cursor + size
        chunk = self.body[self._body_cursor : end]
        self._body_cursor += len(chunk)
        return chunk

    async def _receive_streaming(self, size: int) -> bytes:
        if self._stream_leftover:
            if len(self._stream_leftover) <= size:
                chunk = self._stream_leftover
                self._stream_leftover = b""
                return chunk
            chunk = self._stream_leftover[:size]
            self._stream_leftover = self._stream_leftover[size:]
            return chunk
        if self._stream_eof:
            return b""
        assert self._body_stream is not None
        try:
            chunk = await self._body_stream.receive()
        except anyio.EndOfStream:
            self._stream_eof = True
            return b""
        if len(chunk) <= size:
            return chunk
        self._stream_leftover = chunk[size:]
        return chunk[:size]

    async def complete(self, response: _Response, body: bytes | None = None) -> None:
        self._check_not_started()
        self._started = True
        self.response_status = response.status_code
        headers = _str_headers(response.headers)
        if not body:
            self._proto.response_empty(response.status_code, headers)
        else:
            self._proto.response_bytes(response.status_code, headers, body)

    async def stream(self, response: _Response, chunks: AsyncIterator[bytes], /) -> None:
        self._check_not_started()
        self._started = True
        self.response_status = response.status_code
        headers = _str_headers(response.headers)
        transport = self._proto.response_stream(response.status_code, headers)
        async for chunk in chunks:
            if self._disconnected.is_set():
                return
            await transport.send_bytes(bytes(chunk))

    async def sendfile(self, response: _Response, file: BinaryIO, offset: int, count: int) -> None:
        """Try ``proto.response_file_range`` for zero-copy when ``file``
        has a filesystem path; fall back to chunked stream otherwise."""
        self._check_not_started()
        path = _file_path(file)
        if path is not None:
            self._started = True
            self.response_status = response.status_code
            headers = _str_headers(response.headers)
            self._proto.response_file_range(response.status_code, headers, path, offset, offset + count)
            return
        # No filesystem path — chunked read fallback.
        file.seek(offset)
        chunks = _read_file_chunks(file, count, DEFAULT_BUFFER_SIZE)
        await self.stream(response, chunks)

    def _check_not_started(self) -> None:
        if self._started:
            raise _ResponseAlreadyStarted("Response already started")


# Concrete ctx implements the AsyncHTTPReqCtx Protocol — verify at import.
_: type[AsyncHTTPReqCtx] = _RSGIReqCtx


# --- Scope translation --------------------------------------------------


def build_request_from_scope(scope: RSGIScope) -> Request:
    """Build a localpost :class:`Request` from an RSGI ``scope``."""
    method = scope.method.encode("ascii")
    path = scope.path.encode("utf-8")
    query_string = scope.query_string.encode("ascii") if scope.query_string else b""
    target = path + (b"?" + query_string if query_string else b"")
    headers = tuple(
        (str(name).lower().encode("ascii"), str(value).encode("iso-8859-1")) for name, value in scope.headers.items()
    )
    http_version = scope.http_version.encode("ascii")
    return Request(
        method=method,
        target=target,
        path=path,
        query_string=query_string,
        headers=headers,
        http_version=http_version,
    )


def addrs_from_scope(scope: RSGIScope) -> tuple[str | None, str]:
    """Return ``(remote_addr, local_addr)`` from an RSGI scope.

    RSGI uses ``"host:port"`` strings on ``scope.client`` /
    ``scope.server`` — pass them through.
    """
    client = getattr(scope, "client", None) or None
    server = getattr(scope, "server", "") or ""
    return (str(client) if client else None, str(server))


def _content_length(request: Request) -> int | None:
    for name, value in request.headers:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _str_headers(headers: Any) -> list[tuple[str, str]]:
    """Convert localpost's bytes headers into RSGI's ``(str, str)`` shape."""
    return [
        (
            name.decode("iso-8859-1") if isinstance(name, bytes) else str(name),
            value.decode("iso-8859-1") if isinstance(value, bytes) else str(value),
        )
        for name, value in headers
    ]


def _file_path(file: BinaryIO) -> str | None:
    name = getattr(file, "name", None)
    if name is None or not isinstance(name, str):
        return None
    return name


# --- Body / disconnect / canned responses -------------------------------


async def _pump_body(
    proto: RSGIProtocol,
    body_send: MemoryObjectSendStream[bytes],
) -> None:
    """Streaming-mode body pump: relay ``async for chunk in proto`` into
    ``body_send`` so :meth:`_RSGIReqCtx.receive` can consume chunks
    serialised through anyio's memory stream.

    RSGI's proto is single-consumer (you can't iterate it from two
    tasks), so we own iteration here and the ctx pulls from the
    queue. Disconnect detection runs in a sibling task — RSGI exposes
    ``proto.client_disconnect()`` separately, so we don't need ASGI's
    demux pattern.
    """
    try:
        async with body_send:
            async for chunk in proto:
                if chunk:
                    await body_send.send(bytes(chunk))
    except Exception:  # noqa: BLE001
        return


async def _watch_disconnect(proto: RSGIProtocol, flag: threading.Event) -> None:
    """Set ``flag`` when ``proto.client_disconnect()`` resolves."""
    try:
        await proto.client_disconnect()
    except Exception:  # noqa: BLE001
        return
    flag.set()


async def _send_canned(proto: RSGIProtocol, status: int, body: bytes) -> None:
    headers = [
        ("content-type", "text/plain; charset=utf-8"),
        ("content-length", str(len(body))),
    ]
    proto.response_bytes(status, headers, body)


def _read_file_chunks(file: BinaryIO, count: int, blksize: int) -> AsyncIterator[bytes]:
    """Async-iterator wrapper around a sync file's chunked read.
    Used by :meth:`_RSGIReqCtx.sendfile` when zero-copy isn't available."""

    async def gen() -> AsyncIterator[bytes]:
        remaining = count
        while remaining > 0:
            chunk = file.read(min(blksize, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk

    return gen()
