"""HTTP/1.1 server backend driven by h11.

Pure-Python parser/state-machine. The default backend; readable, no C deps.
For the C-based alternative see :mod:`localpost.http.server_httptools`.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Buffer, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import final

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
    BaseServer,
    RequestHandler,
    emit_handler_error,
    start_http_server_base,
)
from localpost.http._types import BodyTooLarge, InformationalResponse, Request, Response
from localpost.http.config import DEFAULT_BUFFER_SIZE, ServerConfig

__all__ = ["start_http_server", "HTTPConnH11", "HTTPReqCtxH11"]


def _to_h11_response(r: Response | InformationalResponse) -> h11.Response | h11.InformationalResponse:
    if isinstance(r, Response):
        return h11.Response(status_code=r.status_code, headers=r.headers, reason=r.reason)
    return h11.InformationalResponse(status_code=r.status_code, headers=r.headers, reason=r.reason)


def _content_length(headers) -> int | None:
    # h11 normalizes header names to lowercase bytes — direct equality is enough.
    for name, value in headers:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


@final
@dataclass(eq=False, slots=True)
class HTTPConnH11(BaseHTTPConn):
    server: BaseServer
    sock: socket.socket
    addr: tuple[str, int]
    fd: int = field(init=False)
    """The integer file descriptor captured at construction time. Used to
    clean up ``selector._fd_to_key`` after ``sock.close()`` (where
    ``sock.fileno()`` returns -1)."""
    recv_closed: bool = False
    parser: h11.Connection = field(default_factory=lambda: h11.Connection(h11.SERVER))
    close_at: float | None = None
    tracked: bool = False
    body_bytes_received: int = 0
    """Cumulative body bytes received for the current request — reset on
    ``parser.start_next_cycle``. Compared against ``ServerConfig.max_body_size``
    to enforce the upload cap."""
    idle: bool = True

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
        payload_len = len(payload)
        sock, total_sent = self.sock, 0
        while total_sent < payload_len:
            sent = sock.send(payload[total_sent:])
            if sent == 0:
                raise ConnectionAbortedError("socket is broken")
            total_sent = total_sent + sent

    def __call__(self, h: RequestHandler, /) -> None:
        try:
            self._loop(h)
        except h11.RemoteProtocolError as e:
            self.server.logger.warning("Bad client input from %s: %s", self.addr, e)
            self._try_send_status(BAD_REQUEST_RESPONSE, BAD_REQUEST_BODY)
            self.close()
        except h11.LocalProtocolError:
            self.server.logger.exception("Local protocol error from %s", self.addr)
            self._try_send_status(INTERNAL_ERROR_RESPONSE, INTERNAL_ERROR_BODY)
            self.close()
        except BodyTooLarge:
            self.server.logger.warning(
                "Request body from %s exceeds max_body_size=%d", self.addr, self.server.config.max_body_size
            )
            self._try_send_status(PAYLOAD_TOO_LARGE_RESPONSE, PAYLOAD_TOO_LARGE_BODY)
            self.close()

    def _loop(self, h: RequestHandler) -> None:
        parser = self.parser

        while self.tracked:
            if parser.our_state is h11.MUST_CLOSE:
                self.close()
                return
            if parser.our_state is h11.DONE and parser.their_state is h11.DONE:
                parser.start_next_cycle()
                self.body_bytes_received = 0
                self.idle = True
                self.close_at = time.monotonic() + self.server.config.keep_alive_timeout

            event = parser.next_event()

            if event is h11.NEED_DATA:
                if parser.they_are_waiting_for_100_continue:
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
                req = Request(
                    method=bytes(event.method),
                    target=bytes(event.target),
                    headers=[(bytes(n), bytes(v)) for n, v in event.headers],
                    http_version=bytes(event.http_version),
                )
                req_ctx = HTTPReqCtxH11(self.server, self, req)
                try:
                    h(req_ctx)
                except BodyTooLarge:
                    raise
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
            self.sock.settimeout(self.server.config.rw_timeout)
            payload = self.parser.send(_to_h11_response(REQUEST_TIMEOUT_RESPONSE))
            if payload:
                self.sock.sendall(payload)
            payload = self.parser.send(h11.Data(data=REQUEST_TIMEOUT_BODY))
            if payload:
                self.sock.sendall(payload)
            payload = self.parser.send(h11.EndOfMessage())
            if payload:
                self.sock.sendall(payload)
        except Exception:  # noqa: BLE001, S110 — the conn is being torn down anyway
            pass


@dataclass(eq=False, frozen=True, slots=True)
class HTTPReqCtxH11:
    """Per-request context for the h11 backend.

    Structurally satisfies :class:`localpost.http._base.HTTPReqCtx`.
    """

    _server: BaseServer
    _conn: HTTPConnH11
    request: Request

    response_status: int | None = field(default=None, init=False)

    @property
    def borrowed(self) -> bool:
        return not self._conn.tracked

    @contextmanager
    def borrow(self) -> Iterator[HTTPReqCtxH11]:
        """Switch the conn out of selector tracking for the duration of the block."""
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
        parser = self._conn.parser
        if parser.their_state is h11.DONE:
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
                return bytes(event.data)
            elif isinstance(event, h11.EndOfMessage):
                return b""
            else:  # h11.ConnectionClosed is not possible, it will be a protocol error
                raise RuntimeError(f"Unexpected h11 event: {event!r}")

    def start_response(self, response: Response | InformationalResponse, /) -> None:
        if isinstance(response, Response):
            object.__setattr__(self, "response_status", response.status_code)
        self._conn.send(_to_h11_response(response))

    def send(self, chunk: Buffer, /) -> None:
        # h11 wants bytes; widen the public API to any Buffer (memoryview,
        # bytearray, …) so callers can avoid an explicit copy.
        self._conn.send(h11.Data(data=bytes(chunk) if not isinstance(chunk, bytes) else chunk))

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


def start_http_server(config: ServerConfig, handler: RequestHandler, /):
    """Open a listening socket and yield a server driving the h11 backend.

    Default HTTP server: pure-Python parser, no C dependencies. For the
    httptools backend (faster, opt-in via ``[http-fast]``), use
    :func:`localpost.http.start_httptools_server`.
    """

    def _factory(server, sock, addr) -> HTTPConnH11:
        return HTTPConnH11(server, sock, addr)

    return start_http_server_base(config, handler, _factory)
