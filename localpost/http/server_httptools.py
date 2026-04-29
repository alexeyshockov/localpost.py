"""HTTP/1.1 server backend driven by ``httptools`` (llhttp wrapper).

Faster, opt-in alternative to the default :mod:`localpost.http.server_h11`
backend. httptools is a parse-only C extension — response bytes and
keep-alive bookkeeping are handled here.

Initial scope:

- request body streaming via per-conn body buffer
- ``Expect: 100-continue`` (response written inline on first ``receive()``)
- keep-alive driven by ``parser.should_keep_alive()`` and response headers
- pipelining (multiple requests parsed from one ``feed_data`` call) supported
  via a ready-queue
- request bodies serialised by the caller using ``Content-Length``
  (matching what ``Router`` and ``wrap_wsgi`` already produce)

Out of scope for v1: chunked Transfer-Encoding on the response side, HTTP
upgrade negotiation (WebSockets), HTTP/2.
"""

from __future__ import annotations

import collections
import socket
import time
from collections.abc import Buffer, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import final

try:
    import httptools
except ImportError as _e:  # pragma: no cover
    raise ImportError(
        "httptools is not installed. Install with: pip install localpost[http-fast]"
    ) from _e

from localpost.http._base import (
    BAD_REQUEST_BODY,
    BAD_REQUEST_RESPONSE,
    PAYLOAD_TOO_LARGE_BODY,
    PAYLOAD_TOO_LARGE_RESPONSE,
    REQUEST_TIMEOUT_BODY,
    REQUEST_TIMEOUT_RESPONSE,
    BaseHTTPConn,
    BaseServer,
    RequestHandler,
    emit_handler_error,
    start_http_server_base,
)
from localpost.http._types import BodyTooLarge, InformationalResponse, Request, Response
from localpost.http.config import DEFAULT_BUFFER_SIZE, ServerConfig

__all__ = ["start_httptools_server", "HTTPConnHttptools", "HTTPReqCtxHttptools"]


# RFC 7231 §6.1 reason phrases for the codes the server-side actually emits.
_DEFAULT_REASONS: dict[int, bytes] = {
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


def _serialize_response(r: Response | InformationalResponse) -> bytes:
    """Serialise a status + headers block to wire bytes (no body)."""
    out = bytearray(b"HTTP/1.1 ")
    out += str(r.status_code).encode("ascii")
    out += b" "
    out += r.reason or _DEFAULT_REASONS.get(r.status_code, b"")
    out += b"\r\n"
    for name, value in r.headers:
        out += name
        out += b": "
        out += value
        out += b"\r\n"
    out += b"\r\n"
    return bytes(out)


def _has_connection_close(headers: list[tuple[bytes, bytes]]) -> bool:
    return any(name.lower() == b"connection" and b"close" in value.lower() for name, value in headers)


def _has_content_length_or_te(headers: list[tuple[bytes, bytes]]) -> bool:
    """True if the user already framed the response (Content-Length or Transfer-Encoding)."""
    for name, _ in headers:
        n = name.lower()
        if n == b"content-length" or n == b"transfer-encoding":
            return True
    return False


@dataclass(eq=False, slots=True)
class _ReadyRequest:
    """A request whose headers have been parsed (and possibly more).

    Body chunks accumulate as they arrive from subsequent ``feed_data`` calls;
    ``complete`` flips when ``on_message_complete`` fires.
    """

    request: Request
    body: collections.deque[bytes] = field(default_factory=collections.deque)
    complete: bool = False
    expect_100_continue: bool = False
    keep_alive: bool = True
    body_received: int = 0


@final
@dataclass(eq=False, slots=True)
class HTTPConnHttptools(BaseHTTPConn):
    server: BaseServer
    sock: socket.socket
    addr: tuple[str, int]
    fd: int = field(init=False)
    parser: httptools.HttpRequestParser = field(init=False)
    close_at: float | None = None
    tracked: bool = False
    idle: bool = True

    # Currently-being-parsed request state (between on_message_begin and on_message_complete):
    _cur_target: bytes = b""
    _cur_headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    _cur_expect_100: bool = False
    _cur_oversize: bool = False  # set by on_headers_complete if Content-Length exceeds cap

    # Most recently promoted ready request — body / EOM callbacks update its state.
    _last_ready: _ReadyRequest | None = None

    # Completed requests waiting for dispatch (head = next to dispatch). A request is
    # appended here on on_headers_complete; its body chunks accumulate via on_body until
    # on_message_complete flips ``complete``.
    _ready: collections.deque[_ReadyRequest] = field(default_factory=collections.deque)

    # Body cap tracking. Populated either eagerly from Content-Length
    # (on_headers_complete) or progressively via on_body.
    _body_too_large: int | None = None

    # Set when the response indicates ``Connection: close`` or the request lacked
    # keep-alive support — the conn is closed once finish_response returns.
    _close_after_response: bool = False
    _response_started: bool = False

    def __post_init__(self) -> None:
        self.fd = self.sock.fileno()
        self.parser = httptools.HttpRequestParser(self)

    # ----- httptools callbacks (fired inside parser.feed_data) -----

    def on_message_begin(self) -> None:
        self._cur_target = b""
        self._cur_headers = []
        self._cur_expect_100 = False
        self._cur_oversize = False

    def on_url(self, url: bytes) -> None:
        self._cur_target = self._cur_target + url if self._cur_target else url

    def on_header(self, name: bytes, value: bytes) -> None:
        n = name.lower()
        self._cur_headers.append((n, value))
        if n == b"expect" and value.lower() == b"100-continue":
            self._cur_expect_100 = True

    def on_headers_complete(self) -> None:
        # Build the neutral Request envelope and promote it to the ready queue.
        method = self.parser.get_method()
        version = self.parser.get_http_version().encode("ascii")
        keep_alive = self.parser.should_keep_alive()

        # Eager content-length cap check.
        cl: int | None = None
        for n, v in self._cur_headers:
            if n == b"content-length":
                try:
                    cl = int(v)
                except ValueError:
                    cl = None
                break
        if cl is not None and cl > self.server.config.max_body_size:
            self._body_too_large = cl
            self._cur_oversize = True
            return

        req = Request(
            method=bytes(method),
            target=bytes(self._cur_target),
            headers=self._cur_headers,
            http_version=version,
        )
        ready = _ReadyRequest(
            request=req,
            expect_100_continue=self._cur_expect_100,
            keep_alive=keep_alive,
        )
        self._ready.append(ready)
        self._last_ready = ready

    def on_body(self, data: bytes) -> None:
        if self._cur_oversize or self._last_ready is None:
            return
        new_total = self._last_ready.body_received + len(data)
        if new_total > self.server.config.max_body_size:
            self._body_too_large = new_total
            return
        self._last_ready.body_received = new_total
        self._last_ready.body.append(bytes(data))

    def on_message_complete(self) -> None:
        if self._last_ready is not None:
            self._last_ready.complete = True
        self._last_ready = None

    # ----- BaseHTTPConn surface -----

    def __call__(self, h: RequestHandler, /) -> None:
        try:
            self._loop(h)
        except _ProtocolError as e:
            self.server.logger.warning("Bad client input from %s: %s", self.addr, e)
            self._try_send_status(BAD_REQUEST_RESPONSE, BAD_REQUEST_BODY)
            self.close()
        except BodyTooLarge:
            self.server.logger.warning(
                "Request body from %s exceeds max_body_size=%d", self.addr, self.server.config.max_body_size
            )
            self._try_send_status(PAYLOAD_TOO_LARGE_RESPONSE, PAYLOAD_TOO_LARGE_BODY)
            self.close()

    def _feed(self, data: bytes) -> None:
        try:
            self.parser.feed_data(data)
        except httptools.HttpParserUpgrade as e:
            raise _ProtocolError(f"HTTP upgrade not supported: {e}") from e
        except httptools.HttpParserError as e:
            raise _ProtocolError(str(e)) from e
        if self._body_too_large is not None:
            n = self._body_too_large
            self._body_too_large = None
            raise BodyTooLarge(n)

    def _loop(self, h: RequestHandler) -> None:
        config = self.server.config
        while self.tracked:
            # Dispatch any ready request before reading more bytes. We pop
            # before dispatching: the request is committed to ``req_ctx``
            # and ownership transfers — otherwise a worker-pool handler
            # (which sets ``borrowed=True`` and returns) would leave the
            # request in the queue and we'd re-dispatch it on re-track.
            if self._ready:
                ready = self._ready.popleft()
                req_ctx = HTTPReqCtxHttptools(self.server, self, ready)
                try:
                    h(req_ctx)
                except BodyTooLarge:
                    raise
                except Exception:
                    self.server.logger.exception(
                        "Handler raised for %s %r", ready.request.method, ready.request.target
                    )
                    emit_handler_error(req_ctx)
                if req_ctx.borrowed:
                    return
                if not self.tracked:
                    return  # connection was closed during error recovery
                if self._close_after_response or not ready.keep_alive:
                    self.close()
                    return
                self._reset_for_next_request()
                continue

            # No request ready — pump bytes from the socket.
            try:
                data = self.sock.recv(DEFAULT_BUFFER_SIZE)
            except BlockingIOError:
                return  # back to selector
            if not data:
                self.close()
                return
            self.idle = False
            self.close_at = time.monotonic() + config.rw_timeout
            self._feed(data)

    def _reset_for_next_request(self) -> None:
        # Per-request parsing scratch is cleared on the next on_message_begin.
        # Reset our own conn-level fields here.
        self._response_started = False
        self.idle = True
        self.close_at = time.monotonic() + self.server.config.keep_alive_timeout

    def _try_send_status(self, response: Response, body: bytes) -> None:
        """Best-effort: write a status line + body if we haven't started a response yet."""
        if self._response_started:
            return
        try:
            self.sock.settimeout(self.server.config.rw_timeout)
            self.sock.sendall(_serialize_response(response))
            if body:
                self.sock.sendall(body)
        except Exception:  # noqa: BLE001, S110 — connection is being closed anyway
            pass

    def emit_stale_408(self) -> None:
        """Stalled mid-request → 408. Idle keep-alive → silently dropped."""
        if self.idle or self._response_started:
            return
        try:
            self.sock.settimeout(self.server.config.rw_timeout)
            self.sock.sendall(_serialize_response(REQUEST_TIMEOUT_RESPONSE))
            if REQUEST_TIMEOUT_BODY:
                self.sock.sendall(REQUEST_TIMEOUT_BODY)
        except Exception:  # noqa: BLE001, S110 — the conn is being torn down anyway
            pass


class _ProtocolError(Exception):
    """Translated httptools parser error. Mapped to 400 by the conn loop."""


@dataclass(eq=False, frozen=True, slots=True)
class HTTPReqCtxHttptools:
    """Per-request context for the httptools backend."""

    _server: BaseServer
    _conn: HTTPConnHttptools
    _ready: _ReadyRequest

    response_status: int | None = field(default=None, init=False)
    _continue_sent: bool = field(default=False, init=False)
    _chunked: bool = field(default=False, init=False)
    """``True`` if the backend auto-added ``Transfer-Encoding: chunked`` because
    the response had neither ``Content-Length`` nor an explicit
    ``Transfer-Encoding`` — chunks must be framed and a terminator written."""

    @property
    def request(self) -> Request:
        return self._ready.request

    @property
    def borrowed(self) -> bool:
        return not self._conn.tracked

    @contextmanager
    def borrow(self) -> Iterator[HTTPReqCtxHttptools]:
        assert not self.borrowed
        self._server.stop_tracking(self._conn)
        try:
            yield self
        finally:
            self._maybe_give_back()

    def _maybe_give_back(self) -> None:
        if self.borrowed:
            self._server.track(self._conn)

    def complete(self, response: Response, body: bytes | None = None) -> None:
        self.start_response(response)
        if body is not None:
            self.send(body)
        self.finish_response()

    def receive(self, size: int = DEFAULT_BUFFER_SIZE, /) -> bytes:
        # 100-continue: caller is opting to read the body; tell the client
        # to actually send it.
        if self._ready.expect_100_continue and not self._continue_sent:
            self._send_continue()
        # Drain accumulated chunks before pumping more bytes.
        if self._ready.body:
            chunk = self._ready.body.popleft()
            return chunk
        if self._ready.complete:
            return b""
        # No buffered body, no EOM seen — pump more bytes from the socket.
        sock = self._conn.sock
        rw = self._server.config.rw_timeout
        while True:
            try:
                data = sock.recv(size)
            except BlockingIOError:
                # Selector-thread handler on a non-blocking socket: switch
                # to blocking-with-timeout for the duration of this receive,
                # mirroring the h11 backend.
                sock.settimeout(rw)
                try:
                    data = sock.recv(size)
                finally:
                    if self._conn.tracked:
                        sock.settimeout(0)
            if not data:
                # Peer closed mid-body — surface as EOF for the handler.
                self._ready.complete = True
                return b""
            self._conn._feed(data)
            if self._ready.body:
                return self._ready.body.popleft()
            if self._ready.complete:
                return b""

    def _send_continue(self) -> None:
        self._conn.sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        object.__setattr__(self, "_continue_sent", True)

    def start_response(self, response: Response | InformationalResponse, /) -> None:
        if isinstance(response, Response):
            object.__setattr__(self, "response_status", response.status_code)
            self._conn._response_started = True
            if _has_connection_close(response.headers) or not self._ready.keep_alive:
                self._conn._close_after_response = True
            # Auto-frame: no Content-Length / Transfer-Encoding → chunked.
            # Without framing, an HTTP/1.1 client would wait for the connection
            # to close before considering the response complete.
            if not _has_content_length_or_te(response.headers):
                response = Response(
                    status_code=response.status_code,
                    headers=[*response.headers, (b"transfer-encoding", b"chunked")],
                    reason=response.reason,
                )
                object.__setattr__(self, "_chunked", True)
        self._conn.sock.sendall(_serialize_response(response))

    def send(self, chunk: Buffer, /) -> None:
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        if not chunk:
            return
        if self._chunked:
            # RFC 7230 §4.1: <hex-size> CRLF <data> CRLF — concatenated into
            # a single ``sendall`` to keep this to one syscall per chunk.
            framed = f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n"
            self._conn.sock.sendall(framed)
        else:
            self._conn.sock.sendall(chunk)

    def finish_response(self) -> None:
        if self._chunked:
            # Terminating chunk: ``0\r\n\r\n``.
            self._conn.sock.sendall(b"0\r\n\r\n")
        # Drain any remaining request-body bytes the handler skipped — leftover
        # on the wire would corrupt the next pipelined request.
        if not self._ready.complete:
            try:
                while not self._ready.complete:
                    chunk = self.receive(DEFAULT_BUFFER_SIZE)
                    if not chunk:
                        break
            except (BlockingIOError, OSError, _ProtocolError, BodyTooLarge):
                # We can't safely recover — the conn must close.
                self._conn._close_after_response = True
        self._maybe_give_back()


def start_httptools_server(config: ServerConfig, handler: RequestHandler, /):
    """Open a listening socket and yield a server driving the httptools backend.

    Faster than the default h11 backend for header parsing. Requires the
    ``[http-fast]`` extra. For the default backend see
    :func:`localpost.http.start_http_server`.
    """

    def _factory(server: BaseServer, sock: socket.socket, addr: tuple[str, int]) -> HTTPConnHttptools:
        return HTTPConnHttptools(server, sock, addr)

    return start_http_server_base(config, handler, _factory)
