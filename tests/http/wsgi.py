"""Tests for localpost.http.wsgi."""

from __future__ import annotations

import threading

import httpx

from localpost.http import wrap_wsgi


class TestWrapWSGI:
    def test_simple_200(self, serve_in_thread):
        def app(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "5")])
            return [b"hello"]

        with serve_in_thread(wrap_wsgi(app)) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/plain"
        assert resp.text == "hello"

    def test_multi_chunk_response(self, serve_in_thread):
        def app(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")])
            return [b"foo", b"bar", b"baz"]

        with serve_in_thread(wrap_wsgi(app)) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"foobarbaz"

    def test_404(self, serve_in_thread):
        def app(environ, start_response):
            body = b"nope"
            start_response("404 Not Found", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
            return [body]

        with serve_in_thread(wrap_wsgi(app)) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/anything", timeout=5)

        assert resp.status_code == 404
        assert resp.text == "nope"

    def test_environ_path_and_query(self, serve_in_thread):
        seen = {}

        def app(environ, start_response):
            seen["method"] = environ["REQUEST_METHOD"]
            seen["path"] = environ["PATH_INFO"]
            seen["query"] = environ["QUERY_STRING"]
            seen["content_type"] = environ.get("CONTENT_TYPE", "")
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
            return [b"ok"]

        with serve_in_thread(wrap_wsgi(app)) as port:
            httpx.post(
                f"http://127.0.0.1:{port}/api/items?q=1",
                content=b"",
                headers={"Content-Type": "application/json"},
                timeout=5,
            )

        assert seen["method"] == "POST"
        assert seen["path"] == "/api/items"
        assert seen["query"] == "q=1"
        assert seen["content_type"] == "application/json"

    def test_environ_path_info_is_percent_decoded(self, serve_in_thread):
        seen = {}

        def app(environ, start_response):
            seen["path"] = environ["PATH_INFO"]
            seen["query"] = environ["QUERY_STRING"]
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
            return [b"ok"]

        with serve_in_thread(wrap_wsgi(app)) as port:
            httpx.get(f"http://127.0.0.1:{port}/users/al%20ice?q=a%20b", timeout=5)

        assert seen == {"path": "/users/al ice", "query": "q=a%20b"}

    def test_request_body_streaming(self, serve_in_thread):
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

        with serve_in_thread(wrap_wsgi(app)) as port:
            resp = httpx.post(f"http://127.0.0.1:{port}/", content=b"hello wsgi body", timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"hello wsgi body"
        assert bytes(received) == b"hello wsgi body"

    def test_generator_close_called(self, serve_in_thread):
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

        with serve_in_thread(wrap_wsgi(app)) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"onetwo"
        assert close_called.is_set()
