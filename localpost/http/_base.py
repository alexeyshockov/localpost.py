"""Parser-agnostic HTTP server infrastructure.

This module owns the bits of the server that don't touch the HTTP parser:
the listening socket, the selector loop, the op queue / wakeup pipe,
stale-connection sweep, and shutdown. Each concrete backend (h11, httptools)
provides a ``BaseHTTPConn`` subclass driven by its parser's natural idioms.

The dispatch chain is loose-coupled:

    Selector  ── owns fd→SelectorCallback map; nothing HTTP-specific
       │
       ▼
    ConnHandler  ── after-accept policy; owns RequestHandler + conn_factory;
       │              decides which Selector tracks the new conn
       ▼
    RequestHandler  ── pre-body dispatch; returns BodyHandler|None
       │
       ▼
    BodyHandler  ── post-body continuation

ISO-8859-1 is used for header encoding/decoding as per HTTP/1.1 specification.
"""

from __future__ import annotations

import collections
import fcntl
import logging
import os
import selectors
import socket
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, closing, contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol, final

from localpost.http._types import InformationalResponse, Request, Response
from localpost.http.config import LOGGER_NAME, ServerConfig

if TYPE_CHECKING:
    from collections.abc import Buffer

__all__ = [
    "BaseHTTPConn",
    "BaseServer",
    "BodyHandler",
    "ConnFactory",
    "ConnHandler",
    "HTTPReqCtx",
    "Middleware",
    "RequestHandler",
    "RoundRobinAcceptor",
    "Selector",
    "SelectorCallback",
    "TrackHere",
    "_NativeReqCtx",
    "_native_stream",
    "compose",
    "start_http_server",
]


# --------------------------------------------------------------------------
# Op queue ops (cross-thread → selector-thread mutations)
# --------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True, eq=False)
class _OpTrack:
    """Worker-enqueued op: register ``conn`` in the selector with ``data=conn``."""

    conn: BaseHTTPConn


@final
@dataclass(frozen=True, slots=True, eq=False)
class _OpClose:
    """Worker-enqueued op: clean up ``selector._fd_to_key[fd]`` after ``sock.close()``.

    The kernel auto-removes the fd from kqueue/epoll on ``close()``. The
    Python-side ``_fd_to_key`` dict is what we actually need to clean up so
    the conn instance can be GC'd.
    """

    fd: int


@final
@dataclass(frozen=True, slots=True, eq=False)
class _OpCleanup:
    """Cleanup stale keep-alive connections."""


_Op = _OpTrack | _OpClose | _OpCleanup


# --------------------------------------------------------------------------
# Header-scanning helpers (parser-agnostic; both backends use them)
# --------------------------------------------------------------------------


def content_length(headers: Any) -> int | None:
    """Return the ``Content-Length`` value as an int, or ``None`` if absent / malformed.

    Both parsers normalise header names to lowercase bytes, so a direct
    equality check is enough.
    """
    for name, value in headers:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def scan_response_headers(headers: Any) -> tuple[bool, bool, bool]:
    """One-pass scan: ``(has_connection_close, has_framing, has_chunked)``.

    - ``has_framing`` — either ``Content-Length`` or ``Transfer-Encoding``
      is set, i.e. the auto-frame branch in ``start_response`` should be
      skipped.
    - ``has_chunked`` — ``Transfer-Encoding`` carries a ``chunked`` token
      anywhere in its value (per RFC 7230 §3.3.1, ``chunked`` must be the
      final encoding). When true, the httptools backend's ``_chunked``
      flag must be set so subsequent ``send`` calls actually wrap chunks
      with ``<size>\\r\\n<data>\\r\\n`` framing.

    Combined to avoid multiple walks per response.
    """
    has_close = False
    has_framing = False
    has_chunked = False
    for name, value in headers:
        n = name.lower()
        if n == b"connection" and b"close" in value.lower():
            has_close = True
        elif n == b"content-length":
            has_framing = True
        elif n == b"transfer-encoding":
            has_framing = True
            if b"chunked" in value.lower():
                has_chunked = True
    return has_close, has_framing, has_chunked


# --------------------------------------------------------------------------
# Canned protocol-error responses
# --------------------------------------------------------------------------

# Each backend serialises them with its own writer (h11.Connection.send for the
# h11 impl, the hand-written serialiser for the httptools impl). The httptools
# backend also has access to a fully pre-serialised wire form per canned
# response — see ``_PRESERIALIZED`` below — so error paths skip
# ``_serialize_response`` entirely.

# RFC 7231 §6.1 reason phrases for the codes the server side actually emits.
# Single source of truth for both the canned-error wire form below and
# the httptools backend's response writer (``_serialize_response``). Kept
# here so it resolves at module import time even when httptools isn't
# installed.
REASON_PHRASES: dict[int, bytes] = {
    100: b"Continue",
    200: b"OK",
    204: b"No Content",
    301: b"Moved Permanently",
    302: b"Found",
    304: b"Not Modified",
    400: b"Bad Request",
    401: b"Unauthorized",
    403: b"Forbidden",
    404: b"Not Found",
    405: b"Method Not Allowed",
    408: b"Request Timeout",
    413: b"Payload Too Large",
    417: b"Expectation Failed",
    500: b"Internal Server Error",
    501: b"Not Implemented",
    502: b"Bad Gateway",
    503: b"Service Unavailable",
}


def _build_canned(status_code: int, body: bytes) -> tuple[Response, bytes]:
    """Build a canned ``(Response, wire_bytes)`` pair.

    ``wire_bytes`` is the full status-line + headers + body, ready to send
    via :func:`_send_all`. Used by the httptools backend for the protocol
    error paths to skip per-error serialisation.
    """
    response = Response(
        status_code=status_code,
        headers=[
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"connection", b"close"),
        ],
    )
    prelude = bytearray(b"HTTP/1.1 ")
    prelude += str(status_code).encode("ascii")
    prelude += b" "
    prelude += REASON_PHRASES[status_code]
    prelude += b"\r\n"
    for name, value in response.headers:
        prelude += name
        prelude += b": "
        prelude += value
        prelude += b"\r\n"
    prelude += b"\r\n"
    return response, bytes(prelude + body)


INTERNAL_ERROR_BODY = b"Internal Server Error"
INTERNAL_ERROR_RESPONSE, INTERNAL_ERROR_WIRE = _build_canned(500, INTERNAL_ERROR_BODY)

BAD_REQUEST_BODY = b"Bad Request"
BAD_REQUEST_RESPONSE, BAD_REQUEST_WIRE = _build_canned(400, BAD_REQUEST_BODY)

PAYLOAD_TOO_LARGE_BODY = b"Payload Too Large"
PAYLOAD_TOO_LARGE_RESPONSE, PAYLOAD_TOO_LARGE_WIRE = _build_canned(413, PAYLOAD_TOO_LARGE_BODY)

REQUEST_TIMEOUT_BODY = b"Request Timeout"
REQUEST_TIMEOUT_RESPONSE, REQUEST_TIMEOUT_WIRE = _build_canned(408, REQUEST_TIMEOUT_BODY)


# --------------------------------------------------------------------------
# HTTPReqCtx Protocol + handler types
# --------------------------------------------------------------------------


class HTTPReqCtx(Protocol):
    """Per-request context handed to a :data:`RequestHandler`.

    Transport-agnostic surface — implemented by both the native
    ``localpost.http`` server backends and external transports
    (e.g. :func:`localpost.http.wsgi.to_wsgi`'s WSGI bridge).

    ``body`` is empty when a :data:`RequestHandler` runs (pre-body phase).
    It is populated by the selector / WSGI adapter with the fully
    buffered request body before a returned :data:`BodyHandler` runs.

    ``attrs`` is per-request mutable state for cross-cutting concerns
    to thread information through. :class:`localpost.http.Router` writes
    the matched ``RouteMatch`` here; middlewares can attach auth info,
    tracing transactions, etc.

    The ``remote_addr`` / ``local_addr`` / ``scheme`` fields mirror
    Granian RSGI's ``scope.client`` / ``scope.server`` / ``scope.scheme``
    — addresses are formatted as ``"host:port"``. ``remote_addr`` is
    ``None`` when the peer address isn't available (rare; e.g. some
    UNIX-domain transports).

    ``borrowed`` / :meth:`borrow` are degenerate on transports that
    don't manage their own connection lifetime (the WSGI bridge always
    reports ``borrowed=True`` and :meth:`borrow` is a no-op CM).

    **Sync vs. async surface.** Most members mirror
    :class:`localpost.http.AsyncHTTPReqCtx`. The sync-only members
    are :meth:`borrow` / :attr:`borrowed` (the native sync server
    hands a connection between selector and worker threads; async
    transports own their conn for the coroutine's lifetime and have
    nothing to borrow — WSGI satisfies this trivially so handler
    code stays portable).

    The wire-driver trio (``start_response`` / ``send`` /
    ``finish_response``) used to live here too; it has been demoted
    to :class:`_NativeReqCtx` (internal). Handlers should call
    :meth:`complete` (one-shot) or :meth:`stream` (declarative chunk
    iterator) — both transport-portable. Internal sync wire-driver
    code (the h11 / httptools backends, the ``sendfile`` path) still
    uses the trio under the hood.
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
        """True once the peer has gone away (mid-request / mid-response).

        Native backends do a non-blocking ``recv(1, MSG_PEEK | MSG_DONTWAIT)``
        on the request socket; the result is sticky once ``True``. The WSGI
        bridge has no socket handle and always returns ``False`` — surface
        host-server disconnects via ``BrokenPipeError`` from the per-chunk
        write path instead.

        Mirrors :attr:`localpost.http.AsyncHTTPReqCtx.disconnected` so SSE
        / long-running handlers can poll the same property regardless of
        transport. Sync handlers that don't have ctx in scope can still
        use :func:`localpost.http.check_cancelled` (raises) — this
        property is the pull-style alternative.
        """
        ...

    @property
    def borrowed(self) -> bool: ...
    def borrow(self) -> AbstractContextManager[HTTPReqCtx]: ...
    def receive(self, size: int = ..., /) -> bytes: ...
    def complete(self, response: Response, body: bytes | None = None) -> None: ...
    def stream(self, response: Response, chunks: Iterator[bytes], /) -> None:
        """Emit ``response`` then drain ``chunks`` to the wire.

        Declarative streaming — the transport owns iteration, so it can
        choose its own pacing / cancellation policy. The native server
        checks :func:`localpost.http.check_cancelled` between chunks and
        returns silently if the client has gone away. External transports
        (WSGI bridge) hand the iterator straight to their host server.
        """

    def sendfile(self, response: Response, file: BinaryIO, offset: int, count: int) -> None:
        """Emit ``response`` and stream ``count`` bytes from ``file`` starting
        at ``offset`` via :func:`socket.sendfile` (zero-copy). Terminal — like
        :meth:`complete`, no further response calls are valid afterwards.

        Requires the response to declare ``Content-Length: <count>``;
        the backend uses that framing to keep its parser state consistent
        with what the kernel actually wrote. The socket is set blocking
        (with ``rw_timeout``) for the duration of the syscall — restored
        on selector-thread give-back.
        """


class _NativeReqCtx(HTTPReqCtx, Protocol):
    """Internal Protocol — the native ``localpost.http`` ctx.

    Adds back the imperative wire-driver trio (``start_response`` /
    ``send`` / ``finish_response``) and the connection handle that
    internal callers (worker pool, handler-error recovery, the
    backends' own ``stream`` / ``sendfile`` impls) need. External
    transports (WSGI / ASGI / RSGI) do not implement this — they only
    satisfy :class:`HTTPReqCtx`.
    """

    @property
    def conn(self) -> BaseHTTPConn: ...
    def start_response(self, response: Response | InformationalResponse, /) -> None: ...
    def send(self, chunk: Buffer, /) -> None: ...
    def finish_response(self) -> None: ...


BodyHandler = Callable[[HTTPReqCtx], None]
"""Continuation invoked by the selector after the full request body has been
buffered into ``ctx.body``. Must complete the response (or borrow the conn)."""

RequestHandler = Callable[[HTTPReqCtx], BodyHandler | None]
"""Pre-body handler dispatched on ``on_headers_complete``.

Returns one of:
- ``None`` — handler is done (either it called ``ctx.complete(...)`` to
  send a response inline, or it called ``ctx.borrow()`` to hand the conn
  off to a worker thread). Body bytes that arrive afterwards are drained
  silently for keep-alive.
- :data:`BodyHandler` — selector buffers the full request body into
  ``ctx.body`` and then invokes the returned callable. This is the path
  for the JSON-API common case (parse JSON, build response).

Old-style ``Callable[[HTTPReqCtx], None]`` handlers continue to work —
returning ``None`` implicitly is the "done inline" path."""

Middleware = Callable[[RequestHandler], RequestHandler]
"""HTTP middleware: a function that wraps a :data:`RequestHandler`.

Plain Python decorator pattern — no special chain object. A middleware
can short-circuit pre-body (call ``ctx.complete(...)`` and return
``None`` without invoking ``inner``), inspect / modify before passing
through (``return inner(ctx)``), and wrap the returned
:data:`BodyHandler` continuation to run code after the response::

    def with_logging(inner: RequestHandler) -> RequestHandler:
        def wrapped(ctx):
            start = time.monotonic()
            result = inner(ctx)
            if result is None:
                if not ctx.borrowed:
                    _log(start, ctx)
                return None
            def post_body(ctx):
                result(ctx)
                _log(start, ctx)
            return post_body
        return wrapped
"""


def compose(*middlewares: Middleware) -> Middleware:
    """Compose middlewares left-to-right (outermost-first).

    ``compose(a, b, c)(handler)`` is equivalent to ``a(b(c(handler)))`` —
    on dispatch, ``a`` runs first and ``c`` is closest to the handler.
    """

    def wrap(handler: RequestHandler) -> RequestHandler:
        for mw in reversed(middlewares):
            handler = mw(handler)
        return handler

    return wrap


# --------------------------------------------------------------------------
# I/O helpers
# --------------------------------------------------------------------------


def _send_all(conn: BaseHTTPConn, payload: bytes | bytearray | memoryview) -> None:
    """Send all of ``payload`` to ``conn.sock``.

    Optimised for the JSON-API common case: a small response that fits in
    the kernel send buffer. Tries non-blocking ``send`` first; on
    :exc:`BlockingIOError` (kernel buffer full mid-response) flips to
    blocking-with-timeout and drains via ``sendall``. The blocking-mode
    fallback **stays sticky** for the rest of the request when the conn
    is borrowed — :meth:`HTTPReqCtx._maybe_give_back` resets the socket
    to non-blocking on hand-back. On the selector thread the socket is
    restored inline; the selector loop assumes non-blocking I/O.
    """
    sock = conn.sock
    view = payload if isinstance(payload, memoryview) else memoryview(payload)
    total = len(view)
    sent = 0
    while sent < total:
        try:
            n = sock.send(view[sent:])
        except BlockingIOError:
            # Kernel send buffer full; drain the rest with a blocking
            # sendall under ``rw_timeout``. On a borrowed conn we leave
            # the socket blocking-with-timeout so subsequent send/recv
            # calls in the same request skip the BlockingIOError dance;
            # the give-back path resets it. On the selector thread we
            # must restore non-blocking before returning.
            sock.settimeout(conn.selector.config.rw_timeout)
            try:
                sock.sendall(view[sent:])
            finally:
                if conn.tracked:
                    sock.settimeout(0)
            return
        if n == 0:
            raise ConnectionAbortedError("socket is broken")
        sent += n


def _peek_disconnected(sock: socket.socket) -> bool:
    """Non-blocking PEEK probe for peer FIN. Used by native ``_HTTPReqCtx``
    implementations to back ``ctx.disconnected``.

    Returns ``True`` iff the peer half-closed (read side) — ``recv`` returns
    ``b""`` or the socket is broken (any non-``BlockingIOError`` ``OSError``).
    Returns ``False`` when no signal is available (``BlockingIOError``) or
    when bytes are buffered and waiting to be read.

    ``MSG_PEEK`` doesn't consume bytes, so calling this from a worker holding
    a borrowed conn is safe alongside the worker's own send/recv.
    """
    try:
        peeked = sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
    except BlockingIOError:
        return False
    except OSError:
        return True
    return not peeked


def _native_stream(ctx: _NativeReqCtx, response: Response, chunks: Iterator[bytes]) -> None:
    """Internal default :meth:`HTTPReqCtx.stream` impl on top of the
    imperative trio (sync native backends only).

    Delegates to ``ctx.start_response`` / ``ctx.send`` / ``ctx.finish_response``,
    interleaving :func:`localpost.http.check_cancelled` between chunks. On
    a clean disconnect (``RequestCancelled``) returns silently — the conn
    is in an uncertain state and will be closed by the worker pool.

    ``LookupError`` from ``check_cancelled`` (no request context active —
    e.g. on the selector thread) is treated as "no cancellation
    available" and silently skipped.
    """
    # Local import to avoid the import cycle (cancel imports HTTPReqCtx).
    from localpost.http._cancel import RequestCancelled, check_cancelled  # noqa: PLC0415

    ctx.start_response(response)
    try:
        for chunk in chunks:
            try:
                check_cancelled()
            except LookupError:
                pass
            ctx.send(chunk)
    except RequestCancelled:
        return
    ctx.finish_response()


def emit_handler_error(ctx: _NativeReqCtx) -> None:
    """Best-effort recovery when a request handler raises.

    Emits a 500 response if no headers have been sent yet; otherwise closes
    the connection (we can't go back and prepend a status line to bytes
    already on the wire). All I/O failures are swallowed — the goal is to
    avoid amplifying one error into another.

    Native-only. WSGI / ASGI transports surface handler errors through
    their own protocol-level error path, not through ``ctx.conn.close()``.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if ctx.response_status is None:
        try:
            ctx.complete(INTERNAL_ERROR_RESPONSE, INTERNAL_ERROR_BODY)
        except Exception:
            logger.exception("Failed to send 500 after handler error; closing")
        else:
            return
    ctx.conn.close()


# --------------------------------------------------------------------------
# BaseHTTPConn — abstract per-connection surface
# --------------------------------------------------------------------------


class BaseHTTPConn(ABC):
    """Abstract per-connection surface used by :class:`Selector`.

    Subclasses own the parser instance and per-connection state. The
    selector only observes the small surface declared here: tracking flag,
    socket, fd, idle / close-at timestamps, ``__call__`` entry point,
    ``close()``, and the stale-408 emission hook.

    The ``__call__`` signature matches :data:`SelectorCallback` — a conn
    *is* the per-fd callback for its own socket. The :data:`RequestHandler`
    is captured as the ``handler`` field at construction time (by the
    :data:`ConnFactory`).
    """

    selector: Selector
    sock: socket.socket
    addr: tuple[str, int]
    fd: int
    handler: RequestHandler
    tracked: bool
    """``True`` iff this conn is registered in the selector. ``False`` while a
    worker has borrowed it (between ``stop_tracking`` and the next ``track``)."""
    close_at: float | None
    """Used only when tracked, to enforce keep-alive and read timeouts."""
    idle: bool
    """``True`` between requests (after a parser cycle reset) or before the
    first byte arrives. Flips to ``False`` once any byte has been received
    for the current request. Distinguishes idle keep-alive (close silently)
    from a stalled mid-request (emit 408 Request Timeout)."""

    @abstractmethod
    def __call__(self, sel: Selector, /) -> None:
        """Drive the connection: read, parse, dispatch, write. Returns when the
        worker should be released (handler took the conn / connection closed /
        more data needed from selector).

        Invoked by :meth:`Selector.run` whenever this conn's fd is readable.
        """

    def close(self) -> None:
        """Tear down the connection.

        Synchronously sends FIN + closes the socket so the kernel fd is
        freed immediately. The selector-side ``_fd_to_key`` cleanup happens
        on the selector thread — inline if we are it, else enqueued via
        ``_OpClose``. Safe to call from any thread.
        """
        was_tracked = self.tracked
        self.tracked = False
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        if not was_tracked:
            return
        sel = self.selector
        if threading.get_ident() == sel._selector_thread_id:
            try:
                sel._sel.unregister(self.fd)
            except (KeyError, ValueError):
                pass
        else:
            sel._ops.append(_OpClose(self.fd))
            sel._wake()

    @abstractmethod
    def emit_stale_408(self) -> None:
        """Best-effort 408 emission for a stalled mid-request connection.

        Called by :meth:`Selector._cleanup_stale` for conns that received
        bytes but never produced a complete request. The h11 impl sends via
        the parser; the httptools impl writes raw bytes. Implementations
        no-op when no response is appropriate (idle keep-alive, response
        already started). Failures are swallowed — the conn is being torn
        down anyway.
        """


# --------------------------------------------------------------------------
# Type aliases for the dispatch chain
# --------------------------------------------------------------------------


SelectorCallback = Callable[["Selector"], None]
"""Per-fd callback invoked by :meth:`Selector.run` when an fd it owns is
readable. The selector is passed in so the callback can register or
unregister fds on it.

Implementations are dataclasses with ``__call__`` (not closures), so their
state is repr-able for debugging. :class:`BaseHTTPConn` itself satisfies
this signature — a conn *is* its own per-fd callback. Other built-in
implementations: :class:`_DrainWakeup` (wakeup pipe), :class:`_AcceptListener`
(listen socket).
"""

ConnFactory = Callable[
    ["Selector", socket.socket, tuple[str, int], RequestHandler],
    BaseHTTPConn,
]
"""Builds a per-connection :class:`BaseHTTPConn`. Backend-specific —
:class:`localpost.http.server_h11.HTTPConn` and
:class:`localpost.http.server_httptools.HTTPConn` both satisfy this shape.
"""

ConnHandler = Callable[["Selector", socket.socket, tuple[str, int]], None]
"""After-accept policy. Receives the just-accepted raw client socket and
addr (selector that did the accept is passed for reference).

Default behaviour (:class:`TrackHere`): build a conn for the receiving
selector and track it locally.

Acceptor mode (:class:`RoundRobinAcceptor`): build a conn bound to a
worker selector and ``post_track`` it across threads.
"""


# --------------------------------------------------------------------------
# Built-in selector callbacks (callable dataclasses, not closures)
# --------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True, eq=False)
class _DrainWakeup:
    """Selector callback for the wakeup pipe fd. Drains the byte(s) and
    pulls any pending ops onto the selector thread.
    """

    def __call__(self, sel: Selector, /) -> None:
        sel._drain_wakeup()
        sel._drain_ops()


@final
@dataclass(frozen=True, slots=True, eq=False)
class _AcceptListener:
    """Selector callback for a listen socket. Accepts one connection and
    delegates to the configured :data:`ConnHandler`.

    We do **not** loop over ``accept`` here — the listening socket stays
    edge-readable until drained, but the selector polls level-triggered
    by default; a single accept per readable event keeps fairness with
    other fds and matches the existing behaviour.
    """

    listen_sock: socket.socket
    conn_handler: ConnHandler
    logger: logging.Logger

    def __call__(self, sel: Selector, /) -> None:
        client_sock, addr = self.listen_sock.accept()
        # Linux 2.6.28+ inherits ``O_NONBLOCK`` from the listening socket;
        # macOS / BSD do not. Set explicitly. While the conn is tracked
        # the socket is non-blocking; the send/recv paths may flip it to
        # blocking-with-timeout on a borrowed conn (``_send_all`` and the
        # per-backend receive helpers) and the give-back path resets it
        # before re-tracking.
        client_sock.setblocking(False)
        try:
            self.conn_handler(sel, client_sock, addr)
        except Exception:
            self.logger.exception("ConnHandler raised; closing %s", addr)
            with suppress(Exception):
                client_sock.close()


# --------------------------------------------------------------------------
# Built-in ConnHandler implementations
# --------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True, eq=False)
class TrackHere:
    """Default :data:`ConnHandler`. Builds a conn for the accepting selector
    and tracks it locally. This is the behaviour the server has always had —
    a single thread accepts, parses, and dispatches on the same selector.
    """

    handler: RequestHandler
    conn_factory: ConnFactory

    def __call__(self, sel: Selector, sock: socket.socket, addr: tuple[str, int]) -> None:
        conn = self.conn_factory(sel, sock, addr, self.handler)
        sel.track(conn)


@final
@dataclass(slots=True, eq=False)
class RoundRobinAcceptor:
    """:data:`ConnHandler` for the acceptor topology. Spreads new conns
    across a tuple of worker :class:`Selector` instances using a simple
    monotonic counter.

    The worker selectors must be running their own ``run()`` loop on a
    separate thread; this handler enqueues ``_OpTrack`` via
    :meth:`Selector.post_track`, which is cross-thread safe (op queue +
    wakeup pipe).
    """

    workers: tuple[Selector, ...]
    handler: RequestHandler
    conn_factory: ConnFactory
    _next: int = 0

    def __call__(self, _sel: Selector, sock: socket.socket, addr: tuple[str, int]) -> None:
        if not self.workers:
            with suppress(Exception):
                sock.close()
            return
        target = self.workers[self._next % len(self.workers)]
        self._next += 1
        conn = self.conn_factory(target, sock, addr, self.handler)
        target.post_track(conn)


# --------------------------------------------------------------------------
# Selector primitive
# --------------------------------------------------------------------------


@final
class Selector:
    """Dumb fd→callback dispatcher with op queue + wakeup pipe.

    A :class:`Selector` is HTTP-agnostic. It owns:

    - a :class:`selectors.BaseSelector` (fd → :data:`SelectorCallback` map)
    - a self-pipe wakeup so worker threads can ask the selector thread to
      apply ``_OpTrack`` / ``_OpClose`` ops
    - the stale-connection sweep over registered :class:`BaseHTTPConn`s
    - a ``shutting_down`` flag

    The selector thread is the **single writer** to the underlying
    ``selectors.BaseSelector``. ``register`` / ``unregister`` must be
    called from the selector thread; the cross-thread mutation paths
    (``track`` from a worker, ``close`` from any thread) enqueue ops and
    wake the selector.

    Use cases:

    - **All-in-one** (default): a :class:`BaseServer` owns a Selector,
      registers its listen socket on it, and runs the loop on one
      thread. Same behaviour as before this refactor.
    - **Worker-only**: a Selector with no listen socket. The acceptor
      topology spawns N of these, each on its own thread; conns are
      delivered via :meth:`post_track` from a separate acceptor thread.
    """

    def __init__(self, config: ServerConfig, *, port: int, logger: logging.Logger) -> None:
        self.config = config
        self.port: int = port
        """Bound port the *server* is listening on. Threaded through to
        worker selectors so :data:`HTTPReqCtx`-consumers (e.g. the WSGI
        bridge) can populate ``SERVER_PORT`` without reaching for the
        listen socket."""
        self.logger = logger
        self._sel: selectors.BaseSelector = selectors.DefaultSelector()
        self.shutting_down: bool = False
        """Set to True on context-manager exit. Once set, ``track`` rejects
        new registrations and ``_maybe_give_back`` closes connections
        instead of returning them to the selector."""
        # Lock-free op queue + self-pipe wakeup. The selector thread is the
        # single writer to ``self._sel``; cross-thread mutations from
        # workers (``track`` from ``_maybe_give_back``, ``close`` from error
        # paths) enqueue ops and write a wakeup byte. The selector drains at
        # the top of every iteration and on wakeup-callback events.
        self._ops: collections.deque[_Op] = collections.deque()
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        for fd in (r, w):
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
        self._wakeup_r: int = r
        self._wakeup_w: int = w
        self._sel.register(r, selectors.EVENT_READ, data=_DrainWakeup())
        self._selector_thread_id: int | None = None
        """Cached on the first ``run()`` call. Used to route ``track`` /
        ``close`` calls inline when invoked from the selector thread itself
        (e.g. from the accept callback)."""

    # ----- Public registration API (selector-thread only) -----

    def register(self, fd: int, callback: SelectorCallback, /) -> None:
        """Register ``fd`` with ``callback``. Selector-thread only."""
        self._sel.register(fd, selectors.EVENT_READ, data=callback)

    def unregister(self, fd: int, /) -> None:
        """Unregister ``fd``. Selector-thread only. Swallows missing-fd errors."""
        try:
            self._sel.unregister(fd)
        except (KeyError, ValueError):
            pass

    # ----- Conn tracking -----

    def track(self, conn: BaseHTTPConn) -> None:
        """Register ``conn`` in the selector for normal HTTP processing.

        Safe from any thread. When called from a worker, the actual
        ``selectors.register`` happens on the selector thread (drained from
        ``self._ops`` at the top of the next iteration). ``conn.tracked``
        is flipped optimistically so concurrent readers see the intended
        state.

        The socket is non-blocking whenever a conn is tracked. The send /
        recv paths (see :func:`_send_all` and the receive helpers in each
        backend) lazily flip the socket to blocking-with-timeout on the
        first ``BlockingIOError`` and leave it sticky for the rest of
        the request on a borrowed conn; ``_maybe_give_back`` resets the
        socket to non-blocking before re-tracking. The common case
        (response fits in the kernel buffer) pays zero ``settimeout``
        calls per request.

        **Synchronisation edge for parser ownership.** This method's
        op-queue enqueue + wakeup-pipe ``os.write`` (in :meth:`_wake`)
        is a full memory barrier: anything the worker did to the conn's
        parser (``parser.send`` for h11; ``parser.feed_data`` callbacks
        for httptools streaming) is visible to the selector after this
        call.
        """
        if self.shutting_down:
            try:
                conn.sock.close()
            except OSError:
                pass
            conn.tracked = False
            return
        if conn.tracked:
            return  # already tracked — no-op, nothing to enqueue
        conn.tracked = True  # optimistic
        if threading.get_ident() == self._selector_thread_id:
            self._apply_track(conn)
        else:
            self._ops.append(_OpTrack(conn))
            self._wake()

    def stop_tracking(self, conn: BaseHTTPConn) -> None:
        """Unregister ``conn`` from the selector; the worker becomes the sole I/O owner.

        Selector-thread only (called from the dispatcher inside the conn's
        ``__call__``). Socket stays non-blocking — the worker's send
        path (:func:`_send_all`) handles partial writes and falls back
        to blocking-with-timeout only when the kernel buffer fills.
        Client-disconnect detection while the conn is borrowed lives in
        :func:`localpost.http.check_cancelled` (pull-based ``MSG_PEEK``).

        **Synchronisation edge for parser ownership.** Once this returns,
        the selector is done with the conn's parser (h11.Connection /
        httptools.HttpRequestParser); the worker has exclusive access
        until :meth:`track` re-registers. See the parser field's
        docstring on each backend for the full invariant.
        """
        try:
            self._sel.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        conn.tracked = False

    def post_track(self, conn: BaseHTTPConn) -> None:
        """Cross-thread :meth:`track`. Always enqueues ``_OpTrack`` and wakes
        the selector — never applies inline. Used by the acceptor
        topology to deliver fresh conns from the acceptor thread to a
        worker selector.
        """
        if self.shutting_down:
            with suppress(OSError):
                conn.sock.close()
            conn.tracked = False
            return
        conn.tracked = True
        self._ops.append(_OpTrack(conn))
        self._wake()

    def post_close(self, fd: int) -> None:
        """Cross-thread close-cleanup. Enqueues a ``_OpClose`` so the
        selector thread cleans its ``_fd_to_key`` map after the worker
        already closed the socket.
        """
        self._ops.append(_OpClose(fd))
        self._wake()

    def post_cleanup(self) -> None:
        """Cross-thread: ask the selector thread to sweep stale conns.

        Coalesced inside :meth:`_drain_ops` — multiple posts before the
        next drain pass run :meth:`_cleanup_stale` once. Cheap when no
        conns are stale (single dict iteration).
        """
        self._ops.append(_OpCleanup())
        self._wake()

    # ----- Op queue + self-pipe helpers -----

    def _wake(self) -> None:
        """Signal the selector that ``self._ops`` has work.

        ``BlockingIOError`` (pipe full) is benign: a wakeup is already pending,
        the existing byte will fire ``select()`` and the queue is the source
        of truth. Other ``OSError`` (pipe closed during shutdown) is also
        swallowed.
        """
        try:
            os.write(self._wakeup_w, b"\x00")
        except (BlockingIOError, OSError):
            pass

    def _drain_wakeup(self) -> None:
        try:
            while os.read(self._wakeup_r, 4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def _drain_ops(self) -> None:
        """Apply all pending ops on the selector thread.

        ``_OpCleanup`` is coalesced — multiple posts in the same drain pass
        run :meth:`_cleanup_stale` once at the end.
        """
        cleanup_due = False
        while True:
            try:
                op = self._ops.popleft()
            except IndexError:
                break
            try:
                if isinstance(op, _OpTrack):
                    self._apply_track(op.conn)
                elif isinstance(op, _OpClose) and op.fd in self._sel.get_map():
                    try:
                        self._sel.unregister(op.fd)
                    except (KeyError, ValueError):
                        pass
                elif isinstance(op, _OpCleanup):
                    cleanup_due = True
            except Exception:
                self.logger.exception("Op handler raised for %r", op)
        if cleanup_due:
            self._cleanup_stale()

    def _apply_track(self, conn: BaseHTTPConn) -> None:
        """Selector-thread handler for ``_OpTrack``.

        Probes ``self._sel.get_map()`` (an O(1) dict-``in`` check) to choose
        ``modify`` vs ``register``, instead of catching ``KeyError`` from
        ``modify``. The error path inside ``selectors.modify`` builds the
        exception message as ``f"{fileobj!r}"`` — and ``socket.__repr__``
        is surprisingly expensive (~25 µs/call). Per-request overhead.
        """
        sock = conn.sock
        sel = self._sel
        try:
            if conn.fd in sel.get_map():
                sel.modify(sock, selectors.EVENT_READ, data=conn)
            else:
                sel.register(sock, selectors.EVENT_READ, data=conn)
        except Exception:  # noqa: BLE001
            conn.tracked = False
            try:
                sock.close()
            except OSError:
                pass

    # ----- Stale-conn sweep -----

    def _find_stale(self) -> Iterator[BaseHTTPConn]:
        now = time.monotonic()
        for key in self._sel.get_map().values():
            data = key.data
            if isinstance(data, BaseHTTPConn) and data.close_at and now > data.close_at:
                yield data

    def _cleanup_stale(self) -> None:
        # Selector-thread only. Lock-free: ``self._sel`` and ``conn.tracked``
        # are owned by the selector thread.
        stale = list(self._find_stale())
        for conn in stale:
            try:
                self._sel.unregister(conn.sock)
            except (KeyError, ValueError):
                pass
            conn.tracked = False
        for conn in stale:
            # Stalled mid-request gets a 408; idle keep-alive gets silently
            # dropped. The decision and the bytes-on-wire are the conn's
            # job — backends differ.
            with suppress(Exception):
                conn.emit_stale_408()
            with suppress(OSError):
                conn.sock.close()

    # ----- Run loop -----

    def run(self, *, timeout: float | None = None) -> None:
        """One iteration of the selector loop. Should be called repeatedly until the selector is stopped.

        ``timeout`` bounds the underlying ``selectors.select`` call — it caps
        how long this method blocks before returning to the caller, giving
        the caller a chance to check for shutdown / cancellation. Defaults to
        ``config.select_timeout``.

        Uniform dispatch — every fd in the map has a :data:`SelectorCallback`
        as ``key.data``. No isinstance branches, no sentinel comparisons.
        """
        if self._selector_thread_id is None:
            self._selector_thread_id = threading.get_ident()
        if timeout is None:
            timeout = self.config.select_timeout
        self._drain_ops()
        for key, _ in self._sel.select(timeout=timeout):
            cb: SelectorCallback = key.data
            try:
                cb(self)
            except Exception:
                self.logger.exception("Selector callback raised: %r", cb)

    # ----- Shutdown -----

    def shutdown(self) -> None:
        """Set the shutdown flag, drain residual ops, close registered conns,
        and close the wakeup pipe.

        Called from the outer context manager *after* the selector loop has
        stopped. Workers calling ``track`` / ``close`` post-shutdown
        self-clean via the ``shutting_down`` check.
        """
        self.shutting_down = True

        # Drain residual ops — selector won't process them, so the conn
        # references would otherwise leak.
        while True:
            try:
                op = self._ops.popleft()
            except IndexError:
                break
            if isinstance(op, _OpTrack):
                try:
                    op.conn.sock.close()
                except OSError:
                    pass
                op.conn.tracked = False
            # _OpClose just cleans _fd_to_key — handled by the walk below.

        for key in list(self._sel.get_map().values()):
            data = key.data
            if not isinstance(data, BaseHTTPConn):
                continue
            try:
                self._sel.unregister(data.sock)
            except (KeyError, ValueError):
                pass
            try:
                data.sock.close()
            except OSError:
                pass
            data.tracked = False

        # Close the wakeup pipe.
        with suppress(KeyError, ValueError):
            self._sel.unregister(self._wakeup_r)
        for fd in (self._wakeup_r, self._wakeup_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def close(self) -> None:
        """Close the underlying ``selectors.BaseSelector``. Idempotent."""
        with suppress(Exception):
            self._sel.close()


# --------------------------------------------------------------------------
# BaseServer — listen socket + Selector + ConnHandler composition
# --------------------------------------------------------------------------


@final
class BaseServer:
    """Listening server: composes a :class:`Selector`, a listen socket, and
    a :data:`ConnHandler`.

    Parser-agnostic; the conn factory lives inside the :data:`ConnHandler`
    (so ``BaseServer`` itself doesn't know whether you're using the h11 or
    httptools backend).

    The default :class:`TrackHere` ``ConnHandler`` reproduces today's
    behaviour: each accepted connection is built and tracked on the same
    selector that accepted it. Use :class:`RoundRobinAcceptor` to
    distribute conns across worker selectors.
    """

    def __init__(
        self,
        config: ServerConfig,
        conn_handler: ConnHandler,
        logger: logging.Logger,
        server_sock: socket.socket,
        selector: Selector,
    ) -> None:
        self.sock = server_sock
        self.port: int = server_sock.getsockname()[1]
        """Actual port the server is listening on (useful when port 0 is
        specified to auto-assign a free port)."""
        self.selector = selector
        self.config = config
        self.conn_handler = conn_handler
        self.logger = logger
        # Listen socket is registered with an _AcceptListener callback.
        server_sock.settimeout(0)
        selector.register(
            server_sock.fileno(),
            _AcceptListener(server_sock, conn_handler, logger),
        )

    def run(self, *, timeout: float | None = None) -> None:
        """Forwarder to :meth:`Selector.run`. Convenience for the common
        all-in-one shape (BaseServer owns its selector and the caller
        drives one loop).
        """
        self.selector.run(timeout=timeout)

    @property
    def shutting_down(self) -> bool:
        return self.selector.shutting_down

    def shutdown(self) -> None:
        """Tear down: shut down the selector (which closes registered conns
        and the wakeup pipe). The listening socket is closed by the outer
        context manager.
        """
        self.selector.shutdown()


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


def _cleanup_ticker(period: float, selector: Selector, stop: threading.Event) -> None:
    """Daemon-thread cleanup driver for the bare ``start_http_server`` CM.

    Posts an ``_OpCleanup`` to ``selector`` every ``period`` seconds until
    ``stop`` is set. Hosted callers use the anyio variant in ``_service.py``
    instead — this exists so the bare context manager remains self-contained.
    """
    while not stop.wait(period):
        selector.post_cleanup()


@contextmanager
def _start_http_server(
    config: ServerConfig,
    handler: RequestHandler,
    conn_factory: ConnFactory,
    /,
) -> Iterator[BaseServer]:
    """Open a listening socket and yield a :class:`BaseServer` driving ``conn_factory``.

    Internal helper. Public callers use :func:`start_http_server`, which
    selects the per-backend ``HTTPConn`` factory based on
    :attr:`ServerConfig.backend`.
    """
    logger = logging.getLogger(LOGGER_NAME)
    server_sock = socket.create_server(
        (config.host, config.port),
        backlog=config.backlog,
        reuse_port=True,
    )
    port = server_sock.getsockname()[1]
    selector = Selector(config, port=port, logger=logger)
    conn_handler = TrackHere(handler, conn_factory)

    cleanup_stop = threading.Event()
    cleanup_thread = threading.Thread(
        target=_cleanup_ticker,
        args=(config.select_timeout, selector, cleanup_stop),
        name=f"localpost-http-cleanup-{port}",
        daemon=True,
    )

    with closing(server_sock):
        server = BaseServer(config, conn_handler, logger, server_sock, selector)
        logger.info("Serving on %s:%d", config.host, server.port)
        cleanup_thread.start()
        try:
            yield server
        finally:
            cleanup_stop.set()
            cleanup_thread.join(timeout=config.select_timeout * 2)
            server.shutdown()
            selector.close()


def start_http_server(config: ServerConfig, handler: RequestHandler, /) -> AbstractContextManager[BaseServer]:
    """Open a listening socket and yield a server driving the configured backend.

    Backend is read from ``config.backend``. ``"h11"`` (default) is the
    pure-Python parser shipped with the core install; ``"httptools"`` is
    the C-based llhttp wrapper and requires the ``[http-fast]`` extra.
    """
    backend = config.backend
    if backend == "h11":
        from localpost.http.server_h11 import HTTPConn  # noqa: PLC0415
    elif backend == "httptools":
        try:
            from localpost.http.server_httptools import HTTPConn  # noqa: PLC0415
        except ImportError as e:
            raise ImportError(
                "httptools backend requires the [http-fast] extra (pip install localpost[http-fast])"
            ) from e
    else:
        raise ValueError(f"unknown backend {backend!r} (expected 'h11' or 'httptools')")
    return _start_http_server(config, handler, HTTPConn)
