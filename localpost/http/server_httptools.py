"""HTTP/1.1 server backend driven by ``httptools`` (llhttp wrapper).

Faster, opt-in alternative to the default :mod:`localpost.http.server_h11`
backend. httptools is a parse-only C extension — response bytes and
keep-alive bookkeeping are handled here.

Scope:

- request body is buffered in full into ``ctx.body`` before the
  :data:`BodyHandler` continuation is invoked (JSON-API common case)
- ``Expect: 100-continue`` is sent inline only when the pre-body
  :data:`RequestHandler` returns a continuation (i.e., the body is
  actually wanted)
- keep-alive driven by ``parser.should_keep_alive()`` and response
  headers
- HTTP/1.1 pipelining is **not supported**: pipelined clients are served
  one request at a time on the same connection (no parallelism gained,
  no incorrect interleaving emitted)
- response framing: ``Content-Length`` (set by the caller) or auto
  ``Transfer-Encoding: chunked`` if neither is present

Out of scope: HTTP upgrade negotiation (WebSockets), HTTP/2.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Buffer, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, final

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
    BodyHandler,
    RequestHandler,
    _send_all,
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


def _scan_response_headers(headers: list[tuple[bytes, bytes]]) -> tuple[bool, bool]:
    """One-pass scan: ``(has_connection_close, has_content_length_or_te)``.

    Combined to avoid two walks per response.
    """
    has_close = False
    has_framing = False
    for name, value in headers:
        n = name.lower()
        if n == b"connection" and b"close" in value.lower():
            has_close = True
        elif n in {b"content-length", b"transfer-encoding"}:
            has_framing = True
    return has_close, has_framing


@final
@dataclass(eq=False, slots=True)
class HTTPConnHttptools(BaseHTTPConn):
    server: BaseServer
    sock: socket.socket
    addr: tuple[str, int]
    fd: int = field(init=False)
    parser: httptools.HttpRequestParser = field(init=False)
    """The httptools (llhttp) parser — parse-only on our side; the
    response wire bytes are hand-built via ``_serialize_response``
    plus ``_send_all`` (no parser involved on the response path).

    **Single-thread invariant.** The selector owns the parser during
    ``parser.feed_data`` (and the callbacks it fires) for the
    request-headers phase; on streaming routes (``buffer_body=False``)
    the worker also calls ``parser.feed_data`` from inside
    ``ctx.receive`` to drain remaining body bytes. The op-queue /
    wakeup-pipe handoff in
    :class:`localpost.http._base.BaseServer` is the synchronisation
    edge — `os.write` to the wakeup pipe is a full memory barrier.
    The parser is **never** touched concurrently from two threads;
    ownership is strict (selector → worker on ``stop_tracking``;
    worker → selector on ``track``).
    """
    close_at: float | None = None
    tracked: bool = False
    idle: bool = True

    # Per-request scratch state populated by the parser callbacks.
    _cur_target: bytes = b""
    _cur_headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    _cur_expect_100: bool = False
    _cur_oversize: bool = False  # set by on_headers_complete if Content-Length exceeds cap

    # Per-conn dispatch state. One in-flight request at a time (no pipelining):
    # ``_ctx`` is built in ``on_headers_complete`` and the pre-body handler is
    # invoked inline. If it returns a continuation, ``_continuation`` is set
    # and ``on_body`` accumulates into ``_body_buf`` until ``on_message_complete``.
    _ctx: HTTPReqCtxHttptools | None = None
    _continuation: BodyHandler | None = None
    _body_buf: bytearray = field(default_factory=bytearray)
    _message_complete: bool = False
    _body_too_large: int | None = None

    # Streaming mode (``buffer_body=False``): the pre-body handler borrowed
    # the conn before the body was buffered. Selector + worker both feed
    # bytes through ``parser.feed_data``; ``on_body`` populates
    # ``_streaming_body_buf`` regardless of which thread is feeding. The
    # worker drains the buffer via ``ctx.receive``. Same model as h11
    # (``next_event``-driven), just emulated with httptools' push
    # callbacks.
    _streaming_active: bool = False
    _streaming_body_buf: bytearray = field(default_factory=bytearray)
    _streaming_eom: bool = False

    # Set when the response indicates ``Connection: close`` or the request lacked
    # keep-alive support — the conn is closed once finish_response returns.
    _close_after_response: bool = False
    _response_started: bool = False

    # Set by ``__call__`` so parser callbacks can dispatch the pre-body handler.
    _handler: RequestHandler | None = None

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
        # Build the neutral Request envelope and dispatch the pre-body handler.
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
        ctx = HTTPReqCtxHttptools(
            self.server,
            self,
            req,
            _expect_100_continue=self._cur_expect_100,
            _keep_alive=keep_alive,
        )
        self._ctx = ctx
        self._body_buf = bytearray()
        self._message_complete = False
        self._continuation = None

        # Dispatch the pre-body handler inline. It runs on the selector
        # thread; it can call ``ctx.complete(...)`` (response sent inline,
        # returns None), ``ctx.borrow()`` (escape to worker, returns None),
        # or return a :data:`BodyHandler` continuation to receive the body.
        assert self._handler is not None
        try:
            result = self._handler(ctx)
        except BodyTooLarge:
            raise
        except Exception:
            self.server.logger.exception("Handler raised for %s %r", req.method, req.target)
            emit_handler_error(ctx)
            result = None

        if result is None:
            # Either the handler completed inline OR a streaming
            # pool dispatched and borrowed the conn. Detect the latter
            # so on_body / on_message_complete know to populate the
            # streaming body buffer (drained by the worker via
            # ``ctx.receive``) instead of dropping bytes.
            if not self.tracked:
                self._streaming_active = True
            return

        # Continuation returned: we'll buffer the body and invoke it on
        # ``on_message_complete``. If the client sent ``Expect: 100-continue``,
        # tell them to actually send the body now.
        self._continuation = result
        if self._cur_expect_100 and not ctx._continue_sent:
            try:
                self.sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                object.__setattr__(ctx, "_continue_sent", True)
            except OSError:
                # Best-effort; the body recv loop will surface the failure.
                pass

    def on_body(self, data: bytes) -> None:
        if self._streaming_active:
            # Streaming mode: append to the worker-drained buffer regardless
            # of which thread is currently inside ``feed_data``. ``ctx.receive``
            # drains it (and pulls more bytes from the socket if needed).
            new_total = len(self._streaming_body_buf) + len(data)
            if new_total > self.server.config.max_body_size:
                self._body_too_large = new_total
                return
            self._streaming_body_buf += data
            return
        if self._continuation is None or self._cur_oversize:
            return  # no body wanted (handler done) or already oversize
        new_total = len(self._body_buf) + len(data)
        if new_total > self.server.config.max_body_size:
            self._body_too_large = new_total
            return
        self._body_buf += data

    def on_message_complete(self) -> None:
        if self._streaming_active:
            self._streaming_eom = True
            self._message_complete = True
            return
        if self._continuation is not None:
            cont = self._continuation
            self._continuation = None
            ctx = self._ctx
            assert ctx is not None
            ctx.body = bytes(self._body_buf)
            try:
                cont(ctx)
            except BodyTooLarge:
                raise
            except Exception:
                self.server.logger.exception(
                    "Body handler raised for %s %r", ctx.request.method, ctx.request.target
                )
                emit_handler_error(ctx)
        self._message_complete = True

    # ----- BaseHTTPConn surface -----

    def __call__(self, h: RequestHandler, /) -> None:
        self._handler = h
        try:
            self._loop()
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

    def _loop(self) -> None:
        config = self.server.config
        while self.tracked:
            # Did the previous request just complete? Either roll into the
            # next one (keep-alive) or close.
            if self._message_complete:
                ctx = self._ctx
                assert ctx is not None
                if ctx.borrowed:
                    return  # worker has the conn now
                if not self.tracked:
                    return  # closed during error recovery
                if self._close_after_response or not ctx._keep_alive:
                    self.close()
                    return
                self._reset_for_next_request()
                # Fall through to read more bytes (or to dispatch a request
                # whose headers were already buffered by the parser).
                continue

            # Pump bytes from the socket. Parser callbacks fire inline:
            # ``on_headers_complete`` dispatches the pre-body handler;
            # ``on_message_complete`` invokes the continuation if any and
            # sets ``_message_complete``.
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
        # Per-request parsing scratch is cleared by ``on_message_begin``.
        self._ctx = None
        self._continuation = None
        self._body_buf = bytearray()
        self._message_complete = False
        self._response_started = False
        self._close_after_response = False
        self._streaming_active = False
        self._streaming_body_buf = bytearray()
        self._streaming_eom = False
        self.idle = True
        self.close_at = time.monotonic() + self.server.config.keep_alive_timeout

    def _try_send_status(self, response: Response, body: bytes) -> None:
        """Best-effort: write a status line + body if we haven't started a response yet."""
        if self._response_started:
            return
        try:
            _send_all(self, _serialize_response(response) + body)
        except Exception:  # noqa: BLE001, S110 — connection is being closed anyway
            pass

    def emit_stale_408(self) -> None:
        """Stalled mid-request → 408. Idle keep-alive → silently dropped."""
        if self.idle or self._response_started:
            return
        try:
            _send_all(self, _serialize_response(REQUEST_TIMEOUT_RESPONSE) + REQUEST_TIMEOUT_BODY)
        except Exception:  # noqa: BLE001, S110 — the conn is being torn down anyway
            pass


class _ProtocolError(Exception):
    """Translated httptools parser error. Mapped to 400 by the conn loop."""


@dataclass(eq=False, slots=True)
class HTTPReqCtxHttptools:
    """Per-request context for the httptools backend.

    The response-write path auto-buffers: ``start_response`` stores the
    serialised status + headers without flushing; the next ``send`` (or
    ``finish_response`` for empty bodies) emits a single ``sendall`` with
    headers + body framed together. One syscall for the canonical
    ``ctx.complete(response, body)`` path; two for SSE (headers + first
    chunk together; one per subsequent chunk).
    """

    _server: BaseServer
    _conn: HTTPConnHttptools
    request: Request
    _expect_100_continue: bool = False
    _keep_alive: bool = True

    body: bytes = b""
    response_status: int | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    _continue_sent: bool = False
    _chunked: bool = False
    """``True`` if the backend auto-added ``Transfer-Encoding: chunked`` because
    the response had neither ``Content-Length`` nor an explicit
    ``Transfer-Encoding`` — chunks must be framed and a terminator written."""
    _pending_header_bytes: bytes | None = None
    """Buffered status line + headers, awaiting flush on first body chunk
    or ``finish_response``. ``None`` once flushed."""

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
        if not self.borrowed:
            return
        # If a fallback path inside this request flipped the socket to
        # blocking-with-timeout, reset to non-blocking before handing the
        # conn back to the selector — the selector loop assumes a
        # non-blocking socket. ``gettimeout`` reads cached state on the
        # socket object (no syscall), so the no-fallback common case
        # pays nothing.
        sock = self._conn.sock
        if sock.gettimeout() != 0:
            try:
                sock.settimeout(0)
            except OSError:
                pass
        self._server.track(self._conn)

    def complete(self, response: Response, body: bytes | None = None) -> None:
        self.start_response(response)
        if body is not None:
            self.send(body)
        self.finish_response()

    def receive(self, size: int = DEFAULT_BUFFER_SIZE, /) -> bytes:
        """Streaming-read API.

        Two paths:

        - **Streaming route** (``buffer_body=False``, ``conn._streaming_active``):
          parser-driven, same shape as h11. ``on_body`` callbacks (fired
          from ``parser.feed_data`` calls on either thread) populate
          ``conn._streaming_body_buf``; this method drains it. Pulls more
          bytes from the socket and feeds them through the parser if the
          buffer is empty and EOM hasn't fired.
        - **Buffered route**: the body has already been buffered into
          ``ctx.body`` before this body handler ran. ``ctx.receive`` is
          the legacy fallback path (rare); it just calls ``sock.recv``
          for callers that did a hand-rolled ``borrow()``.
        """
        if self._expect_100_continue and not self._continue_sent:
            self._send_continue()

        conn = self._conn
        rw = self._server.config.rw_timeout

        if conn._streaming_active:
            while not conn._streaming_body_buf and not conn._streaming_eom:
                try:
                    data = conn.sock.recv(DEFAULT_BUFFER_SIZE)
                except BlockingIOError:
                    conn.sock.settimeout(rw)
                    try:
                        data = conn.sock.recv(DEFAULT_BUFFER_SIZE)
                    finally:
                        # On a borrowed conn keep the socket blocking-with-timeout
                        # for the rest of the request; subsequent ``recv`` calls
                        # in this loop (and in later sends) skip the
                        # BlockingIOError path entirely. The give-back path
                        # resets the socket on hand-back. On the selector thread
                        # we restore inline.
                        if conn.tracked:
                            conn.sock.settimeout(0)
                if not data:
                    break  # peer FIN mid-body
                # Feed through the parser; ``on_body`` populates the
                # streaming buffer, ``on_message_complete`` flips EOM.
                # Errors (BodyTooLarge / _ProtocolError) propagate to the
                # caller; the worker pool's exception handler emits a 500.
                conn._feed(data)
            if conn._streaming_body_buf:
                n = min(size, len(conn._streaming_body_buf))
                chunk = bytes(conn._streaming_body_buf[:n])
                del conn._streaming_body_buf[:n]
                return chunk
            return b""

        # Buffered route fallback (hand-rolled borrow): raw recv.
        try:
            return conn.sock.recv(size)
        except BlockingIOError:
            conn.sock.settimeout(rw)
            try:
                return conn.sock.recv(size)
            finally:
                if conn.tracked:
                    conn.sock.settimeout(0)

    def _send_continue(self) -> None:
        _send_all(self._conn, b"HTTP/1.1 100 Continue\r\n\r\n")
        self._continue_sent = True

    def start_response(self, response: Response | InformationalResponse, /) -> None:
        # State updates first (response_status, _close_after_response, _chunked
        # for the Response case); for Informational responses we just write
        # bytes without buffering (rare path).
        if isinstance(response, Response):
            self.response_status = response.status_code
            self._conn._response_started = True
            has_close, has_framing = _scan_response_headers(response.headers)
            if has_close or not self._keep_alive:
                self._conn._close_after_response = True
            if not has_framing:
                # Auto-frame: no Content-Length / Transfer-Encoding → chunked.
                # Without framing, an HTTP/1.1 client would wait for the
                # connection to close before considering the response done.
                response = Response(
                    status_code=response.status_code,
                    headers=[*response.headers, (b"transfer-encoding", b"chunked")],
                    reason=response.reason,
                )
                self._chunked = True
            self._pending_header_bytes = _serialize_response(response)
        else:
            # Informational responses (e.g., 100 Continue) flush immediately.
            _send_all(self._conn, _serialize_response(response))

    def send(self, chunk: Buffer, /) -> None:
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        if not chunk and self._pending_header_bytes is None:
            return
        framed = (f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n") if self._chunked and chunk else chunk
        if self._pending_header_bytes is not None:
            # Headers + first body chunk in one syscall.
            _send_all(self._conn, self._pending_header_bytes + framed)
            self._pending_header_bytes = None
        elif framed:
            _send_all(self._conn, framed)

    def finish_response(self) -> None:
        # Flush any still-buffered headers (empty-body case) plus the
        # chunked terminator, in one sendall.
        terminator = b"0\r\n\r\n" if self._chunked else b""
        if self._pending_header_bytes is not None:
            _send_all(self._conn, self._pending_header_bytes + terminator)
            self._pending_header_bytes = None
        elif terminator:
            _send_all(self._conn, terminator)
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
