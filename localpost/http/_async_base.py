"""Async HTTP request-context protocol — transport-neutral.

Sibling of :class:`localpost.http.HTTPReqCtx` (sync). This module
defines the Protocol that async transports implement —
:mod:`localpost.http.asgi` does so today; an RSGI bridge will follow.
Frameworks on top of ``localpost.http`` (notably
:class:`localpost.openapi.HttpAsyncApp`) write handlers against this
Protocol and never import a transport-specific type.

``localpost.http`` itself stays sync server-wise — production-grade
async HTTP servers already exist (uvicorn, hypercorn, granian). What
lives here is the Protocol + the adapters that translate those
servers' surfaces into our request-handler shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol, runtime_checkable

if TYPE_CHECKING:
    from localpost.http._types import Request, Response

__all__ = [
    "AsyncHTTPReqCtx",
    "AsyncRequestHandler",
]


type AsyncRequestHandler = Callable[["AsyncHTTPReqCtx"], Awaitable[None]]
"""Top-level async request handler — drives one request to a response.

Symmetric with :data:`localpost.http.RequestHandler` (sync). The async
side doesn't currently split into a pre-body / body-handler phase —
transports either pre-buffer the body (the ASGI default, capped by
``max_body_size``) or expose it via :meth:`AsyncHTTPReqCtx.receive`,
both before this handler runs.
"""


@runtime_checkable
class AsyncHTTPReqCtx(Protocol):
    """Per-request context for async handlers.

    Mirrors :class:`localpost.http.HTTPReqCtx`'s sync attributes
    (``request`` / ``body`` / ``attrs`` / ``response_status`` / addrs /
    scheme) so resolvers that read those attributes work against either
    flavour. Methods that touch the wire are async.

    ``body`` is empty unless the transport pre-buffers it before
    dispatch. Handlers that need streaming reads call
    ``await ctx.receive(size)`` directly — same name as the sync ctx,
    same contract ("give me up to ``size`` bytes of request body, or
    ``b''`` at EOF"). Whether that comes from a slice of a buffered
    body or a fresh wire read is the transport's choice.
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
    def disconnected(self) -> bool:
        """True once the peer has gone away (mid-response).

        SSE generators / long handlers poll this between events to
        short-circuit cleanly when the client disconnects. Mirrors
        what :func:`localpost.http.check_cancelled` does on the sync
        side — but there's no thread-local indirection here, the user
        already holds ``ctx``.
        """
        ...

    async def receive(self, size: int = ..., /) -> bytes:
        """Read up to ``size`` bytes of the request body.

        If the transport pre-buffered the body before dispatch (the
        ASGI default), this slices the buffer. Otherwise it reads from
        the wire. Returns ``b""`` at EOF.
        """
        ...

    async def complete(self, response: Response, body: bytes | None = None) -> None:
        """Send the final response and body in one shot. Terminal."""
        ...

    async def stream(self, response: Response, chunks: AsyncIterator[bytes], /) -> None:
        """Send ``response`` then drain ``chunks`` to the wire.

        Declarative streaming — the transport owns iteration, so it
        chooses pacing / cancellation policy. Terminal.
        """
        ...

    async def sendfile(self, response: Response, file: BinaryIO, offset: int, count: int) -> None:
        """Send ``response`` and stream ``count`` bytes from ``file``
        starting at ``offset``.

        Transports with native zero-copy support (RSGI's
        ``response_file_range``) use it; ASGI falls back to chunked
        reads. Terminal.
        """
        ...
