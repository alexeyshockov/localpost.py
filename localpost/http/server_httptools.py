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
from collections.abc import Buffer, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, BinaryIO, final

import httptools

from localpost.http._base import (
    BAD_REQUEST_WIRE,
    PAYLOAD_TOO_LARGE_WIRE,
    REASON_PHRASES,
    REQUEST_TIMEOUT_WIRE,
    BaseHTTPConn,
    BodyHandler,
    RequestHandler,
    Selector,
    _peek_disconnected,
    _send_all,
    content_length,
    emit_handler_error,
    native_stream,
    scan_response_headers,
)
from localpost.http._types import BodyTooLarge, InformationalResponse, Request, Response
from localpost.http.config import DEFAULT_BUFFER_SIZE

__all__ = ["HTTPConn"]


def _serialize_response(r: Response | InformationalResponse) -> bytes:
    """Serialise a status + headers block to wire bytes (no body)."""
    out = bytearray(b"HTTP/1.1 ")
    out += str(r.status_code).encode("ascii")
    out += b" "
    out += r.reason or REASON_PHRASES.get(r.status_code, b"")
    out += b"\r\n"
    for name, value in r.headers:
        out += name
        out += b": "
        out += value
        out += b"\r\n"
    out += b"\r\n"
    return bytes(out)


def _response_allows_body(request_method: bytes, status_code: int) -> bool:
    return request_method != b"HEAD" and not (100 <= status_code < 200 or status_code in {204, 304})


@final
@dataclass(slots=True, eq=False)
class HTTPConn(BaseHTTPConn):
    selector: Selector
    sock: socket.socket
    addr: tuple[str, int]
    handler: RequestHandler
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
    :class:`localpost.http._base.Selector` is the synchronisation
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
    """Request target. Most requests fit in one ``on_url`` call; for the rare
    fragmented case we accumulate via ``_cur_target_buf`` and flatten in
    ``on_headers_complete``."""
    _cur_target_buf: bytearray | None = None
    """Allocated lazily on the second ``on_url`` callback to merge fragments
    without creating a new ``bytes`` per fragment."""
    _cur_headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    _cur_expect_100: bool = False
    _cur_oversize: bool = False  # set by on_headers_complete if Content-Length exceeds cap

    # Per-conn dispatch state. One in-flight request at a time (no pipelining):
    # ``_ctx`` is built in ``on_headers_complete`` and the pre-body handler is
    # invoked inline. If it returns a continuation, ``_continuation`` is set
    # and ``on_body`` accumulates into ``_body_buf`` until ``on_message_complete``.
    _ctx: _HTTPReqCtx | None = None
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
    _deferred_streaming_dispatch: Callable[[], None] | None = None

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
        self._cur_target_buf = None
        self._cur_headers = []
        self._cur_expect_100 = False
        self._cur_oversize = False

    def on_url(self, url: bytes) -> None:
        if not self._cur_target:
            self._cur_target = url
            return
        # Second+ fragment: keep a bytearray so we don't allocate a new bytes
        # per fragment. Common case (single fragment) never enters this branch.
        if self._cur_target_buf is None:
            self._cur_target_buf = bytearray(self._cur_target)
        self._cur_target_buf += url

    def on_header(self, name: bytes, value: bytes) -> None:
        n = name.lower()
        # httptools strips leading OWS but leaves trailing SP/HTAB intact;
        # RFC 7230 §3.2.4 requires both sides stripped, and h11 does so. Trim
        # trailing OWS here to keep cross-backend parity.
        if value.endswith((b" ", b"\t")):
            value = value.rstrip(b" \t")
        self._cur_headers.append((n, value))
        if n == b"expect" and value.lower() == b"100-continue":
            self._cur_expect_100 = True

    def on_headers_complete(self) -> None:
        # Build the neutral Request envelope and dispatch the pre-body handler.
        method = self.parser.get_method()
        version = self.parser.get_http_version().encode("ascii")
        keep_alive = self.parser.should_keep_alive()

        # Flatten the multi-fragment target if the rare path was hit.
        if self._cur_target_buf is not None:
            self._cur_target = bytes(self._cur_target_buf)
            self._cur_target_buf = None

        # Eager content-length cap check.
        cl = content_length(self._cur_headers)
        if cl is not None and cl > self.selector.config.max_body_size:
            self._body_too_large = cl
            self._cur_oversize = True
            return

        # Pre-split the URL once. Manually find/slice — measured ~2x faster
        # than ``httptools.parse_url`` (which is C-level but pays Python
        # object-construction overhead per parse). The split is moved into
        # the backend so consumers (Router / wsgi) skip per-dispatch work.
        target = self._cur_target
        qix = target.find(b"?")
        if qix >= 0:
            path = target[:qix]
            query_string = target[qix + 1 :]
        else:
            path = target
            query_string = b""

        # ``method`` and ``self._cur_target`` are already ``bytes`` (httptools
        # callbacks deliver real ``bytes``, not memoryview/bytearray); no copy.
        # httptools rejects lowercase methods at parse time, so ``method`` is
        # already uppercase ASCII.
        req = Request(
            method=method,
            target=self._cur_target,
            path=path,
            query_string=query_string,
            headers=self._cur_headers,
            http_version=version,
        )
        ctx = _HTTPReqCtx(
            self.selector,
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
        try:
            result = self.handler(ctx)
        except BodyTooLarge:
            raise
        except Exception:
            self.selector.logger.exception("Handler raised for %s %r", req.method, req.target)
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
            if new_total > self.selector.config.max_body_size:
                self._body_too_large = new_total
                return
            self._streaming_body_buf += data
            return
        if self._continuation is None or self._cur_oversize:
            return  # no body wanted (handler done) or already oversize
        new_total = len(self._body_buf) + len(data)
        if new_total > self.selector.config.max_body_size:
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
                self.selector.logger.exception("Body handler raised for %s %r", ctx.request.method, ctx.request.target)
                emit_handler_error(ctx)
        self._message_complete = True

    # ----- BaseHTTPConn surface -----

    def __call__(self, _sel: Selector, /) -> None:
        try:
            self._loop()
        except _ProtocolError as e:
            self.selector.logger.warning("Bad client input from %s: %s", self.addr, e)
            self._try_send_wire(BAD_REQUEST_WIRE)
            self.close()
        except BodyTooLarge:
            self.selector.logger.warning(
                "Request body from %s exceeds max_body_size=%d", self.addr, self.selector.config.max_body_size
            )
            self._try_send_wire(PAYLOAD_TOO_LARGE_WIRE)
            self.close()

    def _feed(self, data: bytes) -> None:
        try:
            self.parser.feed_data(data)
        except httptools.HttpParserUpgrade as e:
            self._deferred_streaming_dispatch = None
            raise _ProtocolError(f"HTTP upgrade not supported: {e}") from e
        except httptools.HttpParserError as e:
            self._deferred_streaming_dispatch = None
            raise _ProtocolError(str(e)) from e
        if self._body_too_large is not None:
            n = self._body_too_large
            self._body_too_large = None
            self._deferred_streaming_dispatch = None
            raise BodyTooLarge(n)
        if self._deferred_streaming_dispatch is not None:
            dispatch = self._deferred_streaming_dispatch
            self._deferred_streaming_dispatch = None
            dispatch()

    def _loop(self) -> None:
        config = self.selector.config
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
        self._deferred_streaming_dispatch = None
        self.idle = True
        self.close_at = time.monotonic() + self.selector.config.keep_alive_timeout

    def _try_send_wire(self, wire: bytes) -> None:
        """Best-effort: write a pre-serialised status+headers+body block.

        Used for canned protocol-error responses (400 / 408 / 413). The wire
        bytes are pre-built at module import time (see ``_base.py``) so this
        path skips ``_serialize_response`` entirely.
        """
        if self._response_started:
            return
        try:
            _send_all(self, wire)
        except Exception:  # noqa: BLE001, S110 — connection is being closed anyway
            pass

    def emit_stale_408(self) -> None:
        """Stalled mid-request → 408. Idle keep-alive → silently dropped."""
        if self.idle or self._response_started:
            return
        try:
            _send_all(self, REQUEST_TIMEOUT_WIRE)
        except Exception:  # noqa: BLE001, S110 — the conn is being torn down anyway
            pass


class _ProtocolError(Exception):
    """Translated httptools parser error. Mapped to 400 by the conn loop."""


@dataclass(slots=True, eq=False)
class _HTTPReqCtx:
    """Per-request context for the httptools backend.

    The response-write path auto-buffers: ``start_response`` stores the
    serialised status + headers without flushing; the next ``send`` (or
    ``finish_response`` for empty bodies) emits a single ``sendall`` with
    headers + body framed together. One syscall for the canonical
    ``ctx.complete(response, body)`` path; two for SSE (headers + first
    chunk together; one per subsequent chunk).
    """

    selector: Selector
    conn: HTTPConn
    request: Request
    _expect_100_continue: bool = False
    _keep_alive: bool = True

    body: bytes = b""
    response_status: int | None = None
    attrs: dict[Any, Any] = field(default_factory=dict)
    _disconnected: bool = False
    _continue_sent: bool = False
    _chunked: bool = False
    """``True`` if the backend auto-added ``Transfer-Encoding: chunked`` because
    the response had neither ``Content-Length`` nor an explicit
    ``Transfer-Encoding`` — chunks must be framed and a terminator written."""
    _pending_header_bytes: bytes | None = None
    """Buffered status line + headers, awaiting flush on first body chunk
    or ``finish_response``. ``None`` once flushed."""
    _body_allowed: bool = True
    """False for HEAD / 1xx / 204 / 304 responses, where response body bytes
    must not be written even if the handler passes a body to ``complete``."""

    @property
    def remote_addr(self) -> str | None:
        host, port = self.conn.addr
        return f"{host}:{port}" if host else None

    @property
    def local_addr(self) -> str:
        sel = self.selector
        return f"{sel.config.host}:{sel.port}"

    @property
    def scheme(self) -> str:
        # No TLS support today; native server is plain HTTP.
        return "http"

    @property
    def disconnected(self) -> bool:
        if self._disconnected:
            return True
        if _peek_disconnected(self.conn.sock):
            self._disconnected = True
            return True
        return False

    @property
    def borrowed(self) -> bool:
        return not self.conn.tracked

    @contextmanager
    def borrow(self) -> Iterator[_HTTPReqCtx]:
        assert not self.borrowed
        self.selector.stop_tracking(self.conn)
        try:
            yield self
        finally:
            self._maybe_give_back()

    def _defer_streaming_dispatch(self, dispatcher: Callable[[_HTTPReqCtx], None]) -> None:
        """Start a streaming worker after the current parser feed returns.

        httptools fires callbacks from inside ``parser.feed_data``. Starting
        the worker directly from ``on_headers_complete`` would let it call
        ``ctx.receive`` and re-enter the parser while the selector thread is
        still inside the same feed. Instead, mark streaming active now so any
        body bytes from the current packet are buffered by ``on_body``, then
        let ``_feed`` run the handoff once callbacks are done.
        """
        self.conn._streaming_active = True
        self.conn._deferred_streaming_dispatch = lambda: dispatcher(self)

    def _maybe_give_back(self) -> None:
        if not self.borrowed:
            return
        # If a fallback path inside this request flipped the socket to
        # blocking-with-timeout, reset to non-blocking before handing the
        # conn back to the selector — the selector loop assumes a
        # non-blocking socket. ``gettimeout`` reads cached state on the
        # socket object (no syscall), so the no-fallback common case
        # pays nothing.
        sock = self.conn.sock
        if sock.gettimeout() != 0:
            try:
                sock.settimeout(0)
            except OSError:
                pass
        self.selector.track(self.conn)

    def complete(self, response: Response, body: bytes | None = None) -> None:
        self.start_response(response)
        if body is not None:
            self.send(body)
        self.finish_response()

    def stream(self, response: Response, chunks: Iterator[bytes], /) -> None:
        native_stream(self, response, chunks)

    def sendfile(self, response: Response, file: BinaryIO, offset: int, count: int) -> None:
        # ``Content-Length`` framing is required: chunked needs per-chunk
        # framing bytes that ``socket.sendfile`` can't produce, and a
        # mismatched Content-Length corrupts the wire stream.
        declared: int | None = None
        for name, value in response.headers:
            n = name.lower()
            if n == b"transfer-encoding":
                raise ValueError("sendfile requires Content-Length framing (no Transfer-Encoding)")
            if n == b"content-length":
                try:
                    declared = int(value)
                except ValueError as e:
                    raise ValueError("Content-Length is not a valid integer") from e
        if declared != count:
            raise ValueError("sendfile requires Content-Length matching ``count``")
        self.start_response(response)
        if self._pending_header_bytes is not None:
            _send_all(self.conn, self._pending_header_bytes)
            self._pending_header_bytes = None
        sock = self.conn.sock
        sock.settimeout(self.selector.config.rw_timeout)
        try:
            sock.sendfile(file, offset=offset, count=count)
        finally:
            if self.conn.tracked:
                try:
                    sock.settimeout(0)
                except OSError:
                    pass
        # No EOM / chunked terminator with Content-Length framing — just
        # advance the conn lifecycle (give back / close-after-response).
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

        conn = self.conn
        rw = self.selector.config.rw_timeout

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
        _send_all(self.conn, b"HTTP/1.1 100 Continue\r\n\r\n")
        self._continue_sent = True

    def start_response(self, response: Response | InformationalResponse, /) -> None:
        # State updates first (response_status, _close_after_response, _chunked
        # for the Response case); for Informational responses we just write
        # bytes without buffering (rare path).
        if isinstance(response, Response):
            self.response_status = response.status_code
            self.conn._response_started = True
            self._body_allowed = _response_allows_body(self.request.method, response.status_code)
            has_close, has_framing, has_chunked = scan_response_headers(response.headers)
            if has_close or not self._keep_alive:
                self.conn._close_after_response = True
            if self._body_allowed:
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
                elif has_chunked:
                    # User / middleware supplied ``Transfer-Encoding: chunked``
                    # explicitly. Skip auto-add but still flip ``_chunked`` so
                    # ``send`` frames each chunk — otherwise the header would
                    # advertise chunked while the body went out raw, corrupting
                    # the wire.
                    self._chunked = True
            self._pending_header_bytes = _serialize_response(response)
        else:
            # Informational responses (e.g., 100 Continue) flush immediately.
            _send_all(self.conn, _serialize_response(response))

    def send(self, chunk: Buffer, /) -> None:
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        if not self._body_allowed:
            if self._pending_header_bytes is not None:
                _send_all(self.conn, self._pending_header_bytes)
                self._pending_header_bytes = None
            return
        if not chunk and self._pending_header_bytes is None:
            return
        framed = (f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n") if self._chunked and chunk else chunk
        if self._pending_header_bytes is not None:
            # Headers + first body chunk in one syscall.
            _send_all(self.conn, self._pending_header_bytes + framed)
            self._pending_header_bytes = None
        elif framed:
            _send_all(self.conn, framed)

    def finish_response(self) -> None:
        # Flush any still-buffered headers (empty-body case) plus the
        # chunked terminator, in one sendall.
        terminator = b"0\r\n\r\n" if self._chunked else b""
        if self._pending_header_bytes is not None:
            _send_all(self.conn, self._pending_header_bytes + terminator)
            self._pending_header_bytes = None
        elif terminator:
            _send_all(self.conn, terminator)
        self._maybe_give_back()
