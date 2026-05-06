"""ASGI 3 transport bridge — adapt :data:`AsyncRequestHandler` ⇆ ASGI app.

Symmetric with :mod:`localpost.http.wsgi`: this module owns the
translation between the foreign protocol (ASGI 3) and our async
request-context shape (:class:`AsyncHTTPReqCtx`). The handler doesn't
know anything about ASGI — it just reads ``ctx.request`` / ``ctx.body``
and calls ``await ctx.complete(...)`` / ``await ctx.stream(...)``.

``localpost.http`` itself doesn't ship an async server — production
ASGI servers (uvicorn, hypercorn, granian) already exist. This module
plugs an :data:`AsyncRequestHandler` into one of them via :func:`to_asgi`.

Pre-buffer policy: by default the body is read into ``ctx.body`` before
dispatch (matches the JSON-API common case the ``localpost.openapi``
flavours target). ``max_body_size`` caps the buffer; bodies that
exceed it produce a 413 before the handler runs.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, BinaryIO, final

import anyio

from localpost.http._async_base import AsyncHTTPReqCtx, AsyncRequestHandler
from localpost.http._types import Request
from localpost.http._types import Response as _Response
from localpost.http.config import DEFAULT_BUFFER_SIZE

__all__ = [
    "ASGIScope",
    "ASGIReceive",
    "ASGISend",
    "ASGIApp",
    "to_asgi",
    "build_request_from_scope",
    "addrs_from_scope",
]


# ASGI 3 callable types — kept loose so we don't depend on a specific
# asgiref TypedDict. Scope / event dicts pass through verbatim.
type ASGIScope = dict[str, Any]
type ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
type ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
type ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]


# --- Public adapter -----------------------------------------------------


def to_asgi(handler: AsyncRequestHandler, *, max_body_size: int = 1 << 20) -> ASGIApp:
    """Wrap an :data:`AsyncRequestHandler` as an ASGI 3 application.

    Deploy with any ASGI server::

        from localpost.http.asgi import to_asgi


        async def my_handler(ctx):
            await ctx.complete(Response(200), b"hi")


        asgi_app = to_asgi(my_handler)
        # uvicorn myapp:asgi_app

    Args:
        handler: The async request handler.
        max_body_size: Cap on the buffered request body, in bytes.
            Bodies above this raise ``413 Payload Too Large`` before
            the handler runs. ``-1`` disables the cap. Defaults to
            ``1 << 20`` (1 MiB).

    The returned callable handles ``lifespan`` (no-op accept) and
    ``http`` scopes; WebSocket scopes are rejected with
    :class:`ValueError`. A peer ``http.disconnect`` flips
    ``ctx.disconnected``; long handlers / SSE generators poll it
    between events to short-circuit cleanly.
    """

    async def asgi_app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        kind = scope.get("type")
        if kind == "lifespan":
            await _handle_lifespan(receive, send)
            return
        if kind != "http":
            raise ValueError(f"to_asgi: unsupported ASGI scope type: {kind!r}")
        await _handle_http(handler, max_body_size, scope, receive, send)

    return asgi_app


async def _handle_http(
    handler: AsyncRequestHandler,
    max_body_size: int,
    scope: ASGIScope,
    receive: ASGIReceive,
    send: ASGISend,
) -> None:
    try:
        body = await _read_body(receive, max_body_size)
    except ValueError:
        await _send_canned(send, 413, b"Payload Too Large")
        return

    request = build_request_from_scope(scope)
    remote, local = addrs_from_scope(scope)
    disconnected = threading.Event()
    ctx = _ASGIReqCtx(
        request=request,
        body=body,
        remote_addr=remote,
        local_addr=local,
        scheme=str(scope.get("scheme", "http")),
        _send=send,
        _disconnected=disconnected,
    )

    async with anyio.create_task_group() as tg:
        tg.start_soon(_watch_disconnect, receive, disconnected)
        try:
            await handler(ctx)
        finally:
            tg.cancel_scope.cancel()


# --- Concrete ctx -------------------------------------------------------


class _ResponseAlreadyStarted(RuntimeError):
    """Raised if a handler tries to start two responses on one request."""


@final
@dataclass(slots=True, eq=False)
class _ASGIReqCtx:
    """:class:`AsyncHTTPReqCtx` backed by an ASGI 3 ``scope`` + ``send``.

    The request body is pre-buffered by :func:`_read_body` before this
    ctx is built, so :meth:`receive` slices the buffer (mirrors
    :class:`localpost.http._WSGIReqCtx`'s shape). ``complete`` and
    ``stream`` translate into ``http.response.start`` +
    ``http.response.body`` events. ``disconnected`` flips when the
    watcher task receives ``http.disconnect``.
    """

    request: Request
    body: bytes
    remote_addr: str | None
    local_addr: str
    scheme: str
    _send: ASGISend
    _disconnected: threading.Event
    response_status: int | None = None
    attrs: dict[Any, Any] = field(default_factory=dict)
    _started: bool = False
    _body_cursor: int = 0

    @property
    def disconnected(self) -> bool:
        return self._disconnected.is_set()

    async def receive(self, size: int = DEFAULT_BUFFER_SIZE, /) -> bytes:
        if self._body_cursor >= len(self.body):
            return b""
        end = self._body_cursor + size
        chunk = self.body[self._body_cursor : end]
        self._body_cursor += len(chunk)
        return chunk

    async def complete(self, response: _Response, body: bytes | None = None) -> None:
        self._check_not_started()
        self._started = True
        self.response_status = response.status_code
        await self._send(_response_start_event(response))
        await self._send({"type": "http.response.body", "body": body or b"", "more_body": False})

    async def stream(self, response: _Response, chunks: AsyncIterator[bytes], /) -> None:
        self._check_not_started()
        self._started = True
        self.response_status = response.status_code
        await self._send(_response_start_event(response))
        try:
            async for chunk in chunks:
                if self._disconnected.is_set():
                    return
                await self._send({"type": "http.response.body", "body": bytes(chunk), "more_body": True})
        finally:
            # Always close the response — the ASGI server expects a
            # final ``more_body=False`` event to release the
            # connection. Skip when the peer is already gone.
            if not self._disconnected.is_set():
                await self._send({"type": "http.response.body", "body": b"", "more_body": False})

    async def sendfile(self, response: _Response, file: BinaryIO, offset: int, count: int) -> None:
        """ASGI fallback: chunked read + stream.

        Native zero-copy isn't standardised across ASGI servers; some
        expose ``http.response.pathsend`` extension, but it's optional.
        We always go through the chunked path here.
        """
        file.seek(offset)
        chunks = _read_file_chunks(file, count, DEFAULT_BUFFER_SIZE)
        await self.stream(response, chunks)

    def _check_not_started(self) -> None:
        if self._started:
            raise _ResponseAlreadyStarted("Response already started")


# Concrete ctx implements the AsyncHTTPReqCtx Protocol — verify at import.
# (Cheap insurance against drift; structural protocols are easy to break
# silently.)
_: type[AsyncHTTPReqCtx] = _ASGIReqCtx


# --- Scope / event translation ------------------------------------------


def build_request_from_scope(scope: ASGIScope) -> Request:
    """Build a localpost :class:`Request` from an ASGI ``http`` scope."""
    method = scope["method"].encode("ascii")
    raw_path: bytes = scope.get("raw_path") or scope["path"].encode("utf-8")
    query_string: bytes = scope.get("query_string", b"") or b""
    target = raw_path + (b"?" + query_string if query_string else b"")
    headers = tuple((bytes(name).lower(), bytes(value)) for name, value in scope.get("headers", ()))
    http_version = scope.get("http_version", "1.1").encode("ascii")
    return Request(
        method=method,
        target=target,
        path=raw_path,
        query_string=query_string,
        headers=headers,
        http_version=http_version,
    )


def addrs_from_scope(scope: ASGIScope) -> tuple[str | None, str]:
    """Return ``(remote_addr, local_addr)`` from an ASGI scope.

    ASGI ``client`` / ``server`` are ``[host, port]`` lists or ``None``.
    Mirrors the sync ctx ``"host:port"`` formatting.
    """
    client = scope.get("client")
    server = scope.get("server") or [None, None]
    remote = _fmt_addr(client[0], client[1]) if client else None
    local = _fmt_addr(server[0], server[1]) or ""
    return remote, local


def _fmt_addr(host: Any, port: Any) -> str | None:
    if host is None:
        return None
    if port is None or port == "":
        return str(host)
    return f"{host}:{port}"


def _response_start_event(response: _Response) -> dict[str, Any]:
    return {
        "type": "http.response.start",
        "status": response.status_code,
        "headers": [(bytes(name), bytes(value)) for name, value in response.headers],
    }


# --- Body buffering / disconnect / lifespan / canned responses ---------


async def _read_body(receive: ASGIReceive, max_size: int) -> bytes:
    """Buffer the entire request body from successive ``http.request`` events.

    Raises :class:`ValueError` if the accumulated size would exceed
    ``max_size``. ``http.disconnect`` while reading aborts cleanly with
    whatever bytes have been received so far.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        event = await receive()
        kind = event.get("type")
        if kind == "http.disconnect":
            return b"".join(chunks)
        if kind != "http.request":
            continue
        body: bytes = event.get("body", b"") or b""
        if body:
            total += len(body)
            if max_size >= 0 and total > max_size:
                raise ValueError(f"Request body exceeds max_body_size ({max_size} bytes)")
            chunks.append(body)
        if not event.get("more_body", False):
            return b"".join(chunks)


async def _watch_disconnect(receive: ASGIReceive, flag: threading.Event) -> None:
    """Drain ASGI events while the handler runs; flip ``flag`` on
    ``http.disconnect``. Loop ends silently when the task group cancels."""
    try:
        while True:
            event = await receive()
            if event.get("type") == "http.disconnect":
                flag.set()
                return
    except Exception:  # noqa: BLE001
        # ``receive`` may raise once the response is fully sent; treat as benign.
        return


async def _handle_lifespan(receive: ASGIReceive, send: ASGISend) -> None:
    """Minimal lifespan loop — accept startup / shutdown events without
    plugging into the user's hosting service. (Hosting integration
    drives lifecycle through :mod:`localpost.hosting`.)"""
    while True:
        event = await receive()
        kind = event.get("type")
        if kind == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif kind == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _send_canned(
    send: ASGISend,
    status: int,
    body: bytes,
    *,
    extra_headers: Sequence[tuple[bytes, bytes]] = (),
) -> None:
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        *extra_headers,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _read_file_chunks(file: BinaryIO, count: int, blksize: int) -> AsyncIterator[bytes]:
    """Async-iterator wrapper around a sync file's chunked read.

    Used by :meth:`_ASGIReqCtx.sendfile` — the ``file`` is a sync
    handle (``BinaryIO``); doing the read on the event loop is fine
    for small files but blocks for large ones. A real production
    sendfile path should bridge to a thread; for now we keep the
    semantics simple and document.
    """

    async def gen() -> AsyncIterator[bytes]:
        remaining = count
        while remaining > 0:
            chunk = file.read(min(blksize, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk

    return gen()
