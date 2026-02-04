"""
Simple WSGI server implementation using h11 for HTTP protocol handling.

Notes:
- ISO-8859-1 is used for header encoding/decoding as per HTTP/1.1 specification.
- The server supports keep-alive connections and graceful shutdown.
"""

from __future__ import annotations

import logging
import socket
import sys
from collections.abc import Iterator, Callable
from contextlib import contextmanager, closing, suppress
from io import BufferedReader, DEFAULT_BUFFER_SIZE, RawIOBase
from typing import Any, final
from wsgiref.types import WSGIApplication

import h11

from .config import ServerConfig, LOGGER_NAME

__all__ = ['Server', 'ClientConn', 'start_http_server']

CHECK_TIMEOUT = 0.5  # Seconds

try:
    from anyio import from_thread, NoEventLoopError  # noqa


    def _checkpoint() -> None:
        try:
            from_thread.check_cancelled()
        except NoEventLoopError:
            pass
except ImportError:
    def _checkpoint() -> None:
        pass


def _sock_op[**P, T](op: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Perform a socket operation with automatic retry on timeout."""
    while True:
        try:
            _checkpoint()
            return op(*args, **kwargs)
        except socket.timeout:
            continue


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
        with suppress(EOFError):
            while True:
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
        if self._finished:
            raise EOFError()
        conn, sock = self._conn, self._sock
        while True:
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
                raise EOFError()
            else:
                raise RuntimeError(f"Unexpected h11 event: {event!r}")

    def drain(self) -> None:
        """Consume any remaining body data. Required before starting next request cycle."""
        with suppress(EOFError):
            while not self._finished:
                self._receive(DEFAULT_BUFFER_SIZE)


@contextmanager
def start_http_server(app: WSGIApplication, config: ServerConfig) -> Iterator[Server]:
    _socket = socket.create_server(
        (config.host, config.port),
        backlog=config.backlog,
        reuse_port=True,
    )
    _socket.settimeout(CHECK_TIMEOUT)

    logger = logging.getLogger(LOGGER_NAME)

    server = Server(_socket, app, config, logger)
    logger.info(f"Serving on {config.host}:{server.port}")
    with _socket:
        yield server


@final
class Server:
    def __init__(
        self,
        server_sock: socket.socket,
        app: WSGIApplication,
        config: ServerConfig,
        logger: logging.Logger,
    ) -> None:
        self._socket = server_sock
        self.app = app
        self.config = config
        self.port = server_sock.getsockname()[1]
        """
        Actual port the server is listening on.
        
        Can be useful when port 0 is specified to auto-assign a free port.
        """
        self._logger = logger
        self._closed = False

    def shutdown(self) -> None:
        """Graceful shutdown (stop accepting connections)."""
        if self._closed:
            return
        self._closed = True

    def __iter__(self) -> Iterator[ClientConn]:
        while not self._closed:
            client_sock, client_addr = _sock_op(self._socket.accept)
            yield ClientConn(self, client_sock, self._logger)


@final
class ClientConn:
    def __init__(
        self,
        server: Server,
        client_sock: socket.socket,
        logger: logging.Logger,
    ) -> None:
        self.server = server
        self.config = server.config
        self._socket = client_sock
        self._conn = h11.Connection(h11.SERVER)
        self._logger = logger

    def __call__(self) -> None:
        # TODO Assert it's not called concurrently
        with self._socket:
            self._socket.settimeout(self.config.read_timeout)
            while True:
                keep_alive = self._handle_request()
                if not keep_alive:
                    return
                # Prepare for next request on this client's connection
                self._conn.start_next_cycle()

    def _handle_request(self) -> bool:
        conn, sock = self._conn, self._socket
        request: h11.Request | None = None

        # Only read until we get the Request headers - body is read lazily
        while request is None:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                try:
                    data = _sock_op(sock.recv, DEFAULT_BUFFER_SIZE)
                except socket.timeout:
                    return False  # Idle timeout, close connection
                if not data:
                    return False  # Client closed connection
                conn.receive_data(data)
            elif isinstance(event, h11.Request):
                request = event

        # Create lazy body that will read from socket on demand
        body = RequestBodyStream(conn, sock)
        environ = self._build_environ(request, body)
        keep_alive: bool = False

        def start_response(status: str, headers: list[tuple[str, str]], /) -> None:
            nonlocal keep_alive
            keep_alive = _should_keep_alive(request, headers)

            # Add Connection header if not present
            if not any(name.lower() == 'connection' for name, _ in headers):
                headers.append(('Connection', 'keep-alive' if keep_alive else 'close'))

            # Send response headers immediately
            status_code = int(status.split(' ', 1)[0])
            response = h11.Response(
                status_code=status_code,
                headers=[(name.encode('ISO-8859-1'), value.encode('ISO-8859-1'))
                         for name, value in headers])
            sock.sendall(conn.send(response))

        response_body = self.server.app(environ, start_response)  # type: ignore
        try:
            for chunk in response_body:
                # TODO Check from_thread.check_cancelled()
                if chunk:
                    sock.sendall(conn.send(h11.Data(data=chunk)))
        finally:
            if hasattr(response_body, 'close'):
                response_body.close()  # Generator cleanup

        sock.sendall(conn.send(h11.EndOfMessage()))

        # Drain any unread body data before next request cycle
        if keep_alive:
            body.drain()

        return keep_alive

    def _build_environ(self, request: h11.Request, body: RequestBodyStream) -> dict[str, Any]:
        # Decode path and parse query string
        path = request.target.decode('ISO-8859-1')
        if '?' in path:
            path, query_string = path.split('?', 1)
        else:
            query_string = ''

        headers_dict = {}
        for name, value in request.headers:
            headers_dict[name.decode('ISO-8859-1').lower()] = value.decode('ISO-8859-1')

        # See https://wsgi.readthedocs.io/en/latest/definitions.html
        environ = {
            'REQUEST_METHOD': request.method.decode('ascii'),
            'PATH_INFO': path,
            'QUERY_STRING': query_string,
            'CONTENT_TYPE': headers_dict.get('content-type', ''),
            'CONTENT_LENGTH': headers_dict.get('content-length', ''),
            'SERVER_NAME': self.config.host,  # TODO Actual name
            'SERVER_PORT': str(self.server.port),
            'SERVER_PROTOCOL': f'HTTP/{request.http_version.decode("ascii")}',
            'wsgi.version': (1, 0),
            'wsgi.url_scheme': 'http',
            'wsgi.input': BufferedReader(body),
            'wsgi.errors': sys.stderr,  # Handle it later with the logger
            'wsgi.multithread': True,
            'wsgi.multiprocess': False,
            'wsgi.run_once': False,
        }

        # Add HTTP headers
        for name, value in request.headers:
            name_str = name.decode('ISO-8859-1').upper().replace('-', '_')
            if name_str not in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
                name_str = f'HTTP_{name_str}'
            environ[name_str] = value.decode('ISO-8859-1')

        return environ


def _should_keep_alive(request: h11.Request, response_headers: list[tuple[str, str]]) -> bool:
    """Determine if the connection should be kept alive."""
    # Check if response explicitly sets Connection header
    for name, value in response_headers:
        if name.lower() == 'connection':
            return value.lower() == 'keep-alive'

    # Check request's Connection header
    for name, value in request.headers:
        if name.lower() == b'connection':
            return value.lower() == b'keep-alive'

    # HTTP/1.1 defaults to keep-alive
    return request.http_version == b'1.1'


def _main():
    logging.basicConfig(level=logging.DEBUG)

    def simple_app(_, start_response):
        start_response('200 OK', [('Content-Type', 'text/plain')])
        yield b'Hello, World!\n'
        # return [b'Hello, World!\n']

    with start_http_server(simple_app, ServerConfig()) as server:
        for client_conn in server:
            client_conn()


def _main_flask():
    logging.basicConfig(level=logging.DEBUG)

    from flask import Flask, request

    app = Flask(__name__)

    @app.route('/hello/<name>')
    def hello(name):
        user_agent = request.headers.get('User-Agent', 'Unknown')
        return f'Hello, {name}! Your User-Agent is: {user_agent}\n'

    with start_http_server(app, ServerConfig()) as server:
        for client_conn in server:
            client_conn()


if __name__ == '__main__':
    # _main()
    _main_flask()
