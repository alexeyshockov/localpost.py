"""Parser-agnostic HTTP server infrastructure.

This module owns the bits of the server that don't touch the HTTP parser:
the listening socket, the selector loop, the op queue / wakeup pipe,
stale-connection sweep, and shutdown. Each concrete backend (h11, httptools)
provides a ``BaseHTTPConn`` subclass driven by its parser's natural idioms.

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
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, final

from localpost.http._types import InformationalResponse, Request, Response
from localpost.http.config import LOGGER_NAME, ServerConfig

if TYPE_CHECKING:
    from collections.abc import Buffer

__all__ = [
    "BaseServer",
    "BaseHTTPConn",
    "BodyHandler",
    "HTTPReqCtx",
    "Middleware",
    "RequestHandler",
    "compose",
    "start_http_server_base",
]


# Selector data-tag for ``BaseServer._wakeup_r`` — distinguishable in the for-event
# loop so the selector thread knows to drain the wakeup pipe + op queue.
_WAKEUP_SENTINEL: object = object()


@final
@dataclass(eq=False, slots=True, frozen=True)
class _OpTrack:
    """Worker-enqueued op: register ``conn`` in the selector with ``data=conn``."""

    conn: BaseHTTPConn


@final
@dataclass(eq=False, slots=True, frozen=True)
class _OpClose:
    """Worker-enqueued op: clean up ``selector._fd_to_key[fd]`` after ``sock.close()``.

    The kernel auto-removes the fd from kqueue/epoll on ``close()``. The
    Python-side ``_fd_to_key`` dict is what we actually need to clean up so
    the conn instance can be GC'd.
    """

    fd: int


_Op = _OpTrack | _OpClose


# Canned protocol-error responses, expressed as neutral types. Each backend
# serialises them with its own writer (h11.Connection.send for the h11 impl,
# the hand-written serialiser for the httptools impl).

INTERNAL_ERROR_BODY = b"Internal Server Error"
INTERNAL_ERROR_RESPONSE = Response(
    status_code=500,
    headers=[
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(INTERNAL_ERROR_BODY)).encode("ascii")),
        (b"connection", b"close"),
    ],
)

BAD_REQUEST_BODY = b"Bad Request"
BAD_REQUEST_RESPONSE = Response(
    status_code=400,
    headers=[
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(BAD_REQUEST_BODY)).encode("ascii")),
        (b"connection", b"close"),
    ],
)

PAYLOAD_TOO_LARGE_BODY = b"Payload Too Large"
PAYLOAD_TOO_LARGE_RESPONSE = Response(
    status_code=413,
    headers=[
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(PAYLOAD_TOO_LARGE_BODY)).encode("ascii")),
        (b"connection", b"close"),
    ],
)

REQUEST_TIMEOUT_BODY = b"Request Timeout"
REQUEST_TIMEOUT_RESPONSE = Response(
    status_code=408,
    headers=[
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(REQUEST_TIMEOUT_BODY)).encode("ascii")),
        (b"connection", b"close"),
    ],
)


class HTTPReqCtx(Protocol):
    """Per-request context handed to a :data:`RequestHandler`.

    Both server backends populate concrete implementations of this Protocol
    with the same observable surface. ``_server`` and ``_conn`` are not
    public API but are documented here because :func:`thread_pool_handler`
    reaches into them to borrow the connection for a worker. They're
    declared as read-only properties so covariant subtypes (concrete
    ``HTTPConnH11`` / ``HTTPConnHttptools``) satisfy the Protocol.

    The ``body`` attribute is empty when a :data:`RequestHandler` runs
    (pre-body phase). It is populated by the selector with the fully
    buffered request body before invoking a returned :data:`BodyHandler`.

    The ``attrs`` mapping is per-request mutable state for cross-cutting
    concerns to thread information through. :class:`localpost.http.Router`
    writes the matched ``RouteMatch`` here; middlewares can attach auth
    info, tracing transactions, etc.
    """

    request: Request
    body: bytes
    response_status: int | None
    attrs: dict[str, Any]

    @property
    def _server(self) -> BaseServer: ...
    @property
    def _conn(self) -> BaseHTTPConn: ...
    @property
    def borrowed(self) -> bool: ...
    def borrow(self) -> AbstractContextManager[HTTPReqCtx]: ...
    def receive(self, size: int = ..., /) -> bytes: ...
    def start_response(self, response: Response | InformationalResponse, /) -> None: ...
    def send(self, chunk: Buffer, /) -> None: ...
    def finish_response(self) -> None: ...
    def complete(self, response: Response, body: bytes | None = None) -> None: ...


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


def _send_all(sock: socket.socket, payload: bytes | bytearray | memoryview, rw_timeout: float) -> None:
    """Send all of ``payload`` to ``sock``.

    Optimised for the JSON-API common case: a small response that fits in
    the kernel send buffer. Tries non-blocking ``send`` first; on
    :exc:`BlockingIOError` (kernel buffer full mid-response) transitions
    to blocking-with-timeout for the remainder, then restores
    non-blocking on the way out. Saves the two ``settimeout`` (fcntl)
    calls per request that the prior borrow-with-blocking design paid.
    """
    view = payload if isinstance(payload, memoryview) else memoryview(payload)
    total = len(view)
    sent = 0
    while sent < total:
        try:
            n = sock.send(view[sent:])
        except BlockingIOError:
            # Kernel send buffer full; finish the rest in blocking-with-timeout
            # mode, then restore non-blocking.
            sock.settimeout(rw_timeout)
            try:
                sock.sendall(view[sent:])
            finally:
                sock.settimeout(0)
            return
        if n == 0:
            raise ConnectionAbortedError("socket is broken")
        sent += n


def emit_handler_error(ctx: HTTPReqCtx) -> None:
    """Best-effort recovery when a request handler raises.

    Emits a 500 response if no headers have been sent yet; otherwise closes
    the connection (we can't go back and prepend a status line to bytes
    already on the wire). All I/O failures are swallowed — the goal is to
    avoid amplifying one error into another.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if ctx.response_status is None:
        try:
            ctx.complete(INTERNAL_ERROR_RESPONSE, INTERNAL_ERROR_BODY)
        except Exception:
            logger.exception("Failed to send 500 after handler error; closing")
        else:
            return
    ctx._conn.close()


class BaseHTTPConn(ABC):
    """Abstract per-connection surface used by :class:`BaseServer`.

    Subclasses own the parser instance and per-connection state. The base
    server only observes the small surface declared here: tracking flag,
    socket, fd, idle / close-at timestamps, ``__call__`` entry point,
    ``close()``, and the stale-408 emission hook.
    """

    server: BaseServer
    sock: socket.socket
    addr: tuple[str, int]
    fd: int
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
    def __call__(self, h: RequestHandler, /) -> None:
        """Drive the connection: read, parse, dispatch, write. Returns when the
        worker should be released (handler took the conn / connection closed /
        more data needed from selector)."""

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
        if threading.get_ident() == self.server._selector_thread_id:
            sel = self.server.selector
            if self.fd in sel.get_map():
                try:
                    sel.unregister(self.fd)
                except (KeyError, ValueError):
                    pass
        else:
            self.server._ops.append(_OpClose(self.fd))
            self.server._wake()

    @abstractmethod
    def emit_stale_408(self) -> None:
        """Best-effort 408 emission for a stalled mid-request connection.

        Called by :meth:`BaseServer._cleanup_stale` for conns that received
        bytes but never produced a complete request. The h11 impl sends via
        the parser; the httptools impl writes raw bytes. Implementations
        no-op when no response is appropriate (idle keep-alive, response
        already started). Failures are swallowed — the conn is being torn
        down anyway.
        """


_ConnFactory = Callable[["BaseServer", socket.socket, tuple[str, int]], BaseHTTPConn]


@final
class BaseServer:
    """Parser-agnostic server: owns the listening socket, selector, and op queue.

    A :class:`BaseHTTPConn` factory is injected at construction time — each
    accepted connection is built by ``conn_factory(server, sock, addr)``.
    All parser-specific behaviour lives behind that interface.
    """

    def __init__(
        self,
        config: ServerConfig,
        handler: RequestHandler,
        logger: logging.Logger,
        server_sock: socket.socket,
        selector: selectors.BaseSelector,
        conn_factory: _ConnFactory,
    ) -> None:
        self.sock = server_sock
        self.port: int = server_sock.getsockname()[1]
        """
        Actual port the server is listening on.

        Can be useful when port 0 is specified to auto-assign a free port.
        """
        self.selector = selector
        self.config = config
        self.handler = handler
        self.logger = logger
        self._conn_factory = conn_factory
        self.shutting_down: bool = False
        """Set to True on context-manager exit. Once set, ``track`` rejects
        new registrations and ``_maybe_give_back`` closes connections
        instead of returning them to the selector."""
        # Lock-free op queue + self-pipe wakeup. The selector thread is the
        # single writer to ``self.selector``; cross-thread mutations from
        # workers (``track`` from ``_maybe_give_back``, ``close`` from error
        # paths) enqueue ops and write a wakeup byte. The selector drains at
        # the top of every iteration and on wakeup-sentinel events.
        self._ops: collections.deque[_Op] = collections.deque()
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        for fd in (r, w):
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
        self._wakeup_r: int = r
        self._wakeup_w: int = w
        selector.register(r, selectors.EVENT_READ, data=_WAKEUP_SENTINEL)
        self._selector_thread_id: int | None = None
        """Cached on the first ``run()`` call. Used to route ``track`` /
        ``close`` calls inline when invoked from the selector thread itself
        (e.g. from the accept branch of ``run``)."""
        # Stale-connection cleanup runs lazily — at most once per
        # ``_cleanup_interval``. Avoids walking ``selector.get_map()`` on
        # every iteration when nothing's actually stale (which is the
        # common case under load: keep_alive_timeout=15s, rw_timeout=1s).
        # Floor at 100 ms so users with degenerate tiny timeouts don't
        # pathologically over-cleanup.
        self._cleanup_interval: float = max(
            min(config.rw_timeout, config.keep_alive_timeout) / 2,
            0.1,
        )
        self._last_cleanup_at: float = 0.0

    def _find_stale(self):
        now = time.monotonic()
        for key in self.selector.get_map().values():
            if (conn := key.data) and isinstance(conn, BaseHTTPConn) and conn.close_at and now > conn.close_at:
                yield conn

    def _cleanup_stale(self):
        # Selector-thread only. Lock-free: ``self.selector`` and
        # ``conn.tracked`` are owned by the selector thread.
        #
        # We deliberately keep this on the selector thread rather than a
        # dedicated cleanup thread. With the lazy gate below the common
        # case is an O(1) timestamp compare (no walk); the actual O(N)
        # sweep runs at most every ``_cleanup_interval``, ~twice a second
        # by default. A separate thread would either need a lock around
        # ``selector.get_map()`` (losing the lock-free property of the
        # current op-queue model) or maintain a parallel data structure
        # — both add machinery without buying anything visible.
        #
        # Lazy: skip the O(N) walk over registered conns when not enough
        # time has passed since the last sweep. Worst-case extra detection
        # latency is ``_cleanup_interval`` (default 0.5 s) on top of the
        # configured ``rw_timeout`` / ``keep_alive_timeout`` — both already
        # measure non-fatal conditions.
        now = time.monotonic()
        if now - self._last_cleanup_at < self._cleanup_interval:
            return
        self._last_cleanup_at = now
        stale = list(self._find_stale())
        for conn in stale:
            try:
                self.selector.unregister(conn.sock)
            except (KeyError, ValueError):
                pass
            conn.tracked = False
        for conn in stale:
            # Stalled mid-request gets a 408; idle keep-alive gets silently
            # dropped. The decision and the bytes-on-wire are the conn's
            # job — backends differ.
            try:
                conn.emit_stale_408()
            except Exception:  # noqa: BLE001, S110 — the conn is being torn down anyway
                pass
            try:
                conn.sock.close()
            except OSError:
                pass

    def track(self, conn: BaseHTTPConn) -> None:
        """Register ``conn`` in the selector for normal HTTP processing.

        Safe from any thread. When called from a worker, the actual
        ``selector.register`` happens on the selector thread (drained from
        ``self._ops`` at the top of the next iteration). ``conn.tracked``
        is flipped optimistically so concurrent readers see the intended
        state.

        The socket is kept non-blocking throughout the connection's
        lifetime — see :func:`_send_all` for the worker-side send path
        that handles ``BlockingIOError`` with a blocking-with-timeout
        fallback. This avoids two ``settimeout`` (fcntl) calls per
        request on the borrow / re-track boundary.
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

        Selector-thread only (called from the dispatcher inside ``BaseServer.run``'s
        for-event loop). Socket stays non-blocking — the worker's send
        path (:func:`_send_all`) handles partial writes and falls back
        to blocking-with-timeout only when the kernel buffer fills.
        Client-disconnect detection while the conn is borrowed lives in
        :func:`localpost.http.check_cancelled` (pull-based ``MSG_PEEK``).
        """
        try:
            self.selector.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        conn.tracked = False

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
        """Apply all pending ops on the selector thread."""
        while True:
            try:
                op = self._ops.popleft()
            except IndexError:
                return
            try:
                if isinstance(op, _OpTrack):
                    self._apply_track(op.conn)
                elif isinstance(op, _OpClose) and op.fd in self.selector.get_map():
                    try:
                        self.selector.unregister(op.fd)
                    except (KeyError, ValueError):
                        pass
            except Exception:
                self.logger.exception("Op handler raised for %r", op)

    def _apply_track(self, conn: BaseHTTPConn) -> None:
        """Selector-thread handler for ``_OpTrack``.

        Probes ``selector.get_map()`` (an O(1) dict-``in`` check) to choose
        ``modify`` vs ``register``, instead of catching ``KeyError`` from
        ``modify``. The error path inside ``selectors.modify`` builds the
        exception message as ``f"{fileobj!r}"`` — and ``socket.__repr__``
        is surprisingly expensive (~25 µs/call). Per-request overhead.
        """
        sock = conn.sock
        sel = self.selector
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

    def _shutdown_active_connections(self) -> None:
        """Set the shutdown flag and close any connections still in the selector.

        Called from ``start_http_server.__exit__`` *after* the selector loop
        has stopped. Workers calling ``track`` / ``close`` post-shutdown
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

        for key in list(self.selector.get_map().values()):
            if key.fileobj is self.sock:
                continue  # listening socket — closed by the outer CM
            if key.fileobj == self._wakeup_r:
                continue  # wakeup pipe — closed below
            if not isinstance(key.data, BaseHTTPConn):
                continue
            conn = key.data
            try:
                self.selector.unregister(conn.sock)
            except (KeyError, ValueError):
                pass
            try:
                conn.sock.close()
            except OSError:
                pass
            conn.tracked = False

        # Close the wakeup pipe.
        try:
            self.selector.unregister(self._wakeup_r)
        except (KeyError, ValueError):
            pass
        for fd in (self._wakeup_r, self._wakeup_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def run(self, *, timeout: float | None = None) -> None:
        """One iteration of the server loop. Should be called repeatedly until the server is stopped.

        ``timeout`` bounds the underlying ``selector.select`` call — it caps how long this
        method blocks before returning to the caller, giving the caller a chance to check
        for shutdown / cancellation. Defaults to ``config.select_timeout``.
        """
        if self._selector_thread_id is None:
            self._selector_thread_id = threading.get_ident()
        if timeout is None:
            timeout = self.config.select_timeout
        server_sock = self.sock
        h = self.handler
        self._drain_ops()
        self._cleanup_stale()
        for key, _ in self.selector.select(timeout=timeout):
            data = key.data
            if data is _WAKEUP_SENTINEL:
                self._drain_wakeup()
                self._drain_ops()
                continue
            if key.fileobj is server_sock:
                client_sock, client_addr = server_sock.accept()
                # Linux 2.6.28+ inherits ``O_NONBLOCK`` from the listening
                # socket; macOS / BSD do not. Set explicitly — once per
                # connection, never again. The conn stays non-blocking for
                # its lifetime; the send path (``_send_all``) handles
                # blocking-with-timeout fallback on ``BlockingIOError``.
                client_sock.setblocking(False)
                conn = self._conn_factory(self, client_sock, client_addr)
                self.track(conn)
                continue
            if isinstance(data, BaseHTTPConn):
                try:
                    data(h)
                except Exception:
                    self.logger.exception("Unhandled exception from connection %s", data.addr)
                    try:
                        data.close()
                    except Exception:  # noqa: BLE001, S110
                        pass
            else:
                raise RuntimeError(f"Unexpected selector key: {key!r}")  # noqa: TRY004


@contextmanager
def start_http_server_base(
    config: ServerConfig,
    handler: RequestHandler,
    conn_factory: _ConnFactory,
    /,
) -> Iterator[BaseServer]:
    """Open a listening socket and yield a :class:`BaseServer` driving ``conn_factory``.

    Each public entry point (``start_http_server``, ``start_httptools_server``)
    is a thin wrapper supplying its own conn factory. The handler is fixed
    for the lifetime of the server.
    """
    logger = logging.getLogger(LOGGER_NAME)
    server_sock = socket.create_server(
        (config.host, config.port),
        backlog=config.backlog,
        reuse_port=True,
    )
    selector = selectors.DefaultSelector()

    server_sock.settimeout(0)
    selector.register(server_sock, selectors.EVENT_READ)

    with closing(server_sock), closing(selector):
        server = BaseServer(config, handler, logger, server_sock, selector, conn_factory)
        logger.info("Serving on %s:%d", config.host, server.port)
        try:
            yield server
        finally:
            server._shutdown_active_connections()
