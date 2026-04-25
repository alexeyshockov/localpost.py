"""Tests for localpost.http.router."""

from __future__ import annotations

from http import HTTPMethod
from io import BytesIO

import httpx

from localpost.http import (
    RequestCtx,
    Response,
    Router,
    Routes,
    URITemplate,
)

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
        routes = Routes()

        @routes.get("/hello/{name}")
        def hello(ctx: RequestCtx) -> Response:
            return Response(200, {"content-type": "text/plain"}, [f"hi {ctx.path_args['name']}".encode()])

        assert hello  # keep reference so pyright is happy
        router = routes.build()

        status, _, body = _fake_wsgi(router, "GET", "/hello/alice")
        assert status.startswith("200")
        assert body == b"hi alice"

    def test_404_on_miss(self):
        router = Routes().build()
        status, _, body = _fake_wsgi(router, "GET", "/nope")
        assert status.startswith("404")
        assert body == b"Not Found"

    def test_405_with_allow_header(self):
        routes = Routes()

        @routes.post("/resource")
        def create(_: RequestCtx) -> Response:
            return Response(201, {}, [])

        assert create
        router = routes.build()

        status, headers, body = _fake_wsgi(router, "GET", "/resource")
        assert status.startswith("405")
        assert body == b"Method Not Allowed"
        header_names = {name.lower(): value for name, value in headers}
        assert header_names.get("allow") == "POST"

    def test_query_parsing(self):
        routes = Routes()

        @routes.get("/search")
        def search(ctx: RequestCtx) -> Response:
            q = ctx.query_args.get("q", [""])[0]
            return Response(200, {"content-type": "text/plain"}, [q.encode()])

        assert search
        router = routes.build()

        status, _, body = _fake_wsgi(router, "GET", "/search", query="q=books&extra=1")
        assert status.startswith("200")
        assert body == b"books"

    def test_multiple_methods_same_template(self):
        routes = Routes()

        @routes.get("/item")
        def _get(_: RequestCtx) -> Response:
            return Response(200, {}, [b"g"])

        @routes.post("/item")
        def _post(_: RequestCtx) -> Response:
            return Response(200, {}, [b"p"])

        assert _get is not None
        assert _post is not None
        router = routes.build()

        s_g, _, b_g = _fake_wsgi(router, "GET", "/item")
        s_p, _, b_p = _fake_wsgi(router, "POST", "/item")
        assert s_g.startswith("200")
        assert b_g == b"g"
        assert s_p.startswith("200")
        assert b_p == b"p"

    def test_request_body_read(self):
        routes = Routes()

        @routes.post("/echo")
        def echo(ctx: RequestCtx) -> Response:
            return Response(200, {}, [ctx.body()])

        assert echo
        router = routes.build()

        status, _, body = _fake_wsgi(router, "POST", "/echo", body=b"payload")
        assert status.startswith("200")
        assert body == b"payload"

    def test_longer_literal_prefix_wins(self):
        """Ambiguous templates: /books/new should beat /books/{id}."""
        routes = Routes()
        hits: list[str] = []

        @routes.get("/books/{id}")
        def get_book(_: RequestCtx) -> Response:
            hits.append("by_id")
            return Response(200, {}, [b"id"])

        @routes.get("/books/new")
        def new_book(_: RequestCtx) -> Response:
            hits.append("new")
            return Response(200, {}, [b"new"])

        assert get_book is not None
        assert new_book is not None
        router = routes.build()

        _fake_wsgi(router, "GET", "/books/new")
        _fake_wsgi(router, "GET", "/books/42")

        assert hits == ["new", "by_id"]


# --- Router.as_handler (native) -------------------------------------------


class TestRouterAsHandler:
    def test_dispatch_200(self, serve_in_thread):
        routes = Routes()

        @routes.get("/ping")
        def ping(_: RequestCtx) -> Response:
            return Response(200, {"content-type": "text/plain"}, [b"pong"])

        assert ping
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5)

        assert resp.status_code == 200
        assert resp.text == "pong"

    def test_dispatch_path_params(self, serve_in_thread):
        routes = Routes()
        captured: dict = {}

        @routes.get("/books/{book_id}")
        def get_book(ctx: RequestCtx) -> Response:
            captured["book_id"] = ctx.path_args["book_id"]
            captured["method"] = ctx.method
            captured["matched_template"] = ctx.matched_template.template
            return Response(200, {"content-type": "text/plain"}, [b"ok"])

        assert get_book
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            httpx.get(f"http://127.0.0.1:{port}/books/xyz-123", timeout=5)

        assert captured["book_id"] == "xyz-123"
        assert captured["method"] == HTTPMethod.GET
        assert captured["matched_template"] == "/books/{book_id}"

    def test_404(self, serve_in_thread):
        router = Routes().build()

        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/does-not-exist", timeout=5)

        assert resp.status_code == 404
        assert resp.text == "Not Found"

    def test_405_with_allow_header(self, serve_in_thread):
        routes = Routes()

        @routes.post("/resource")
        def _post(_: RequestCtx) -> Response:
            return Response(201, {}, [])

        assert _post
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/resource", timeout=5)

        assert resp.status_code == 405
        assert resp.headers.get("allow") == "POST"

    def test_query_parsing(self, serve_in_thread):
        routes = Routes()
        captured: dict = {}

        @routes.get("/search")
        def search(ctx: RequestCtx) -> Response:
            captured["q"] = ctx.query_args.get("q", [""])[0]
            captured["raw"] = ctx.query_string
            return Response(200, {"content-type": "text/plain"}, [b"ok"])

        assert search
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            httpx.get(f"http://127.0.0.1:{port}/search?q=books", timeout=5)

        assert captured["q"] == "books"
        assert captured["raw"] == "q=books"

    def test_streaming_response(self, serve_in_thread):
        routes = Routes()

        @routes.get("/stream")
        def stream(_: RequestCtx) -> Response:
            return Response(
                200,
                {"transfer-encoding": "chunked", "content-type": "text/plain"},
                [b"first", b"second", b"third"],
            )

        assert stream
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/stream", timeout=5)

        assert resp.status_code == 200
        assert resp.content == b"firstsecondthird"

    def test_handler_sees_request_headers(self, serve_in_thread):
        routes = Routes()
        captured: dict = {}

        @routes.get("/hdr")
        def hdr(ctx: RequestCtx) -> Response:
            captured["x_custom"] = ctx.headers.get("x-custom")
            return Response(200, {}, [b"ok"])

        assert hdr
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            httpx.get(f"http://127.0.0.1:{port}/hdr", headers={"X-Custom": "yo"}, timeout=5)

        assert captured["x_custom"] == "yo"

    def test_ctx_can_read_body(self, serve_in_thread):
        routes = Routes()
        captured: dict = {}

        @routes.post("/echo")
        def echo(ctx: RequestCtx) -> Response:
            captured["body"] = ctx.body()
            return Response(200, {}, [captured["body"]])

        assert echo
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.post(f"http://127.0.0.1:{port}/echo", content=b"hello body", timeout=5)

        assert resp.status_code == 200
        assert captured["body"] == b"hello body"


# --- Routes registration API ---------------------------------------------


class TestRoutesRegistration:
    def test_explicit_add(self):
        routes = Routes()

        def handler(_: RequestCtx) -> Response:
            return Response(200, {}, [b"x"])

        routes.add("GET", "/thing", handler)
        assert len(routes.paths) == 1

    def test_same_template_multiple_methods_stored_once(self):
        routes = Routes()

        @routes.get("/r")
        def g(_: RequestCtx) -> Response:
            return Response(200, {}, [])

        @routes.post("/r")
        def p(_: RequestCtx) -> Response:
            return Response(200, {}, [])

        assert g is not None
        assert p is not None

        assert len(routes.paths) == 1
        methods = next(iter(routes.paths.values()))
        assert set(methods) == {HTTPMethod.GET, HTTPMethod.POST}

    def test_decorator_returns_original_handler(self):
        routes = Routes()

        def handler(_: RequestCtx) -> Response:
            return Response(200, {}, [])

        registered = routes.get("/x")(handler)
        assert registered is handler


# --- Router build-time checks --------------------------------------------


class TestRouterBuild:
    def test_empty_routes_produce_empty_router(self):
        """An empty Routes still builds a valid (never-matching) Router."""
        router = Routes().build()
        status, _, body = _fake_wsgi(router, "GET", "/")
        assert status.startswith("404")
        assert body == b"Not Found"

    def test_build_is_idempotent_snapshot(self):
        """Building twice produces equivalent routers with the same routes order."""
        routes = Routes()
        routes.add("GET", "/a", lambda ctx: Response(200, {}, []))
        routes.add("GET", "/b", lambda ctx: Response(200, {}, []))
        r1 = routes.build()
        r2 = routes.build()
        assert [r.template.template for r in r1.routes] == [r.template.template for r in r2.routes]

    def test_allow_header_precomputed(self):
        routes = Routes()
        routes.add("GET", "/x", lambda ctx: Response(200, {}, []))
        routes.add("POST", "/x", lambda ctx: Response(200, {}, []))
        router = routes.build()
        # Methods are sorted for a stable header; both methods end up in the header.
        assert router.routes[0].allow_header in ("GET, POST", "POST, GET")
        assert set(router.routes[0].allow_header.split(", ")) == {"GET", "POST"}
