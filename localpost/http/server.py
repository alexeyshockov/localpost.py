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
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from typing import final

import h11

from localpost import threadtools
from localpost.http.config import DEFAULT_BUFFER_SIZE, LOGGER_NAME, ServerConfig

__all__ = ["start_http_server", "HTTPReqCtx", "RequestHandler"]


RW_TIMEOUT = threadtools.CHECK_TIMEOUT


@contextmanager
def start_http_server(config: ServerConfig) -> Iterator[Server]:
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
        server = Server(config, logger, server_sock, selector)
        # logger.info(f"Serving on {config.host}:{server.port}")
        logger.info("Serving on %s:%d", config.host, server.port)
        yield server
        # TODO Close all the client connections that are currently registered in the selector
        # They can be in either:
        # - Idle state (keep-alive) — just close the socket, no need to send anything
        # - Request being sent by the client, but not fully received yet — send 503 Service Unavailable and close the socket
        #       or simply close the socket for now
        # - Draining request body, but not fully received yet — response already sent, just close the socket


@final
class Server:
    def __init__(
        self,
        config: ServerConfig,
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
        self.logger = logger
        self._lock = threading.Lock()

    def _find_stale(self):
        now = time.monotonic()
        for key in self.selector.get_map().values():
            if (conn := key.data) and isinstance(conn, HTTPConn) and conn.close_at and now > conn.close_at:
                yield conn

    def _cleanup_stale(self):
        with self._lock:
            for conn in list(self._find_stale()):
                self.selector.unregister(conn.sock)
                conn.close()  # TODO Send 408 Request Timeout with Connection: close, then close the socket

    # TODO Add self-pipe wakeup trick and a queue
    def track(self, conn: HTTPConn) -> None:
        sock = conn.sock
        sock.settimeout(0)
        with self._lock:
            self.selector.register(sock, selectors.EVENT_READ, data=conn)
        conn.tracked = True

    def stop_tracking(self, conn: HTTPConn) -> None:
        sock = conn.sock
        with self._lock:
            self.selector.unregister(sock)
        sock.settimeout(RW_TIMEOUT)
        conn.tracked = False

    def run(self, h: RequestHandler) -> None:
        """One iteration of the server loop. Should be called repeatedly until the server is stopped."""
        server_sock = self.sock
        threadtools.check_cancelled()
        self._cleanup_stale()
        # TODO Take iteration payload (pending connections) and set it empty, under the lock
        # TODO Add selector.select() to the current payload (chain)
        for key, _ in self.selector.select(timeout=threadtools.CHECK_TIMEOUT):
            if key.fileobj is server_sock:
                client_sock, client_addr = server_sock.accept()
                conn = HTTPConn(self, client_sock, client_addr)
                self.track(conn)
            elif (conn := key.data) and isinstance(conn, HTTPConn):
                conn(h)  # TODO Handle exceptions...
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

    def close(self) -> None:
        if self.tracked:
            self.server.selector.unregister(self.sock)
        self.sock.close()

    def receive(self, size: int = DEFAULT_BUFFER_SIZE, /) -> None:
        data = self.sock.recv(size)
        self.parser.receive_data(data)
        if not data:
            self.recv_closed = True

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
        parser = self.parser

        # TODO Handler LocalProtocolError, it will send respo automatically

        # TODO Check state: if our_state is not h11.DONE, then send 500 Internal Server Error and give back
        # (a handler should always finish with a response)

        while self.tracked:
            if parser.our_state is h11.MUST_CLOSE:
                self.close()  # TODO Proper half close, later
                # FIXME Remove the socket from the selector
                return
            if parser.our_state is h11.DONE and parser.their_state is h11.DONE:
                parser.start_next_cycle()
                self.close_at = time.monotonic() + self.server.config.keep_alive_timeout

            event = parser.next_event()

            if event is h11.NEED_DATA:
                if parser.they_are_waiting_for_100_continue:  # Drain the request body
                    self.send(h11.Response(status_code=417, headers=[], reason="Expectation Failed"))
                    continue
                # TODO h11.RemoteProtocolError: peer closed connection without sending complete message body
                try:
                    self.receive()
                except BlockingIOError:
                    return  # Wait for it in the selector
                self.close_at = time.monotonic() + RW_TIMEOUT
            elif isinstance(event, h11.Data | h11.EndOfMessage):
                continue  # Drain the request body
            elif isinstance(event, h11.Request):
                req_ctx = HTTPReqCtx(self.server, self, event)
                h(req_ctx)
                if req_ctx.borrowed:
                    return
            elif isinstance(event, h11.ConnectionClosed):
                self.server.logger.debug("Client closed connection")
                self.close()
                return
            else:
                raise RuntimeError(f"Unexpected {event!r} in the connection loop")


@dataclass(eq=False, frozen=True, slots=True)
class HTTPReqCtx:
    _server: Server
    _conn: HTTPConn
    request: h11.Request

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
                self._conn.receive(size)
            elif isinstance(event, h11.Data):
                return event.data
            elif isinstance(event, h11.EndOfMessage):
                return b""
            else:  # h11.ConnectionClosed is not possible, it will be a protocol error
                raise RuntimeError(f"Unexpected h11 event: {event!r}")

    def start_response(self, response: h11.Response | h11.InformationalResponse, /) -> None:
        self._conn.send(response)

    # TODO Accept any Buffer, later
    def send(self, chunk: bytes, /) -> None:
        self._conn.send(h11.Data(chunk))

    def finish_response(self) -> None:
        self._conn.send(h11.EndOfMessage())
        self._maybe_give_back()


RequestHandler = Callable[[HTTPReqCtx], None]
