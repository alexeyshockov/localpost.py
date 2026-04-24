"""Tests for localpost.http.wsgi."""

from __future__ import annotations

import threading

import httpx
import pytest

from localpost import threadtools
from localpost.http import ServerConfig, start_http_server, wrap_wsgi


@pytest.fixture(autouse=True)
def _disable_cancellation_check(monkeypatch):
    monkeypatch.setattr(threadtools, "check_cancelled", lambda: None)


@pytest.fixture
def server_config():
    return ServerConfig(host="127.0.0.1", port=0)


def _run_iterations(server, handler, n=20):
    for _ in range(n):
        try:
            server.run(handler)
        except (OSError, ValueError, AttributeError):
            return


class TestWrapWSGI:
    def test_simple_200(self, server_config):
        def app(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "5")])
            return [b"hello"]

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, wrap_wsgi(app)))
            t.start()
            resp = httpx.get(f"http://127.0.0.1:{server.port}/")
            t.join(timeout=5)

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/plain"
        assert resp.text == "hello"

    def test_multi_chunk_response(self, server_config):
        def app(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")])
            return [b"foo", b"bar", b"baz"]

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, wrap_wsgi(app)))
            t.start()
            resp = httpx.get(f"http://127.0.0.1:{server.port}/")
            t.join(timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"foobarbaz"

    def test_404(self, server_config):
        def app(environ, start_response):
            body = b"nope"
            start_response("404 Not Found", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
            return [body]

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, wrap_wsgi(app)))
            t.start()
            resp = httpx.get(f"http://127.0.0.1:{server.port}/anything")
            t.join(timeout=5)

        assert resp.status_code == 404
        assert resp.text == "nope"

    def test_environ_path_and_query(self, server_config):
        seen = {}

        def app(environ, start_response):
            seen["method"] = environ["REQUEST_METHOD"]
            seen["path"] = environ["PATH_INFO"]
            seen["query"] = environ["QUERY_STRING"]
            seen["content_type"] = environ.get("CONTENT_TYPE", "")
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
            return [b"ok"]

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, wrap_wsgi(app)))
            t.start()
            httpx.post(
                f"http://127.0.0.1:{server.port}/api/items?q=1",
                content=b"",
                headers={"Content-Type": "application/json"},
            )
            t.join(timeout=5)

        assert seen["method"] == "POST"
        assert seen["path"] == "/api/items"
        assert seen["query"] == "q=1"
        assert seen["content_type"] == "application/json"

    def test_request_body_streaming(self, server_config):
        received = bytearray()

        def app(environ, start_response):
            wsgi_input = environ["wsgi.input"]
            while True:
                chunk = wsgi_input.read(4)
                if not chunk:
                    break
                received.extend(chunk)
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", str(len(received)))])
            return [bytes(received)]

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, wrap_wsgi(app)))
            t.start()
            resp = httpx.post(f"http://127.0.0.1:{server.port}/", content=b"hello wsgi body")
            t.join(timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"hello wsgi body"
        assert bytes(received) == b"hello wsgi body"

    def test_generator_close_called(self, server_config):
        close_called = threading.Event()

        class Body:
            def __init__(self):
                self._chunks = iter([b"one", b"two"])

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._chunks)

            def close(self):
                close_called.set()

        def app(environ, start_response):
            start_response(
                "200 OK",
                [("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")],
            )
            return Body()

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, wrap_wsgi(app)))
            t.start()
            resp = httpx.get(f"http://127.0.0.1:{server.port}/")
            t.join(timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"onetwo"
        assert close_called.is_set()
