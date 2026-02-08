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
from collections.abc import Iterator, Callable, Iterable
from contextlib import contextmanager, suppress, AbstractContextManager, nullcontext, closing
from dataclasses import dataclass, field
from io import DEFAULT_BUFFER_SIZE, RawIOBase, IOBase
from typing import final

import h11

from localpost._sync_utils import CHECK_TIMEOUT, check_cancelled
from localpost.http.config import ServerConfig, LOGGER_NAME

__all__ = ['Server', 'ClientConn', 'RequestHandler', 'start_http_server']


def start_http_server(config: ServerConfig) -> AbstractContextManager[Server]:
    server_sock = socket.create_server(
        (config.host, config.port),
        backlog=config.backlog,
        reuse_port=True,
    )
    logger = logging.getLogger(LOGGER_NAME)

    server = Server(server_sock, config, logger)
    logger.info(f"Serving on {config.host}:{server.port}")
    return closing(server)


@dataclass(slots=True)
class Connections:
    server: Server
    keep_alive_conns: list[ClientConn] = field(default_factory=list)
    _selector: selectors.BaseSelector = field(default_factory=selectors.DefaultSelector)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # Add self-pipe wakeup trick and queue
    def register(self, conn: ClientConn) -> None:
        sock = conn.sock.sock
        sock.settimeout(0)
        with self._lock:
            self._selector.register(sock, selectors.EVENT_READ, data=conn)

    def unregister(self, conn: ClientConn) -> None:
        sock = conn.sock.sock
        with self._lock:
            self._selector.unregister(sock)
        sock.settimeout(CHECK_TIMEOUT)

    def select(self) -> Iterator[selectors.SelectorKey]:
        selector, server_sock = self._selector, self.server.sock
        server_sock.settimeout(0)
        selector.register(server_sock, selectors.EVENT_READ)
        try:
            while self.server.running:
                check_cancelled()
                for key, _ in selector.select(timeout=CHECK_TIMEOUT):
                    yield key
        finally:
            selector.unregister(server_sock)


@final
class Server:
    def __init__(
        self,
        server_sock: socket.socket,
        config: ServerConfig,
        logger: logging.Logger,
    ) -> None:
        self.sock = server_sock
        self.port = server_sock.getsockname()[1]
        """
        Actual port the server is listening on.
        
        Can be useful when port 0 is specified to auto-assign a free port.
        """
        self.config = config
        self.conns = Connections(self)
        self._logger = logger
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def close(self) -> None:
        """Stop accepting new connections and close the server socket."""
        if not self._running:
            return
        self.sock.close()  # Safe to call if from another thread, will cause accept() to raise OSError

    def keep_alive(self, conn: ClientConn) -> None:
        self.conns.register(conn)

    def accept(self) -> Iterable[ClientConn]:
        conns, server_sock = self.conns, self.sock
        self._running = True
        for key in conns.select():
            if key.fileobj is server_sock:
                client_sock, client_addr = server_sock.accept()
                cs = ClientSocket(client_sock, client_addr, self.config.rw_timeout)
                yield ClientConn(self, self.config, cs, self._logger)
            elif (conn := key.data) and isinstance(conn, ClientConn):
                conns.unregister(conn)
                yield conn
            else:
                raise RuntimeError(f"Unexpected selector key: {key!r}")


@dataclass(frozen=True, slots=True)
class ClientSocket:
    sock: socket.socket
    addr: tuple[str, int]
    timeout: float
    """Timeout for receive/send operations."""

    def recv(self, size: int = DEFAULT_BUFFER_SIZE, /) -> bytes:
        for _ in range(int(self.timeout / CHECK_TIMEOUT)):
            check_cancelled()
            with suppress(TimeoutError):
                return self.sock.recv(size)
        raise TimeoutError("receive timeout")

    def sendall(self, buf, /) -> None:
        for _ in range(int(self.timeout / CHECK_TIMEOUT)):
            check_cancelled()
            with suppress(TimeoutError):
                return self.sock.sendall(buf)
        raise TimeoutError("send timeout")

    def close(self) -> None:
        self.sock.close()


@final
@dataclass(slots=True)
class ClientConn:
    server: Server
    config: ServerConfig
    sock: ClientSocket
    _logger: logging.Logger
    _conn: h11.Connection = field(default_factory=lambda: h11.Connection(h11.SERVER))

    def __call__(self, h: RequestHandler) -> None:
        conn, sock = self._conn, self.sock
        try:
            while True:
                if (prev_state := conn.our_state) is h11.DONE:
                    conn.start_next_cycle()
                event = conn.next_event()
                if event is h11.NEED_DATA and prev_state is h11.DONE:
                    self.server.keep_alive(self)
                    return
                elif event is h11.NEED_DATA:
                    conn.receive_data(sock.recv())
                elif isinstance(event, h11.Request):
                    self._handle_request(h, event)
                    if conn.our_state is h11.MUST_CLOSE:
                        sock.close()
                        return
                elif isinstance(event, h11.ConnectionClosed):
                    self._logger.debug("Client closed connection")
                    sock.close()
                    return
                else:  # h11.Data | h11.EndOfMessage should be handled while processing the request (body)
                    raise RuntimeError(f"Unexpected {event!r} in the connection loop")
        except TimeoutError:
            self._logger.debug("Client connection timed out", exc_info=True)

    def _handle_request(self, h: RequestHandler, request: h11.Request) -> None:
        conn, sock = self._conn, self.sock
        body = RequestBodyStream(conn, sock)
        response, response_chunks = h(self, request, body)
        with response_chunks as chunks:
            sock.sendall(conn.send(response))
            check_cancelled()
            for chunk in chunks:
                check_cancelled()
                sock.sendall(conn.send(chunk))
            sock.sendall(conn.send(h11.EndOfMessage()))
        body.drain()


@dataclass(slots=True)
class RequestBodyStream(RawIOBase):
    conn: h11.Connection
    sock: ClientSocket
    finished: bool = False

    def writable(self):
        return False

    def seekable(self):
        return False

    def readable(self):
        return True

    def readall(self):
        chunks = bytearray()
        with suppress(EOFError):
            while not self.finished:
                chunks.extend(self._receive(DEFAULT_BUFFER_SIZE))
        return chunks

    def readinto(self, b: bytearray, /) -> int:
        try:
            data = self._receive(len(b))
            size = len(data)
            b[:size] = data
            return size
        except EOFError:
            return 0

    def _receive(self, size: int, /) -> bytes:
        """Receive next chunk of body data from the socket via h11."""
        if self.finished:
            raise EOFError()
        conn, sock = self.conn, self.sock
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                conn.receive_data(sock.recv(size))
            elif isinstance(event, h11.Data):
                return event.data
            elif isinstance(event, h11.EndOfMessage):
                self.finished = True
                raise EOFError()
            elif isinstance(event, h11.ConnectionClosed):
                raise ConnectionAbortedError("Client closed connection unexpectedly")
            else:
                raise RuntimeError(f"Unexpected h11 event: {event!r}")

    def drain(self) -> None:
        """Consume any remaining body data. Required before starting next request cycle."""
        with suppress(EOFError):
            while not self.finished:
                self._receive(DEFAULT_BUFFER_SIZE)


RequestHandler = Callable[
    [ClientConn, h11.Request, IOBase],
    tuple[h11.Response, AbstractContextManager[Iterable[h11.Data]]]
]


def _main():
    logging.basicConfig(level=logging.DEBUG)

    def simple_app(c: ClientConn, r: h11.Request, rb: IOBase):
        return (h11.Response(status_code=200, headers=[('Content-Type', 'text/plain')]),
                nullcontext([h11.Data(b'Hello, World!\n')]))

    with start_http_server(ServerConfig()) as server:
        for client_conn in server.accept():
            client_conn(simple_app)


if __name__ == '__main__':
    _main()
