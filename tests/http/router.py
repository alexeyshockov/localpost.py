"""Tests for localpost.http.router."""

from __future__ import annotations

import threading
from http import HTTPMethod
from io import BytesIO

import httpx
import pytest

from localpost import threadtools
from localpost.http import RequestCtx, Response, Router, ServerConfig, URITemplate, start_http_server

# --- URITemplate ----------------------------------------------------------


class TestURITemplate:
    def test_literal_match(self):
        t = URITemplate.parse("/foo/bar")
        assert t.match("/foo/bar") == {}
        assert t.match("/foo/baz") is None

    def test_single_var_match(self):
        t = URITemplate.parse("/books/{id}")
        assert t.match("/books/42") == {"id": "42"}
        assert t.match("/books") is None
        assert t.match("/books/42/chapters") is None  # variable doesn't span slashes

    def test_multiple_vars_match(self):
        t = URITemplate.parse("/users/{uid}/posts/{pid}")
        assert t.match("/users/alice/posts/17") == {"uid": "alice", "pid": "17"}
        assert t.match("/users/alice") is None

    def test_variable_names_preserved(self):
        t = URITemplate.parse("/a/{x}/b/{y}")
        assert t.variable_names == ("x", "y")

    def test_non_matching_returns_none(self):
        t = URITemplate.parse("/hello/{name}")
        assert t.match("/goodbye/alice") is None


# --- Router.wsgi ----------------------------------------------------------


def _fake_wsgi(router: Router, method: str, path: str, query: str = "", body: bytes = b""):
    """Drive router.wsgi synchronously; returns (status_line, headers, body)."""
    captured: dict = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "wsgi.input": BytesIO(body),
    }
    out = router.wsgi(environ, start_response)
    return captured["status"], captured["headers"], b"".join(out)


class TestRouterWSGI:
    def test_matches_and_dispatches(self):
        router = Router()

        @router.get("/hello/{name}")
        def hello(ctx: RequestCtx) -> Response:
            return Response(200, {"content-type": "text/plain"}, [f"hi {ctx.path_args['name']}".encode()])

        assert hello  # keep reference so pyright is happy

        status, _, body = _fake_wsgi(router, "GET", "/hello/alice")
        assert status.startswith("200")
        assert body == b"hi alice"

    def test_404_on_miss(self):
        router = Router()
        status, _, body = _fake_wsgi(router, "GET", "/nope")
        assert status.startswith("404")
        assert body == b"Not Found"

    def test_405_with_allow_header(self):
        router = Router()

        @router.post("/resource")
        def create(_: RequestCtx) -> Response:
            return Response(201, {}, [])

        assert create

        status, headers, body = _fake_wsgi(router, "GET", "/resource")
        assert status.startswith("405")
        assert body == b"Method Not Allowed"
        header_names = {name.lower(): value for name, value in headers}
        assert header_names.get("allow") == "POST"

    def test_query_parsing(self):
        router = Router()

        @router.get("/search")
        def search(ctx: RequestCtx) -> Response:
            q = ctx.query_args.get("q", [""])[0]
            return Response(200, {"content-type": "text/plain"}, [q.encode()])

        assert search

        status, _, body = _fake_wsgi(router, "GET", "/search", query="q=books&extra=1")
        assert status.startswith("200")
        assert body == b"books"

    def test_multiple_methods_same_template(self):
        router = Router()

        @router.get("/item")
        def _get(_: RequestCtx) -> Response:
            return Response(200, {}, [b"g"])

        @router.post("/item")
        def _post(_: RequestCtx) -> Response:
            return Response(200, {}, [b"p"])

        assert _get is not None
        assert _post is not None

        s_g, _, b_g = _fake_wsgi(router, "GET", "/item")
        s_p, _, b_p = _fake_wsgi(router, "POST", "/item")
        assert s_g.startswith("200")
        assert b_g == b"g"
        assert s_p.startswith("200")
        assert b_p == b"p"

    def test_request_body_read(self):
        router = Router()

        @router.post("/echo")
        def echo(ctx: RequestCtx) -> Response:
            return Response(200, {}, [ctx.body()])

        assert echo

        status, _, body = _fake_wsgi(router, "POST", "/echo", body=b"payload")
        assert status.startswith("200")
        assert body == b"payload"


# --- Router.as_handler (native) -------------------------------------------


@pytest.fixture(autouse=True)
def _disable_cancellation_check(monkeypatch):
    monkeypatch.setattr(threadtools, "check_cancelled", lambda: None)


@pytest.fixture
def server_config():
    return ServerConfig(host="127.0.0.1", port=0)


def _run_iterations(server, handler, n=15):
    for _ in range(n):
        try:
            server.run(handler)
        except (OSError, ValueError, AttributeError):
            return  # Server context manager exited; selector/socket closed


class TestRouterAsHandler:
    def test_dispatch_200(self, server_config):
        router = Router()

        @router.get("/ping")
        def ping(_: RequestCtx) -> Response:
            return Response(200, {"content-type": "text/plain"}, [b"pong"])

        assert ping

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, router.as_handler()))
            t.start()
            resp = httpx.get(f"http://127.0.0.1:{server.port}/ping")
            t.join(timeout=5)

        assert resp.status_code == 200
        assert resp.text == "pong"

    def test_dispatch_path_params(self, server_config):
        router = Router()
        captured: dict = {}

        @router.get("/books/{book_id}")
        def get_book(ctx: RequestCtx) -> Response:
            captured["book_id"] = ctx.path_args["book_id"]
            captured["method"] = ctx.method
            captured["matched_template"] = ctx.matched_template.template
            return Response(200, {"content-type": "text/plain"}, [b"ok"])

        assert get_book

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, router.as_handler()))
            t.start()
            httpx.get(f"http://127.0.0.1:{server.port}/books/xyz-123")
            t.join(timeout=5)

        assert captured["book_id"] == "xyz-123"
        assert captured["method"] == HTTPMethod.GET
        assert captured["matched_template"] == "/books/{book_id}"

    def test_404(self, server_config):
        router = Router()

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, router.as_handler()))
            t.start()
            resp = httpx.get(f"http://127.0.0.1:{server.port}/does-not-exist")
            t.join(timeout=5)

        assert resp.status_code == 404
        assert resp.text == "Not Found"

    def test_405_with_allow_header(self, server_config):
        router = Router()

        @router.post("/resource")
        def _post(_: RequestCtx) -> Response:
            return Response(201, {}, [])

        assert _post

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, router.as_handler()))
            t.start()
            resp = httpx.get(f"http://127.0.0.1:{server.port}/resource")
            t.join(timeout=5)

        assert resp.status_code == 405
        assert resp.headers.get("allow") == "POST"

    def test_query_parsing(self, server_config):
        router = Router()
        captured: dict = {}

        @router.get("/search")
        def search(ctx: RequestCtx) -> Response:
            captured["q"] = ctx.query_args.get("q", [""])[0]
            captured["raw"] = ctx.query_string
            return Response(200, {"content-type": "text/plain"}, [b"ok"])

        assert search

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, router.as_handler()))
            t.start()
            httpx.get(f"http://127.0.0.1:{server.port}/search?q=books")
            t.join(timeout=5)

        assert captured["q"] == "books"
        assert captured["raw"] == "q=books"

    def test_streaming_response(self, server_config):
        router = Router()

        @router.get("/stream")
        def stream(_: RequestCtx) -> Response:
            return Response(
                200,
                {"transfer-encoding": "chunked", "content-type": "text/plain"},
                [b"first", b"second", b"third"],
            )

        assert stream

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, router.as_handler()))
            t.start()
            resp = httpx.get(f"http://127.0.0.1:{server.port}/stream")
            t.join(timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"firstsecondthird"

    def test_handler_sees_request_headers(self, server_config):
        router = Router()
        captured: dict = {}

        @router.get("/hdr")
        def hdr(ctx: RequestCtx) -> Response:
            captured["x_custom"] = ctx.headers.get("x-custom")
            return Response(200, {}, [b"ok"])

        assert hdr

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, router.as_handler()))
            t.start()
            httpx.get(f"http://127.0.0.1:{server.port}/hdr", headers={"X-Custom": "yo"})
            t.join(timeout=5)

        assert captured["x_custom"] == "yo"

    def test_ctx_can_read_body(self, server_config):
        router = Router()
        captured: dict = {}

        @router.post("/echo")
        def echo(ctx: RequestCtx) -> Response:
            captured["body"] = ctx.body()
            return Response(200, {}, [captured["body"]])

        assert echo

        with start_http_server(server_config) as server:
            t = threading.Thread(target=_run_iterations, args=(server, router.as_handler(), 25))
            t.start()
            resp = httpx.post(f"http://127.0.0.1:{server.port}/echo", content=b"hello body")
            t.join(timeout=5)

        assert resp.status_code == 200
        assert captured["body"] == b"hello body"


# --- Router registration API ---------------------------------------------


class TestRouterRegistration:
    def test_explicit_add(self):
        router = Router()

        def handler(_: RequestCtx) -> Response:
            return Response(200, {}, [b"x"])

        router.add("GET", "/thing", handler)
        assert len(router.paths) == 1

    def test_same_template_multiple_methods_stored_once(self):
        router = Router()

        @router.get("/r")
        def g(_: RequestCtx) -> Response:
            return Response(200, {}, [])

        @router.post("/r")
        def p(_: RequestCtx) -> Response:
            return Response(200, {}, [])

        assert g is not None
        assert p is not None

        assert len(router.paths) == 1
        methods = next(iter(router.paths.values()))
        assert set(methods) == {HTTPMethod.GET, HTTPMethod.POST}

    def test_decorator_returns_original_handler(self):
        router = Router()

        def handler(_: RequestCtx) -> Response:
            return Response(200, {}, [])

        registered = router.get("/x")(handler)
        assert registered is handler
