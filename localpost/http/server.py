"""
Simple WSGI server implementation using h11 for HTTP protocol handling.

Notes:
- ISO-8859-1 is used for header encoding/decoding as per HTTP/1.1 specification.
- The server supports keep-alive connections and graceful shutdown.
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
from collections.abc import Buffer, Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from typing import final

import h11

from localpost.http.config import DEFAULT_BUFFER_SIZE, LOGGER_NAME, ServerConfig

__all__ = ["start_http_server", "HTTPReqCtx", "RequestHandler"]


# Selector data-tag for ``Server._wakeup_r`` — distinguishable in the for-event
# loop so the selector thread knows to drain the wakeup pipe + op queue.
_WAKEUP_SENTINEL: object = object()


@final
@dataclass(eq=False, slots=True, frozen=True)
class _OpTrack:
    """Worker-enqueued op: register ``conn`` in the selector with ``data=conn``."""

    conn: HTTPConn


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


_INTERNAL_ERROR_BODY = b"Internal Server Error"
_INTERNAL_ERROR_RESPONSE = h11.Response(
    status_code=500,
    headers=[
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(_INTERNAL_ERROR_BODY)).encode("ascii")),
        (b"connection", b"close"),
    ],
)

_BAD_REQUEST_BODY = b"Bad Request"
_BAD_REQUEST_RESPONSE = h11.Response(
    status_code=400,
    headers=[
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(_BAD_REQUEST_BODY)).encode("ascii")),
        (b"connection", b"close"),
    ],
)

_PAYLOAD_TOO_LARGE_BODY = b"Payload Too Large"
_PAYLOAD_TOO_LARGE_RESPONSE = h11.Response(
    status_code=413,
    headers=[
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(_PAYLOAD_TOO_LARGE_BODY)).encode("ascii")),
        (b"connection", b"close"),
    ],
)

_REQUEST_TIMEOUT_BODY = b"Request Timeout"
_REQUEST_TIMEOUT_RESPONSE = h11.Response(
    status_code=408,
    headers=[
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(_REQUEST_TIMEOUT_BODY)).encode("ascii")),
        (b"connection", b"close"),
    ],
)


class BodyTooLarge(Exception):
    """Raised when an incoming request body would exceed ``ServerConfig.max_body_size``.

    Surfaces both from ``HTTPReqCtx.receive`` (when the handler is reading the body)
    and from ``HTTPConn``'s drain path (when the handler skipped reading it). The
    connection loop converts it into a 413 Payload Too Large response.
    """


def _content_length(headers) -> int | None:
    # h11 normalizes header names to lowercase bytes — direct equality is enough.
    for name, value in headers:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


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
            ctx.complete(_INTERNAL_ERROR_RESPONSE, _INTERNAL_ERROR_BODY)
        except Exception:
            logger.exception("Failed to send 500 after handler error; closing")
        else:
            return
    ctx._conn.close()


@contextmanager
def start_http_server(config: ServerConfig, handler: RequestHandler, /) -> Iterator[Server]:
    """Open a listening socket and yield a ``Server`` bound to ``handler``.

    The handler is fixed for the lifetime of the server — every accepted request
    is dispatched to it. Per-iteration overrides are not supported.

    On context exit the server signals shutdown, closes any active client
    connections still registered in the selector (idle keep-alive or
    mid-request), and tears down the listening socket. Borrowed connections
    held by handler threads are closed when the handler returns and tries to
    re-register them on the (now shutting-down) server.
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

    # server_sock.close()  # Safe to call it from another thread, will cause accept() to raise OSError
    with closing(server_sock), closing(selector):
        server = Server(config, handler, logger, server_sock, selector)
        logger.info("Serving on %s:%d", config.host, server.port)
        try:
            yield server
        finally:
            server._shutdown_active_connections()


@final
class Server:
    def __init__(
        self,
        config: ServerConfig,
        handler: RequestHandler,
        logger: logging.Logger,
        server_sock: socket.socket,
        selector: selectors.BaseSelector,
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
            if (conn := key.data) and isinstance(conn, HTTPConn) and conn.close_at and now > conn.close_at:
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
            # Stalled mid-request (some bytes received but no complete request
            # yet, and no response started) gets a 408; idle keep-alive gets
            # silently dropped.
            if not conn.idle and conn.parser.our_state is h11.IDLE:
                conn.sock.settimeout(self.config.rw_timeout)
                try:
                    payload = conn.parser.send(_REQUEST_TIMEOUT_RESPONSE)
                    if payload:
                        conn.sock.sendall(payload)
                    payload = conn.parser.send(h11.Data(data=_REQUEST_TIMEOUT_BODY))
                    if payload:
                        conn.sock.sendall(payload)
                    payload = conn.parser.send(h11.EndOfMessage())
                    if payload:
                        conn.sock.sendall(payload)
                except Exception:  # noqa: BLE001, S110 — the conn is being torn down anyway
                    pass
            try:
                conn.sock.close()
            except OSError:
                pass

    def track(self, conn: HTTPConn) -> None:
        """Register ``conn`` in the selector for normal HTTP processing.

        Safe from any thread. When called from a worker, the actual
        ``selector.register`` happens on the selector thread (drained from
        ``self._ops`` at the top of the next iteration). ``conn.tracked``
        is flipped optimistically so concurrent readers see the intended
        state.
        """
        sock = conn.sock
        try:
            sock.settimeout(0)
        except OSError:
            conn.tracked = False
            return
        if self.shutting_down:
            try:
                sock.close()
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

    def stop_tracking(self, conn: HTTPConn) -> None:
        """Unregister ``conn`` from the selector; the worker becomes the sole I/O owner.

        Selector-thread only (called from the dispatcher inside ``Server.run``'s
        for-event loop). Switches the socket to blocking-with-timeout
        (``rw_timeout``) so the worker can do synchronous send/recv.
        Client-disconnect detection while the conn is borrowed lives in
        :func:`localpost.http.check_cancelled` (pull-based ``MSG_PEEK``).
        """
        sock = conn.sock
        try:
            self.selector.unregister(sock)
        except (KeyError, ValueError):
            pass
        conn.tracked = False
        sock.settimeout(self.config.rw_timeout)

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

    def _apply_track(self, conn: HTTPConn) -> None:
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
        has stopped (verified in ``_service.py:run_server`` and
        ``serve_in_thread`` test fixture). Workers calling ``track`` /
        ``close`` post-shutdown self-clean via the ``shutting_down`` check.
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
            if not isinstance(key.data, HTTPConn):
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
                conn = HTTPConn(self, client_sock, client_addr)
                self.track(conn)
                continue
            if isinstance(data, HTTPConn):
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


@final
@dataclass(eq=False, slots=True)
class HTTPConn:
    server: Server
    sock: socket.socket
    addr: tuple[str, int]
    fd: int = field(init=False)
    """The integer file descriptor captured at construction time. Used to
    clean up ``selector._fd_to_key`` after ``sock.close()`` (where
    ``sock.fileno()`` returns -1)."""
    recv_closed: bool = False
    parser: h11.Connection = field(default_factory=lambda: h11.Connection(h11.SERVER))
    close_at: float | None = None  # Used only when tracked, to enforce keep-alive and read timeouts
    tracked: bool = False
    """``True`` iff this conn is registered in the selector. ``False`` while a
    worker has borrowed it (between ``stop_tracking`` and the next ``track``)."""
    body_bytes_received: int = 0
    """Cumulative body bytes received for the current request — reset on
    ``parser.start_next_cycle``. Compared against ``ServerConfig.max_body_size``
    to enforce the upload cap."""
    idle: bool = True
    """``True`` between requests (after ``start_next_cycle``) or before the
    first byte arrives. Flips to ``False`` once any byte has been received
    for the current request. Distinguishes idle keep-alive (close silently)
    from a stalled mid-request (emit 408 Request Timeout)."""

    def __post_init__(self) -> None:
        self.fd = self.sock.fileno()

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
        # Selector still has _fd_to_key[fd]. Clean up.
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

    def receive(self, size: int = DEFAULT_BUFFER_SIZE, /) -> None:
        data = self.sock.recv(size)
        self.parser.receive_data(data)
        if not data:
            self.recv_closed = True
        else:
            self.idle = False

    # Helper method when a req (conn) is borrowed
    def send(self, event: h11.InformationalResponse | h11.Response | h11.Data | h11.EndOfMessage) -> None:
        payload = self.parser.send(event)
        payload_len = len(payload)
        sock, total_sent = self.sock, 0
        while total_sent < payload_len:
            sent = sock.send(payload[total_sent:])
            if sent == 0:
                raise ConnectionAbortedError("socket is broken")
            total_sent = total_sent + sent

    def __call__(self, h: RequestHandler) -> None:
        try:
            self._loop(h)
        except h11.RemoteProtocolError as e:
            self.server.logger.warning("Bad client input from %s: %s", self.addr, e)
            self._try_send_status(_BAD_REQUEST_RESPONSE, _BAD_REQUEST_BODY)
            self.close()
        except h11.LocalProtocolError:
            self.server.logger.exception("Local protocol error from %s", self.addr)
            self._try_send_status(_INTERNAL_ERROR_RESPONSE, _INTERNAL_ERROR_BODY)
            self.close()
        except BodyTooLarge:
            self.server.logger.warning(
                "Request body from %s exceeds max_body_size=%d", self.addr, self.server.config.max_body_size
            )
            self._try_send_status(_PAYLOAD_TOO_LARGE_RESPONSE, _PAYLOAD_TOO_LARGE_BODY)
            self.close()

    def _loop(self, h: RequestHandler) -> None:
        parser = self.parser

        while self.tracked:
            if parser.our_state is h11.MUST_CLOSE:
                self.close()  # close() shuts down WR + unregisters
                return
            if parser.our_state is h11.DONE and parser.their_state is h11.DONE:
                parser.start_next_cycle()
                self.body_bytes_received = 0
                self.idle = True
                self.close_at = time.monotonic() + self.server.config.keep_alive_timeout

            event = parser.next_event()

            if event is h11.NEED_DATA:
                if parser.they_are_waiting_for_100_continue:  # Drain the request body
                    self.send(h11.Response(status_code=417, headers=[], reason="Expectation Failed"))
                    continue
                try:
                    self.receive()
                except BlockingIOError:
                    return  # Wait for it in the selector
                self.close_at = time.monotonic() + self.server.config.rw_timeout
            elif isinstance(event, h11.Data):
                self.body_bytes_received += len(event.data)
                if self.body_bytes_received > self.server.config.max_body_size:
                    raise BodyTooLarge(self.body_bytes_received)
                continue  # Drain the request body
            elif isinstance(event, h11.EndOfMessage):
                continue
            elif isinstance(event, h11.Request):
                cl = _content_length(event.headers)
                if cl is not None and cl > self.server.config.max_body_size:
                    raise BodyTooLarge(cl)
                req_ctx = HTTPReqCtx(self.server, self, event)
                try:
                    h(req_ctx)
                except BodyTooLarge:
                    raise  # outer handler emits 413
                except Exception:
                    self.server.logger.exception("Handler raised for %s %r", event.method, event.target)
                    emit_handler_error(req_ctx)
                if req_ctx.borrowed:
                    return
                if not self.tracked:
                    return  # connection was closed during error recovery
            elif isinstance(event, h11.ConnectionClosed):
                self.server.logger.debug("Client closed connection")
                self.close()
                return
            else:
                raise RuntimeError(f"Unexpected {event!r} in the connection loop")

    def _try_send_status(self, response: h11.Response, body: bytes) -> None:
        """Best-effort: try to send a response if the parser is still in a writable state.

        Used as a recovery path when the connection is about to be closed due to a
        protocol error. Failures are silently swallowed.
        """
        if self.parser.our_state is not h11.IDLE and self.parser.our_state is not h11.SEND_RESPONSE:
            return
        try:
            self.send(response)
            if body:
                self.send(h11.Data(data=body))
            self.send(h11.EndOfMessage())
        except Exception:  # noqa: BLE001, S110 — connection is being closed anyway
            pass


@dataclass(eq=False, frozen=True, slots=True)
class HTTPReqCtx:
    _server: Server
    _conn: HTTPConn
    request: h11.Request

    response_status: int | None = field(default=None, init=False)
    """Status code of the response sent for this request (set by start_response / complete)."""

    @property
    def borrowed(self) -> bool:
        return not self._conn.tracked

    @contextmanager
    def borrow(self):
        """Switch the conn out of selector tracking for the duration of the block.

        Useful for selector-thread handlers that need to do blocking I/O
        (e.g. reading a request body after sending ``100 Continue``).
        ``finish_response`` re-tracks the conn via ``_maybe_give_back`` on
        the success path; on early exit we re-track here.

        The :func:`thread_pool_handler` dispatch path doesn't go through this
        CM — it calls ``stop_tracking`` directly before queueing the conn to
        a worker, and the response-completion logic re-tracks via
        ``finish_response``.
        """
        assert not self.borrowed
        self._server.stop_tracking(self._conn)
        try:
            yield self
        finally:
            self._maybe_give_back()

    def _maybe_give_back(self) -> None:
        if self.borrowed:
            self._server.track(self._conn)

    # Usually with a simple response, like 404 or 405, with a small body (so it can fit into the kernel socket buffer,
    # to not block the server thread)
    def complete(self, response: h11.Response, body: bytes | None = None) -> None:
        self.start_response(response)
        if body is not None:
            self.send(body)
        self.finish_response()

    def receive(self, size: int = DEFAULT_BUFFER_SIZE, /) -> bytes:
        parser = self._conn.parser
        if parser.their_state is h11.DONE:  # Request body exhausted
            return b""
        if parser.they_are_waiting_for_100_continue:
            self._conn.send(h11.InformationalResponse(status_code=100, headers=[], reason="Continue"))
        while True:
            event = parser.next_event()
            if event is h11.NEED_DATA:
                # Sync handlers can't tolerate BlockingIOError on a non-blocking
                # socket: switch to a brief blocking read bounded by rw_timeout,
                # then restore. Borrowed connections are already blocking, so
                # the settimeout calls are no-ops on the rw_timeout value there.
                sock = self._conn.sock
                rw = self._server.config.rw_timeout
                try:
                    self._conn.receive(size)
                except BlockingIOError:
                    sock.settimeout(rw)
                    try:
                        self._conn.receive(size)
                    finally:
                        if self._conn.tracked:
                            sock.settimeout(0)
            elif isinstance(event, h11.Data):
                self._conn.body_bytes_received += len(event.data)
                if self._conn.body_bytes_received > self._server.config.max_body_size:
                    raise BodyTooLarge(self._conn.body_bytes_received)
                return event.data
            elif isinstance(event, h11.EndOfMessage):
                return b""
            else:  # h11.ConnectionClosed is not possible, it will be a protocol error
                raise RuntimeError(f"Unexpected h11 event: {event!r}")

    def start_response(self, response: h11.Response | h11.InformationalResponse, /) -> None:
        if isinstance(response, h11.Response):
            object.__setattr__(self, "response_status", response.status_code)
        self._conn.send(response)

    def send(self, chunk: Buffer, /) -> None:
        # h11 wants bytes; widen the public API to any Buffer (memoryview,
        # bytearray, …) so callers can avoid an explicit copy.
        self._conn.send(h11.Data(bytes(chunk) if not isinstance(chunk, bytes) else chunk))

    def finish_response(self) -> None:
        self._conn.send(h11.EndOfMessage())
        # Drain h11's pending ``EndOfMessage`` for the request side before
        # giving the conn back. For a no-body request the selector parsed
        # ``Request`` and stopped — h11 still has the implicit EndOfMessage
        # queued, and ``their_state`` won't reach ``DONE`` until something
        # consumes it. Without this drain, the next selector wake on a
        # keep-alive request hits ``PAUSED`` from ``parser.next_event``.
        #
        # If h11 needs more bytes (handler didn't read a non-empty body),
        # close the conn — keep-alive isn't safe with un-drained body bytes.
        parser = self._conn.parser
        while parser.their_state is not h11.DONE:
            event = parser.next_event()
            if event is h11.NEED_DATA or event is h11.PAUSED or isinstance(event, h11.ConnectionClosed):
                self._conn.close()
                return
        self._maybe_give_back()


RequestHandler = Callable[[HTTPReqCtx], None]
