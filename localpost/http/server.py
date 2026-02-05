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
from collections.abc import Iterator, Callable, Iterable
from contextlib import contextmanager, suppress
from io import DEFAULT_BUFFER_SIZE, RawIOBase, BufferedReader
from typing import final

import h11
from anyio import from_thread

from localpost._sync_utils import _sock_op, CHECK_TIMEOUT
from .config import ServerConfig, LOGGER_NAME

import dataclasses as dc

__all__ = ['Server', 'ClientConn', 'RequestHandler', 'start_http_server']


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
        self._closed = False

    def shutdown(self) -> None:
        """Stop accepting new connections and close the server socket."""
        if self._closed:
            return
        self._closed = True
        self._socket.close()  # Safe to call if from another thread, will cause accept() to raise OSError

    def __iter__(self) -> Iterator[ClientConn]:
        while not self._closed:
            try:
                client_sock, client_addr = _sock_op(self._socket.accept)
                yield ClientConn(self, client_sock, client_addr, self._logger)
            except OSError:
                if self._closed:
                    return  # Socket was closed, exit gracefully
                raise  # Unexpected error


@final
class RequestBodyStream(RawIOBase):
    def __init__(self, conn: h11.Connection, sock: socket.socket) -> None:
        self._conn = conn
        self._sock = sock
        self._finished = False

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
                data = _sock_op(sock.recv, size)
                if not data:
                    raise ConnectionAbortedError("Client closed connection unexpectedly")
                conn.receive_data(data)
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


@final
class ClientConn:
    def __init__(
        self,
        server: Server,
        client_sock: socket.socket,
        client_addr: tuple[str, int],
        logger: logging.Logger,
    ) -> None:
        self.server = server
        self.config = server.config
        self.socket = client_sock
        self.address = client_addr
        self.conn = h11.Connection(h11.SERVER)
        self._logger = logger

    def __call__(self, h: RequestHandler) -> None:
        # TODO Assert it's not called concurrently

        with self.socket:
            self.socket.settimeout(self.config.rw_timeout)
            while True:
                rb = RequestBodyStream(self.conn, self.socket)
                keep_alive = self._handle_request(h, rb)
                if not keep_alive:
                    return
                rb.drain()
                # Prepare for next request on this client's connection
                self.conn.start_next_cycle()
                # TODO Proper keep-alive timeout

    def _handle_request(self, h: RequestHandler, body: RequestBodyStream) -> bool:
        conn, sock = self.conn, self.socket
        request: h11.Request | None = None

        # Only read until we get the Request headers - body is read lazily
        while request is None:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                try:
                    data = sock.recv(DEFAULT_BUFFER_SIZE)
                except TimeoutError:
                    return False  # Idle timeout, close connection
                if not data:
                    return False  # Client closed connection
                conn.receive_data(data)
            elif isinstance(event, h11.Request):
                request = event

        keep_alive: bool = False

        def start_response(response: h11.Response) -> None:
            from_thread.check_cancelled()

            nonlocal keep_alive
            keep_alive = _should_keep_alive(request, response)

            # Add Connection header if not present?..
            sock.sendall(conn.send(response))

        response_chunks = h(self, request, body, start_response)
        try:
            from_thread.check_cancelled()
            for chunk in response_chunks:
                from_thread.check_cancelled()
                sock.sendall(conn.send(chunk))
        finally:
            if hasattr(response_chunks, 'close'):
                response_chunks.close()  # Generator cleanup

        sock.sendall(conn.send(h11.EndOfMessage()))

        return keep_alive


StartResponse = Callable[[h11.Response], None]
RequestHandler = Callable[[ClientConn, h11.Request, RawIOBase, StartResponse], Iterable[h11.Data]]


def _should_keep_alive(request: h11.Request, response: h11.Response) -> bool:
    """Determine if the connection should be kept alive."""
    # Check if either request or response has an explicit Connection header
    for name, value in itertools.chain(response.headers, request.headers):
        if name.lower() == b'connection':
            return value.lower() == 'keep-alive'

    # HTTP/1.1 defaults to keep-alive
    return request.http_version == b'1.1'


def _main():
    logging.basicConfig(level=logging.DEBUG)

    def simple_app(_, start_response):
        start_response('200 OK', [('Content-Type', 'text/plain')])
        yield b'Hello, World!\n'
        # return [b'Hello, World!\n']

    with start_http_server(ServerConfig()) as server:
        for client_conn in server:
            client_conn(simple_app)


if __name__ == '__main__':
    _main()
