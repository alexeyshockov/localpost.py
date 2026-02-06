"""
Simple WSGI server implementation using h11 for HTTP protocol handling.

Notes:
- ISO-8859-1 is used for header encoding/decoding as per HTTP/1.1 specification.
- The server supports keep-alive connections and graceful shutdown.
"""

from __future__ import annotations

import itertools
import logging
import socket
import time
from collections.abc import Iterator, Callable, Iterable, Generator
from contextlib import contextmanager, suppress, closing
from dataclasses import dataclass
from io import DEFAULT_BUFFER_SIZE, RawIOBase
from typing import final, Literal

import h11

from localpost._sync_utils import CHECK_TIMEOUT, check_cancelled
from localpost.http.config import ServerConfig, LOGGER_NAME

__all__ = ['Server', 'ClientConn', 'RequestHandler', 'StartResponse', 'start_http_server']


@contextmanager
def start_http_server(config: ServerConfig) -> Iterator[Server]:
    _socket = socket.create_server(
        (config.host, config.port),
        backlog=config.backlog,
        reuse_port=True,
    )
    _socket.settimeout(CHECK_TIMEOUT)

    logger = logging.getLogger(LOGGER_NAME)

    server = Server(_socket, config, logger)
    logger.info(f"Serving on {config.host}:{server.port}")
    try:
        yield server
    finally:
        server.shutdown()


@final
class Server:
    def __init__(
        self,
        server_sock: socket.socket,
        config: ServerConfig,
        logger: logging.Logger,
    ) -> None:
        self._socket = server_sock
        self.config = config
        self.port = server_sock.getsockname()[1]
        """
        Actual port the server is listening on.
        
        Can be useful when port 0 is specified to auto-assign a free port.
        """
        self._logger = logger
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def shutdown(self) -> None:
        """Stop accepting new connections and close the server socket."""
        if not self._running:
            return
        self._socket.close()  # Safe to call if from another thread, will cause accept() to raise OSError

    def __iter__(self) -> Iterator[ClientConn]:
        self._running = True
        while True:
            try:
                check_cancelled()
                client_sock, client_addr = self._socket.accept()
                cs = ClientSocket(self, client_sock, client_addr, self.config.rw_timeout)
                yield ClientConn(self, cs, self._logger)
            except TimeoutError:
                pass
            except OSError:
                if self._running:
                    self._running = False
                    return  # Socket was closed, exit gracefully
                raise  # Unexpected error


@final
@dataclass(slots=True)
class RequestBodyStream(RawIOBase):
    _conn: h11.Connection
    _sock: ClientSocket
    _finished: bool = False

    def writable(self):
        return False

    def seekable(self):
        return False

    def readable(self) -> bool:
        return True

    def readall(self):
        chunks = bytearray()
        for chunk in self.receive_chunks():
            chunks.extend(chunk)
        return chunks

    def readinto(self, b: bytearray, /) -> int:
        try:
            data = self.receive_chunk(len(b))
            size = len(data)
            b[:size] = data
            return size
        except EOFError:
            return 0

    def receive_chunks(self) -> Iterable[bytes]:
        with suppress(EOFError):
            while True:
                yield self.receive_chunk(DEFAULT_BUFFER_SIZE)

    def receive_chunk(self, size: int, /) -> bytes:
        """Receive next chunk of body data from the socket via h11."""
        conn, sock = self._conn, self._sock
        while True:
            if self.closed:
                raise ValueError("Read on closed request body stream")
            if self._finished:
                raise EOFError("End of request body stream")
            event = conn.next_event()
            if event is h11.NEED_DATA:
                conn.receive_data(sock.recv(size))
            elif isinstance(event, h11.Data):
                return event.data
            elif isinstance(event, h11.EndOfMessage):
                self._finished = True
            else:
                raise RuntimeError(f"Unexpected h11 event: {event!r}")

    def drain(self) -> None:
        """Consume any remaining body data. Required before starting next request cycle."""
        with suppress(EOFError):
            while not self._finished:
                self.receive_chunk(DEFAULT_BUFFER_SIZE)


class ServerShutdown(Exception):
    pass


@dataclass(slots=True)
class ClientSocket:
    server: Server
    sock: socket.socket
    addr: tuple[str, int]
    timeout: float = CHECK_TIMEOUT * 5
    """Timeout for receive/send operations"""
    _recv_buf: bytes | None = None

    def __post_init__(self):
        self.sock.settimeout(CHECK_TIMEOUT)

    def wait_for_req(self, timeout: float) -> None:
        for _ in range(int(timeout / CHECK_TIMEOUT)):
            check_cancelled()
            if not self.server.running:
                raise ServerShutdown()
            with suppress(TimeoutError):
                self._recv_buf = self.sock.recv(DEFAULT_BUFFER_SIZE)
                return
        raise TimeoutError()  # TODO Message

    def recv(self, size: int = DEFAULT_BUFFER_SIZE, /) -> bytes:
        buf = self._recv_buf
        if buf is not None:
            self._recv_buf = None
        else:
            for _ in range(int(self.timeout / CHECK_TIMEOUT)):
                check_cancelled()
                with suppress(TimeoutError):
                    buf = self.sock.recv(size)
        if buf is None:
            raise TimeoutError()  # TODO Message
        elif buf == b"":
            raise ConnectionAbortedError("Client closed connection unexpectedly")
        return buf

    def sendall(self, buf, /) -> None:
        for _ in range(int(self.timeout / CHECK_TIMEOUT)):
            check_cancelled()
            with suppress(TimeoutError):
                return self.sock.sendall(buf)
        raise TimeoutError()  # TODO Message

    def close(self) -> None:
        self.sock.close()


@final
class ClientConn:
    def __init__(
        self,
        server: Server,
        client_sock: ClientSocket,
        logger: logging.Logger,
    ) -> None:
        self.server = server
        self.config = server.config
        self.socket = client_sock
        self.conn = h11.Connection(h11.SERVER)
        self._logger = logger

    @property
    def state(self) -> Literal['active', 'idle', 'closed']:
        if self.conn.our_state is h11.IDLE:
            return 'idle'
        elif self.conn.our_state is h11.MUST_CLOSE:
            return 'closed'
        return 'active'

    def __call__(self, h: RequestHandler) -> None:
        # TODO Assert it's not called concurrently
        conn, sock = self.conn, self.socket

        try:
            # Only read until we get the Request headers - body is read lazily
            while self.server.running:
                if conn.our_state is h11.MUST_CLOSE:
                    return
                elif conn.our_state is h11.DONE:
                    sock.wait_for_req(self.config.keep_alive_timeout)
                event = conn.next_event()
                if event is h11.NEED_DATA:
                    conn.receive_data(sock.recv())
                elif isinstance(event, h11.Request):
                    body = RequestBodyStream(conn, sock)
                    self._handle_request(h, event, body)
                    body.drain()
                    if conn.our_state is h11.MUST_CLOSE:
                        return
                    # conn.start_next_cycle()
                elif event == h11.ConnectionClosed:
                    # TODO Actually close, gracefully
                    return  # Client closed connection
                else:
                    raise RuntimeError(f"Unexpected h11 event: {event!r}")
        except ServerShutdown:
            return
        except TimeoutError | ConnectionAbortedError as exc:
            self._logger.debug(exc.message)
        finally:
            sock.close()

    def _handle_request(self, h: RequestHandler, request: h11.Request, body: RequestBodyStream) -> None:
        conn, sock = self.conn, self.socket

        def start_response(response: h11.Response) -> None:
            check_cancelled()
            # Add Connection header if not present?..
            sock.sendall(conn.send(response))

        with closing(h(self, request, body, start_response)) as response_chunks:
            check_cancelled()
            for chunk in response_chunks:
                check_cancelled()
                sock.sendall(conn.send(chunk))

        sock.sendall(conn.send(h11.EndOfMessage()))


StartResponse = Callable[[h11.Response], None]
RequestHandler = Callable[[ClientConn, h11.Request, RawIOBase, StartResponse], Generator[h11.Data]]


def _main():
    logging.basicConfig(level=logging.DEBUG)

    def simple_app(c: ClientConn, r: h11.Request, rb: RawIOBase, start_response: StartResponse):
        start_response(h11.Response(status_code=200, headers=[('Content-Type', 'text/plain')]))
        yield h11.Data(data=b'Hello, World!\n')

    with start_http_server(ServerConfig()) as server:
        for client_conn in server:
            client_conn(simple_app)


if __name__ == '__main__':
    _main()
