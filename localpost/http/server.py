"""
Simple WSGI server implementation using h11 for HTTP protocol handling.

Notes:
- ISO-8859-1 is used for header encoding/decoding as per HTTP/1.1 specification.
- The server supports keep-alive connections and graceful shutdown.
"""

from __future__ import annotations

import logging
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
        self._lock = threading.Lock()
        self.shutting_down: bool = False
        """Set to True on context-manager exit. Once set, ``track`` rejects
        new registrations and ``_maybe_give_back`` closes connections
        instead of returning them to the selector."""
        # Pre-baked ``Keep-Alive: timeout=N`` header tuple, or ``None`` if
        # keep-alive is disabled (``keep_alive_timeout < 1``). Computed once
        # so :meth:`HTTPReqCtx._maybe_inject_keep_alive` doesn't pay the
        # f-string + ``.encode`` cost per request.
        ka_timeout = int(config.keep_alive_timeout)
        self.keep_alive_header: tuple[bytes, bytes] | None = (
            (b"keep-alive", f"timeout={ka_timeout}".encode("ascii")) if ka_timeout >= 1 else None
        )

    def _find_stale(self):
        now = time.monotonic()
        for key in self.selector.get_map().values():
            if (conn := key.data) and isinstance(conn, HTTPConn) and conn.close_at and now > conn.close_at:
                yield conn

    def _cleanup_stale(self):
        with self._lock:
            stale = list(self._find_stale())
            for conn in stale:
                try:
                    self.selector.unregister(conn.sock)
                except (KeyError, ValueError):
                    pass
                conn.tracked = False
        # Drop the lock for any I/O — closing a socket can block, and we don't
        # want to hold the selector lock while talking to the kernel.
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

        Idempotent — a no-op if ``conn`` is already tracked.
        """
        sock = conn.sock
        try:
            sock.settimeout(0)
        except OSError:
            conn.tracked = False
            return
        with self._lock:
            if self.shutting_down:
                try:
                    sock.close()
                except OSError:
                    pass
                conn.tracked = False
                return
            if not conn.tracked:
                self.selector.register(sock, selectors.EVENT_READ, data=conn)
                conn.tracked = True

    def stop_tracking(self, conn: HTTPConn) -> None:
        """Unregister ``conn`` from the selector; the worker becomes the sole I/O owner.

        Switches the socket to blocking-with-timeout (``rw_timeout``) so the
        worker can do synchronous send/recv. Client-disconnect detection
        while the conn is borrowed lives in
        :func:`localpost.http.check_cancelled` (pull-based ``MSG_PEEK``).
        """
        sock = conn.sock
        with self._lock:
            try:
                self.selector.unregister(sock)
            except (KeyError, ValueError):
                pass
            conn.tracked = False
        sock.settimeout(self.config.rw_timeout)

    def _shutdown_active_connections(self) -> None:
        """Set the shutdown flag and close any connections still in the selector.

        Called from ``start_http_server.__exit__``. Borrowed connections (held
        by handler threads) are not in the selector — they are closed by their
        handlers on the next ``_maybe_give_back`` after we set the flag.
        """
        with self._lock:
            self.shutting_down = True
            keys = list(self.selector.get_map().values())
            for key in keys:
                if key.fileobj is self.sock:
                    continue  # listening socket — closed by the outer CM
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

    def run(self, *, timeout: float | None = None) -> None:
        """One iteration of the server loop. Should be called repeatedly until the server is stopped.

        ``timeout`` bounds the underlying ``selector.select`` call — it caps how long this
        method blocks before returning to the caller, giving the caller a chance to check
        for shutdown / cancellation. Defaults to ``config.select_timeout``.
        """
        if timeout is None:
            timeout = self.config.select_timeout
        server_sock = self.sock
        h = self.handler
        self._cleanup_stale()
        for key, _ in self.selector.select(timeout=timeout):
            if key.fileobj is server_sock:
                client_sock, client_addr = server_sock.accept()
                conn = HTTPConn(self, client_sock, client_addr)
                self.track(conn)
            elif (data := key.data) and isinstance(data, HTTPConn):
                try:
                    data(h)
                except Exception:
                    self.logger.exception("Unhandled exception from connection %s", data.addr)
                    try:
                        data.close()
                    except Exception:  # noqa: BLE001, S110
                        pass
            else:
                raise RuntimeError(f"Unexpected selector key: {key!r}")


@final
@dataclass(eq=False, slots=True)
class HTTPConn:
    server: Server
    sock: socket.socket
    addr: tuple[str, int]
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

    def close(self) -> None:
        if self.tracked:
            with self.server._lock:
                try:
                    self.server.selector.unregister(self.sock)
                except (KeyError, ValueError):
                    pass
                self.tracked = False
        # Send a FIN before close() so the client sees a clean half-close
        # rather than a possible RST when there's unread data in the kernel
        # receive buffer. Errors are expected on already-broken sockets.
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        self.sock.close()

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
        assert not self.borrowed
        self._server.stop_tracking(self._conn)
        try:
            yield self
        finally:
            self._maybe_give_back()
            # if not self._conn.recv_closed:
            #     self._server.track(self._conn)

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
            response = self._maybe_inject_keep_alive(response)
        self._conn.send(response)

    def _maybe_inject_keep_alive(self, response: h11.Response) -> h11.Response:
        """Append ``Keep-Alive: timeout=N`` to the response on persistent HTTP/1.1
        connections so clients can size their keep-alive pool to our deadline.

        Header names are compared directly because h11 normalizes them to
        lowercase bytes on both the request side (parser output) and the
        response side (``h11.Response`` constructor). The keep-alive tuple
        itself is pre-baked on the server.
        """
        ka_header = self._server.keep_alive_header
        if ka_header is None:
            return response
        if self.request.http_version != b"1.1":
            return response
        for name, value in self.request.headers:
            if name == b"connection" and b"close" in value.lower():
                return response
        for name, value in response.headers:
            if name == b"connection" and b"close" in value.lower():
                return response
            if name == b"keep-alive":
                return response  # caller already set it
        return h11.Response(
            status_code=response.status_code,
            headers=[*response.headers, ka_header],
            reason=response.reason,
        )

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
