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
from typing import final, Literal

import h11

from localpost import threadtools
from localpost.http.config import DEFAULT_BUFFER_SIZE, LOGGER_NAME, ServerConfig

__all__ = ["Server", "HTTPReq", "RequestHandler", "start_http_server"]



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
        logger.info(f"Serving on {config.host}:{server.port}")
        yield server


@dataclass(slots=True)
class Connections:
    server: Server
    _selector: selectors.BaseSelector
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def select(self) -> Iterator[selectors.SelectorKey]:
        selector, server_sock = self._selector, self.server.sock
        server_sock.settimeout(0)
        selector.register(server_sock, selectors.EVENT_READ)
        try:
            while True:
                self._cleanup_stale()
                # TODO Take iteration payload (pending connections) and set it empty, under the lock
                # TODO Add selector.select() to the current payload (chain)
                for key, _ in selector.select(timeout=threadtools.CHECK_TIMEOUT):
                    yield key
        finally:
            selector.unregister(server_sock)


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
        self.port = server_sock.getsockname()[1]
        """
        Actual port the server is listening on.

        Can be useful when port 0 is specified to auto-assign a free port.
        """
        self.selector = selector
        self.config = config
        self._logger = logger
        self._lock = threading.Lock()

    def _find_stale(self):
        now, keep_alive_timeout = time.monotonic(), self.server.config.keep_alive_timeout
        for key in self._selector.get_map().values():
            if (conn := key.data) and isinstance(conn, HTTPConn):
                if now - conn.idle_since > keep_alive_timeout:
                    yield conn

    def _cleanup_stale(self):
        with self._lock:
            for conn in list(self._find_stale()):
                self._selector.unregister(conn.sock.raw_sock)
                conn.close()

    # TODO Add self-pipe wakeup trick and queue
    def track(self, conn: HTTPConn) -> None:
        conn.idle_since, sock = time.monotonic(), conn.sock.raw_sock
        sock.settimeout(0)
        with self._lock:
            self._selector.register(sock, selectors.EVENT_READ, data=conn)
        conn.tracked = True

    # TODO Remove
    def keep_alive(self, conn: HTTPConn) -> None:
        self.track(conn)

    def stop_tracking(self, conn: HTTPConn) -> None:
        sock = conn.sock.raw_sock
        with self._lock:
            self._selector.unregister(sock)
        sock.settimeout(None)
        conn.tracked = False

    def run(self, h: RequestHandler) -> Iterator[None]:
        conns, server_sock = self._conns, self.sock
        self._running = True
        for key in conns.select():
            yield
            if key.fileobj is server_sock:
                client_sock, client_addr = server_sock.accept()
                cs = ClientSocket(client_sock, client_addr, self.config.rw_timeout)
                # yield HTTPConn(self, self.config, cs, self._logger)
                self._handle_client_conn(HTTPConn(self, self.config, cs, self._logger))
            elif (conn := key.data) and isinstance(conn, HTTPConn):
                # conns.unregister(conn)
                # yield conn
                self._handle_client_conn(conn)
            else:
                raise RuntimeError(f"Unexpected selector key: {key!r}")


@final
@dataclass(slots=True)
class HTTPConn:
    sock: socket.socket
    addr: tuple[str, int]
    parser: h11.Connection = field(default_factory=lambda: h11.Connection(h11.SERVER))
    idle_since: float = 0.0
    tracked: bool = False
    current_req: _HTTPReq | None = None

    def req_state(self) -> Literal["DRAIN_BODY", "COLLECT_BODY"] | None:
        our_state, their_state = self.our_state, self.their_state
        if self.current_req:
            if our_state in (h11.DONE, h11.MUST_CLOSE):
                return "DRAIN_BODY"
            # We are in the middle of processing a request, the handler is requested to collect the full request body
            assert our_state in (h11.SEND_RESPONSE, h11.SEND_BODY)
            if their_state is h11.SEND_BODY:
                return "COLLECT_BODY"

    # FIXME Do call it!
    def close(self) -> None:
        self.sock.close()

    def receive(self, size: int, /) -> None:
        self.parser.receive_data(self.sock.recv(size))

    def send(self, event: h11.Event) -> None:
        self.sock.sendall(self.parser.send(event))

    def __call__(self, h: RequestHandler) -> None:
        parser, sock = self.parser, self.sock

        while self.tracked:
            if parser.our_state is h11.MUST_CLOSE:
                client_conn.close()
                return
            if parser.our_state is h11.DONE and parser.their_state is h11.DONE:
                parser.start_next_cycle()
                self.current_req = None
            event = parser.next_event()

            if (self.current_req and
                parser.their_state in h11.SEND_BODY and
                parser.our_state in (h11.SEND_RESPONSE, h11.SEND_BODY)
            ):  # We are in the middle of processing a request, the handler is requested to collect the full request body
                if event is h11.NEED_DATA:
                    self._conn.receive(size)
                elif isinstance(event, h11.Data):
                    self.current_req.request_body += event.data
                elif isinstance(event, h11.EndOfMessage):
                    h(self.current_req)  # Call the handler again, now with the full request body
                    if req_ctx.borrowed:
                        return

            elif event is h11.NEED_DATA:
                if parser.they_are_waiting_for_100_continue:
                    if self.req_state() == "DRAIN_BODY":
                        self._logger.debug("Draining request body for a request with 'Expect: 100-continue' header")
                        self.send(h11.Response(status_code=417, headers=[], reason="Expectation Failed"))
                    else:
                        self.send(h11.InformationalResponse(status_code=100, headers=[], reason="Continue"))
                parser.receive_data(sock.recv(DEFAULT_BUFFER_SIZE))
            elif isinstance(event, h11.Request):
                self.current_req = req_ctx = _HTTPReq(self, client_conn, event)
                h(req_ctx)
                if req_ctx.borrowed:
                    return
                # There can be multiple cases:
                #  - the request is fully processed and the response is sent, we are done
                #  - their_state is SEND_BODY, which means that the handler requested the full request body
            elif isinstance(event, h11.ConnectionClosed):
                self._logger.debug("Client closed connection")
                client_conn.close()
                return
            else:  # h11.Data | h11.EndOfMessage should be handled while processing the request (body)
                raise RuntimeError(f"Unexpected {event!r} in the connection loop")


@dataclass(eq=False, frozen=True, slots=True)
class _HTTPReq:
    _server: Server
    _conn: HTTPConn

    request: h11.Request
    request_body: bytes | None = field(default=None, init=False)

    @property
    def borrowed(self) -> bool:
        return not self._conn.tracked

    def borrow(self) -> BorrowedHTTPReq:
        assert self._conn.tracked
        self._server.stop_tracking(self._conn)
        return BorrowedHTTPReq(self._server, self._conn, self.request)

    # Usually with a simple response, like 404 or 405, with a small body (so it can fit into the kernel socket buffer,
    # to not block the server thread)
    def complete(self, response: h11.Response, body: bytes | None = None) -> None:
        self._conn.send(response)
        if body is not None:
            self._conn.send(h11.Data(body))
        self._conn.send(h11.EndOfMessage())


@dataclass(eq=False, frozen=True, slots=True)
class BorrowedHTTPReq:
    _server: Server
    _conn: HTTPConn

    request: h11.Request
    request_body: bytearray | None

    def give_back(self) -> None:
        assert not self._conn.tracked
        self._server.track(self._conn)

    def receive(self, size: int = DEFAULT_BUFFER_SIZE, /) -> bytes:
        parser = self._conn.parser
        if parser.their_state is h11.DONE:  # Request body exhausted
            return b""
        while True:
            event = parser.next_event()
            if event is h11.NEED_DATA:
                if parser.they_are_waiting_for_100_continue:
                    self._conn.send(h11.InformationalResponse(status_code=100, headers=[], reason="Continue"))
                self._conn.receive(size)
            elif isinstance(event, h11.Data):
                return event.data
            elif isinstance(event, h11.EndOfMessage):
                return b""
            # elif isinstance(event, h11.ConnectionClosed):
            #     raise ConnectionAbortedError("Client closed connection unexpectedly")
            else:
                raise RuntimeError(f"Unexpected h11 event: {event!r}")

    def start_response(self, response: h11.Response, /) -> None:
        self._conn.send(response)

    def send(self, chunk: bytes, /) -> None:
        self._conn.send(h11.Data(chunk))

    def end_response(self) -> None:
        self._conn.send(h11.EndOfMessage())


RequestHandler = Callable[[HTTPReq], None]
