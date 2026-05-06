"""Async per-request context — the ASGI 3 bridge.

:class:`AsyncHTTPReqCtx` mirrors the sync :class:`localpost.http.HTTPReqCtx`
Protocol but with async ``complete`` / ``stream`` / ``receive`` methods.
The concrete :class:`_ASGIReqCtx` implementation builds a :class:`Request`
from an ASGI ``scope``, pre-buffers the request body (matches the JSON-API
common case), and translates ``ctx.complete(...)`` / ``ctx.stream(...)``
into ASGI ``http.response.start`` + ``http.response.body`` events.

Resolvers (:class:`FromPath` / :class:`FromQuery` / :class:`FromHeader` /
:class:`FromBody`) only read sync attributes (``request``, ``body``,
``attrs``) — that's the entire point of pre-buffering. The same
sync resolvers work in both flavours.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from localpost.http._types import Request, Response

__all__ = [
    "AsyncHTTPReqCtx",
    "ASGIScope",
    "ASGIReceive",
    "ASGISend",
]


# ASGI 3 callable types — we don't depend on a particular asgiref TypedDict
# to keep the surface narrow. Scope/event dicts are passed through verbatim.
type ASGIScope = dict[str, Any]
type ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
type ASGISend = Callable[[dict[str, Any]], Awaitable[None]]


@runtime_checkable
class AsyncHTTPReqCtx(Protocol):
    """Per-request context handed to an :class:`AsyncOperation`.

    Mirrors :class:`localpost.http.HTTPReqCtx`'s sync attributes
    (``request``, ``body``, ``attrs``, ``response_status``, addr/scheme
    properties) so the same arg-resolvers work against either flavour.
    Methods that touch the wire are async.
    """

    request: Request
    body: bytes
    response_status: int | None
    attrs: dict[Any, Any]

    @property
    def remote_addr(self) -> str | None: ...
    @property
    def local_addr(self) -> str: ...
    @property
    def scheme(self) -> str: ...
    @property
    def disconnected(self) -> bool: ...

    async def complete(self, response: Response, body: bytes | None = None) -> None: ...
    async def stream(self, response: Response, chunks: AsyncIterator[bytes], /) -> None: ...


# --- ASGI bridge ---------------------------------------------------------


class _ResponseAlreadyStarted(RuntimeError):
    """Raised if a handler tries to start two responses on one request."""


@dataclass(slots=True, eq=False)
class _ASGIReqCtx:
    """:class:`AsyncHTTPReqCtx` backed by an ASGI 3 ``scope`` + ``send``.

    The body is pre-buffered by :func:`_read_body` before dispatch so
    sync resolvers can read ``ctx.body`` directly. ``complete`` and
    ``stream`` translate into ``http.response.start`` +
    ``http.response.body`` ASGI events. ``disconnected`` flips when an
    ``http.disconnect`` event arrives via the watcher task spawned by
    :class:`HttpAsyncApp`.
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

    @property
    def disconnected(self) -> bool:
        return self._disconnected.is_set()

    async def complete(self, response: Response, body: bytes | None = None) -> None:
        self._check_not_started()
        self._started = True
        self.response_status = response.status_code
        await self._send(_response_start_event(response))
        await self._send({"type": "http.response.body", "body": body or b"", "more_body": False})

    async def stream(self, response: Response, chunks: AsyncIterator[bytes], /) -> None:
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
            # Always close the response — even if iteration raised, the ASGI
            # server expects a final ``more_body=False`` event to release
            # the connection. Skip when the peer is already gone (the
            # transport will surface the close on its own).
            if not self._disconnected.is_set():
                await self._send({"type": "http.response.body", "body": b"", "more_body": False})

    def _check_not_started(self) -> None:
        if self._started:
            raise _ResponseAlreadyStarted("Response already started")


# --- Scope / event translation ------------------------------------------


def build_request_from_scope(scope: ASGIScope) -> Request:
    """Build a localpost :class:`Request` from an ASGI ``http`` scope."""
    method = scope["method"].encode("ascii")
    raw_path: bytes = scope.get("raw_path") or scope["path"].encode("utf-8")
    query_string: bytes = scope.get("query_string", b"") or b""
    target = raw_path + (b"?" + query_string if query_string else b"")
    # ASGI lowercases header names already; it sends bytes pairs.
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


def _response_start_event(response: Response) -> dict[str, Any]:
    return {
        "type": "http.response.start",
        "status": response.status_code,
        "headers": [(bytes(name), bytes(value)) for name, value in response.headers],
    }


async def read_body(receive: ASGIReceive, max_size: int) -> bytes:
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
            # Spec says we may ignore unknown events; keep looping.
            continue
        body: bytes = event.get("body", b"") or b""
        if body:
            total += len(body)
            if max_size >= 0 and total > max_size:
                raise ValueError(f"Request body exceeds max_body_size ({max_size} bytes)")
            chunks.append(body)
        if not event.get("more_body", False):
            return b"".join(chunks)
