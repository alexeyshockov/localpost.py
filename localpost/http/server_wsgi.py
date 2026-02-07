from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable
from contextlib import closing, AbstractContextManager, suppress
from io import BufferedReader, RawIOBase
from typing import Any
from wsgiref.types import WSGIApplication

import h11
from flask import stream_with_context

from localpost.http.server import ClientConn, RequestHandler


def wrap_wsgi(app: WSGIApplication) -> RequestHandler:
    """Wrap a WSGI application as a RequestHandler."""

    def handler(
        client: ClientConn, request: h11.Request, body: RawIOBase
    ) -> tuple[h11.Response, AbstractContextManager[Iterable[h11.Data]]]:
        environ = _build_environ(client, request, body)
        response: h11.Response | None = None

        def wsgi_start_response(
            status: str,
            headers: list[tuple[str, str]],
            exc_info: Any = None,
        ) -> Callable[[bytes], None]:
            nonlocal response
            if exc_info:
                try:
                    raise exc_info[1].with_traceback(exc_info[2])
                finally:
                    exc_info = None  # Avoid circular reference
            status_code = int(status.split(' ', 1)[0])  # Parse from "200 OK" format
            response = h11.Response(status_code=status_code, headers=[
                (name.encode('ISO-8859-1'), value.encode('ISO-8859-1'))
                for name, value in headers
            ])
            return _wsgi_response_write

        def wsgi_run():
            chunks = app(environ, wsgi_start_response)
            yield  # Skip the first iteration, to call the app and set the response object
            with closing(chunks) if hasattr(chunks, 'close') else suppress():  # type: ignore
                for chunk in chunks:
                    yield h11.Data(chunk)

        response_body = wsgi_run()
        next(response_body)
        assert response, "WSGI app did not call start_response"
        return response, closing(response_body)

    return handler


def _wsgi_response_write(_: bytes) -> None:
    raise NotImplementedError("write() is deprecated and not supported")


def _build_environ(client: ClientConn, request: h11.Request, body: RawIOBase) -> dict[str, Any]:
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
        'SERVER_NAME': client.config.host,
        'SERVER_PORT': str(client.server.port),
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

    from flask import Flask, request as flask_request

    from localpost.http.config import ServerConfig
    from localpost.http.server import start_http_server

    app = Flask(__name__)

    @app.route('/hello/<name>')
    def hello(name):
        user_agent = flask_request.headers.get('User-Agent', 'Unknown')
        return f'Hello, {name}! Your User-Agent is: {user_agent}\n'


    @app.route('/hello-stream/<name>')
    @stream_with_context
    def hello_stream(name):
        user_agent = flask_request.headers.get('User-Agent', 'Unknown')
        yield f'Hello, {name}! '
        yield f'Your User-Agent is: {user_agent}\n'

    handler = wrap_wsgi(app)
    with start_http_server(ServerConfig()) as server:
        for client_conn in server:
            client_conn(handler)


if __name__ == '__main__':
    _main_flask()
