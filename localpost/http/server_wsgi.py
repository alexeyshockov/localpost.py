"""
Simple WSGI server implementation using h11 for HTTP protocol handling.

Notes:
- ISO-8859-1 is used for header encoding/decoding as per HTTP/1.1 specification.
- The server supports keep-alive connections and graceful shutdown.
"""

from __future__ import annotations

import io
import logging
import sys
from io import BufferedReader
from typing import Any
from wsgiref.types import WSGIApplication

import h11

from .server import RequestHandler


def wrap_wsgi_app(app: WSGIApplication) -> RequestHandler:
    # def start_response(status: str, headers: list[tuple[str, str]], /) -> None:
    #     nonlocal keep_alive
    #     keep_alive = _should_keep_alive(request, headers)
    #
    #     # Add Connection header if not present
    #     if not any(name.lower() == 'connection' for name, _ in headers):
    #         headers.append(('Connection', 'keep-alive' if keep_alive else 'close'))
    #
    #     # Send response headers immediately
    #     status_code = int(status.split(' ', 1)[0])
    #     response = h11.Response(
    #         status_code=status_code,
    #         headers=[(name.encode('ISO-8859-1'), value.encode('ISO-8859-1'))
    #                  for name, value in headers])
    #     sock.sendall(conn.send(response))
    #
    # response_body = self.server.app(environ, start_response)  # type: ignore
    # try:
    #     for chunk in response_body:
    #         # TODO Check from_thread.check_cancelled()
    #         if chunk:
    #             sock.sendall(conn.send(h11.Data(data=chunk)))
    # finally:
    #     if hasattr(response_body, 'close'):
    #         response_body.close()  # Generator cleanup
    #
    # sock.sendall(conn.send(h11.EndOfMessage()))
    #
    # # Drain any unread body data before next request cycle
    # if keep_alive:
    #     body.drain()
    #
    # return keep_alive
    pass  # TODO Implement this function


def _build_environ(self, request: h11.Request, body: io.RawIOBase) -> dict[str, Any]:
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


def _main_flask():
    logging.basicConfig(level=logging.DEBUG)

    from flask import Flask, request

    app = Flask(__name__)

    @app.route('/hello/<name>')
    def hello(name):
        user_agent = request.headers.get('User-Agent', 'Unknown')
        return f'Hello, {name}! Your User-Agent is: {user_agent}\n'

    with start_http_server(ServerConfig()) as server:
        for client_conn in server:
            client_conn(app)

if __name__ == '__main__':
    _main_flask()
