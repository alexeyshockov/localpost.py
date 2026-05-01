"""HTTP/1.1 server backend driven by h11.

Pure-Python parser/state-machine. The default backend; readable, no C deps.
For the C-based alternative see :mod:`localpost.http.server_httptools`.

Like the httptools backend, this one:

- buffers the full request body into ``ctx.body`` before invoking a
  returned :data:`BodyHandler` continuation
- auto-buffers the response headers; the next ``send`` (or
  ``finish_response`` for empty bodies) emits headers + first body chunk
  in a single ``sendall``
- does not parallelise pipelined requests on the same connection
  (sequential serving via h11's own state machine)
"""

from __future__ import annotations

import socket
import time
from collections.abc import Buffer, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, cast, final

import h11

from localpost.http._base import (
    BAD_REQUEST_BODY,
    BAD_REQUEST_RESPONSE,
    INTERNAL_ERROR_BODY,
    INTERNAL_ERROR_RESPONSE,
    PAYLOAD_TOO_LARGE_BODY,
    PAYLOAD_TOO_LARGE_RESPONSE,
    REQUEST_TIMEOUT_BODY,
    REQUEST_TIMEOUT_RESPONSE,
    BaseHTTPConn,
    BodyHandler,
    RequestHandler,
    Selector,
    _send_all,
    emit_handler_error,
)
from localpost.http._types import BodyTooLarge, InformationalResponse, Request, Response
from localpost.http.config import DEFAULT_BUFFER_SIZE

__all__ = ["HTTPConn", "HTTPReqCtxH11"]


def _to_h11_response(r: Response | InformationalResponse) -> h11.Response | h11.InformationalResponse:
    headers = cast(list, r.headers)
    if isinstance(r, Response):
        return h11.Response(status_code=r.status_code, headers=headers, reason=r.reason)
    return h11.InformationalResponse(status_code=r.status_code, headers=headers, reason=r.reason)


def _content_length(headers) -> int | None:
    # h11 normalizes header names to lowercase bytes — direct equality is enough.
    for name, value in headers:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _has_response_framing(headers) -> bool:
    return any(name.lower() in {b"content-length", b"transfer-encoding"} for name, _ in headers)


@final
@dataclass(eq=False, slots=True)
class HTTPConn(BaseHTTPConn):
    selector: Selector
    sock: socket.socket
    addr: tuple[str, int]
    handler: RequestHandler
    fd: int = field(init=False)
    """The integer file descriptor captured at construction time. Used to
    clean up ``selector._fd_to_key`` after ``sock.close()`` (where
    ``sock.fileno()`` returns -1)."""
    recv_closed: bool = False
    parser: h11.Connection = field(default_factory=lambda: h11.Connection(h11.SERVER))
    """The h11 state machine — used for **both** parsing the request
    (``parser.next_event`` / ``parser.receive_data``) and serialising
    the response (``parser.send``).

    **Single-thread invariant.** The selector owns the parser from
    ``__call__`` entry until ``stop_tracking`` (in the
    :data:`BodyHandler` dispatcher); the worker owns it from then until
    ``track`` re-registers the conn. The op-queue / wakeup-pipe
    handoff in :class:`localpost.http._base.Selector` is the
    synchronisation edge — `os.write` to the wakeup pipe is a full
    memory barrier across threads. The parser is **never** touched
    concurrently from two threads.
    """
    close_at: float | None = None
    tracked: bool = False
    body_bytes_received: int = 0
    """Cumulative body bytes received for the current request — reset on
    ``parser.start_next_cycle``. Compared against ``ServerConfig.max_body_size``
    to enforce the upload cap."""
    idle: bool = True

    # Per-request dispatch state. Set on ``h11.Request``, used while we
    # iterate ``h11.Data`` events to accumulate the body, and consumed on
    # ``h11.EndOfMessage`` to fire the continuation. ``_ctx`` is the
    # current request context shared with the handler; ``_continuation``
    # is the post-body callback (None if the pre-body handler completed
    # inline or borrowed).
    _ctx: HTTPReqCtxH11 | None = None
    _continuation: BodyHandler | None = None
    _body_buf: bytearray = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        self.fd = self.sock.fileno()

    def receive(self, size: int = DEFAULT_BUFFER_SIZE, /) -> None:
        data = self.sock.recv(size)
        self.parser.receive_data(data)
        if not data:
            self.recv_closed = True
        else:
            self.idle = False

    def send(self, event: h11.InformationalResponse | h11.Response | h11.Data | h11.EndOfMessage) -> None:
        payload = self.parser.send(event)
        if payload is None:
            return
        _send_all(self, payload)

    def __call__(self, _sel: Selector, /) -> None:
        try:
            self._loop()
        except h11.RemoteProtocolError as e:
            self.selector.logger.warning("Bad client input from %s: %s", self.addr, e)
            self._try_send_status(BAD_REQUEST_RESPONSE, BAD_REQUEST_BODY)
            self.close()
        except h11.LocalProtocolError:
            self.selector.logger.exception("Local protocol error from %s", self.addr)
            self._try_send_status(INTERNAL_ERROR_RESPONSE, INTERNAL_ERROR_BODY)
            self.close()
        except BodyTooLarge:
            self.selector.logger.warning(
                "Request body from %s exceeds max_body_size=%d", self.addr, self.selector.config.max_body_size
            )
            self._try_send_status(PAYLOAD_TOO_LARGE_RESPONSE, PAYLOAD_TOO_LARGE_BODY)
            self.close()

    def _loop(self) -> None:
        parser = self.parser
        h = self.handler

        while self.tracked:
            if parser.our_state is h11.MUST_CLOSE:
                self.close()
                return
            if parser.our_state is h11.DONE and parser.their_state is h11.DONE:
                parser.start_next_cycle()
                self.body_bytes_received = 0
                self.idle = True
                self.close_at = time.monotonic() + self.selector.config.keep_alive_timeout
                # Per-request state from previous cycle is no longer relevant.
                self._ctx = None
                self._continuation = None
                self._body_buf = bytearray()

            event = parser.next_event()

            if event is h11.NEED_DATA:
                if parser.they_are_waiting_for_100_continue:
                    if self._continuation is not None:
                        # Body is wanted (handler returned a continuation) —
                        # tell the client to send it. Fall through to recv.
                        self.send(h11.InformationalResponse(status_code=100, headers=[], reason="Continue"))
                    else:
                        # No body needed — short-circuit with 417.
                        self.send(h11.Response(status_code=417, headers=[], reason="Expectation Failed"))
                        continue
                try:
                    self.receive()
                except BlockingIOError:
                    return  # Wait for it in the selector
                self.close_at = time.monotonic() + self.selector.config.rw_timeout
            elif isinstance(event, h11.Data):
                self.body_bytes_received += len(event.data)
                if self.body_bytes_received > self.selector.config.max_body_size:
                    raise BodyTooLarge(self.body_bytes_received)
                if self._continuation is not None:
                    self._body_buf += event.data
                # Else: pre-body handler returned None — drain silently.
            elif isinstance(event, h11.EndOfMessage):
                if self._continuation is not None:
                    cont = self._continuation
                    ctx = self._ctx
                    assert ctx is not None
                    self._continuation = None
                    ctx.body = bytes(self._body_buf)
                    self._body_buf = bytearray()
                    try:
                        cont(ctx)
                    except BodyTooLarge:
                        raise
                    except Exception:
                        self.selector.logger.exception(
                            "Body handler raised for %s %r", ctx.request.method, ctx.request.target
                        )
                        emit_handler_error(ctx)
                    if ctx.borrowed:
                        return
                    if not self.tracked:
                        return
            elif isinstance(event, h11.Request):
                cl = _content_length(event.headers)
                if cl is not None and cl > self.selector.config.max_body_size:
                    raise BodyTooLarge(cl)
                # h11 hands us ``bytes`` for method/target/version and a list
                # of ``(bytes, bytes)`` tuples for headers. ``bytes(b)`` for an
                # already-``bytes`` argument returns the same object, so the
                # wraps were no-ops; the per-tuple comprehension was the only
                # real cost. ``list(event.headers)`` is a shallow copy that
                # insulates Request from h11's per-event Headers subclass.
                # h11 is lenient on method case (per RFC the method is
                # case-sensitive but most clients send uppercase); normalise
                # here so consumers can rely on it.
                target = event.target
                qix = target.find(b"?")
                if qix >= 0:
                    path = target[:qix]
                    query_string = target[qix + 1 :]
                else:
                    path = target
                    query_string = b""
                method = event.method
                if not method.isupper():
                    method = method.upper()
                req = Request(
                    method=method,
                    target=target,
                    path=path,
                    query_string=query_string,
                    headers=list(event.headers),
                    http_version=event.http_version,
                )
                ctx = HTTPReqCtxH11(self.selector, self, req)
                self._ctx = ctx
                self._body_buf = bytearray()
                self._continuation = None
                try:
                    result = h(ctx)
                except BodyTooLarge:
                    raise
                except Exception:
                    self.selector.logger.exception("Handler raised for %s %r", event.method, event.target)
                    emit_handler_error(ctx)
                    result = None
                if result is not None:
                    self._continuation = result
                if ctx.borrowed:
                    return
                if not self.tracked:
                    return  # connection was closed during error recovery
            elif isinstance(event, h11.ConnectionClosed):
                self.selector.logger.debug("Client closed connection")
                self.close()
                return
            else:
                raise RuntimeError(f"Unexpected {event!r} in the connection loop")

    def _try_send_status(self, response: Response, body: bytes) -> None:
        """Best-effort: try to send a response if the parser is still in a writable state.

        Used as a recovery path when the connection is about to be closed due to a
        protocol error. Failures are silently swallowed.
        """
        if self.parser.our_state is not h11.IDLE and self.parser.our_state is not h11.SEND_RESPONSE:
            return
        try:
            self.send(_to_h11_response(response))
            if body:
                self.send(h11.Data(data=body))
            self.send(h11.EndOfMessage())
        except Exception:  # noqa: BLE001, S110 — connection is being closed anyway
            pass

    def emit_stale_408(self) -> None:
        """Stalled mid-request → 408. Idle keep-alive → silently dropped."""
        if self.idle or self.parser.our_state is not h11.IDLE:
            return
        try:
            payload = self.parser.send(_to_h11_response(REQUEST_TIMEOUT_RESPONSE))
            if payload:
                _send_all(self, payload)
            payload = self.parser.send(h11.Data(data=REQUEST_TIMEOUT_BODY))
            if payload:
                _send_all(self, payload)
            payload = self.parser.send(h11.EndOfMessage())
            if payload:
                _send_all(self, payload)
        except Exception:  # noqa: BLE001, S110 — the conn is being torn down anyway
            pass


@dataclass(eq=False, slots=True)
class HTTPReqCtxH11:
    """Per-request context for the h11 backend.

    Structurally satisfies :class:`localpost.http._base.HTTPReqCtx`.

    The response-write path auto-buffers: ``start_response`` advances h11
    and stashes the returned bytes; the next ``send`` (or
    ``finish_response`` for empty bodies) emits headers + first body
    chunk in a single ``sendall``.
    """

    selector: Selector
    conn: HTTPConn
    request: Request

    body: bytes = b""
    response_status: int | None = None
    attrs: dict[Any, Any] = field(default_factory=dict)
    _pending_header_bytes: bytes | None = None

    @property
    def borrowed(self) -> bool:
        return not self.conn.tracked

    @contextmanager
    def borrow(self) -> Iterator[HTTPReqCtxH11]:
        """Switch the conn out of selector tracking for the duration of the block."""
        assert not self.borrowed
        self.selector.stop_tracking(self.conn)
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
        # non-blocking socket and a stray BlockingIOError is its only
        # "no more data" signal. ``gettimeout`` reads cached state on the
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

    def receive(self, size: int = DEFAULT_BUFFER_SIZE, /) -> bytes:
        """Streaming-read API. Rare under the JSON-API contract — typical
        callers receive the buffered body via ``ctx.body``. After the
        :data:`BodyHandler` continuation runs, the body has been fully
        consumed and the parser's state side reads ``b""``."""
        parser = self.conn.parser
        if parser.their_state is h11.DONE:
            return b""
        if parser.they_are_waiting_for_100_continue:
            self.conn.send(h11.InformationalResponse(status_code=100, headers=[], reason="Continue"))
        while True:
            event = parser.next_event()
            if event is h11.NEED_DATA:
                # Sync handlers can't tolerate BlockingIOError on a non-blocking
                # socket: switch to a blocking read bounded by ``rw_timeout``.
                # On a borrowed conn we leave the socket blocking-with-timeout
                # so the next iteration's ``recv`` skips the BlockingIOError
                # path entirely; the give-back path resets it on hand-back.
                # On the selector thread we restore non-blocking inline.
                sock = self.conn.sock
                try:
                    self.conn.receive(size)
                except BlockingIOError:
                    sock.settimeout(self.selector.config.rw_timeout)
                    try:
                        self.conn.receive(size)
                    finally:
                        if self.conn.tracked:
                            sock.settimeout(0)
            elif isinstance(event, h11.Data):
                self.conn.body_bytes_received += len(event.data)
                if self.conn.body_bytes_received > self.selector.config.max_body_size:
                    raise BodyTooLarge(self.conn.body_bytes_received)
                return bytes(event.data)
            elif isinstance(event, h11.EndOfMessage):
                return b""
            else:  # h11.ConnectionClosed is not possible, it will be a protocol error
                raise RuntimeError(f"Unexpected h11 event: {event!r}")

    def start_response(self, response: Response | InformationalResponse, /) -> None:
        if isinstance(response, Response):
            self.response_status = response.status_code
            if self.request.method == b"HEAD" and not _has_response_framing(response.headers):
                response = Response(
                    status_code=response.status_code,
                    headers=[*response.headers, (b"content-length", b"0")],
                    reason=response.reason,
                )
            # Drive the h11 state machine, but buffer the bytes for a
            # coalesced ``sendall`` with the first body chunk.
            payload = self.conn.parser.send(_to_h11_response(response))
            self._pending_header_bytes = bytes(payload) if payload else b""
        else:
            # Informational responses (100 Continue, etc.) flush immediately.
            self.conn.send(_to_h11_response(response))

    def send(self, chunk: Buffer, /) -> None:
        # h11 wants bytes; widen the public API to any Buffer (memoryview,
        # bytearray, …) so callers can avoid an explicit copy.
        chunk_bytes = chunk if isinstance(chunk, bytes) else bytes(chunk)
        payload = self.conn.parser.send(h11.Data(data=chunk_bytes))
        if payload is None:
            return
        if self._pending_header_bytes is not None:
            combined = self._pending_header_bytes + payload
            self._pending_header_bytes = None
            self._sock_sendall(combined)
        elif payload:
            self._sock_sendall(payload)

    def finish_response(self) -> None:
        # Coalesce: ``EndOfMessage`` payload (chunked terminator or nothing)
        # plus any still-pending header bytes (empty-body case) emit in one
        # ``sendall``.
        eom_payload = self.conn.parser.send(h11.EndOfMessage())
        eom_bytes = bytes(eom_payload) if eom_payload else b""
        if self._pending_header_bytes is not None:
            combined = self._pending_header_bytes + eom_bytes
            self._pending_header_bytes = None
            if combined:
                self._sock_sendall(combined)
        elif eom_bytes:
            self._sock_sendall(eom_bytes)
        # Drain h11's pending ``EndOfMessage`` for the request side before
        # giving the conn back. For a no-body request the selector parsed
        # ``Request`` and stopped — h11 still has the implicit EndOfMessage
        # queued, and ``their_state`` won't reach ``DONE`` until something
        # consumes it. Without this drain, the next selector wake on a
        # keep-alive request hits ``PAUSED`` from ``parser.next_event``.
        #
        # If h11 needs more bytes (handler didn't read a non-empty body),
        # close the conn — keep-alive isn't safe with un-drained body bytes.
        parser = self.conn.parser
        while parser.their_state is not h11.DONE:
            event = parser.next_event()
            if event is h11.NEED_DATA or event is h11.PAUSED or isinstance(event, h11.ConnectionClosed):
                self.conn.close()
                return
        self._maybe_give_back()

    def _sock_sendall(self, payload: bytes) -> None:
        _send_all(self.conn, payload)
