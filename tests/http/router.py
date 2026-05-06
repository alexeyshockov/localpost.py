"""Tests for localpost.http.router — the lean dispatcher.

The Router attaches a :class:`RouteMatch` to ``ctx.attrs[RouteMatch]``
and delegates to the registered :class:`localpost.http.RequestHandler`.
404 / 405 are answered inline. Pythonic helpers (response converters,
param injection, etc.) live in ``HttpApp``; tests for those live elsewhere.
"""

from __future__ import annotations

from http import HTTPMethod
from unittest.mock import Mock

import httpx
import pytest

from localpost.http import (
    HTTPReqCtx,
    Response,
    RouteMatch,
    Routes,
    URITemplate,
    route_match,
)


def _ok_handler(body: bytes = b"ok"):
    """Build a minimal HTTPReqCtx-shape handler that emits 200 + ``body``."""

    def handler(ctx: HTTPReqCtx) -> None:
        ctx.complete(
            Response(
                status_code=200,
                headers=[
                    (b"content-type", b"text/plain"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            ),
            body,
        )
        return None

    return handler


# --- URITemplate ---------------------------------------------------------


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


# --- Router.as_handler ---------------------------------------------------


class TestRouterDispatch:
    def test_dispatch_200(self, serve_in_thread):
        routes = Routes()
        routes.get("/ping")(_ok_handler(b"pong"))
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5)

        assert resp.status_code == 200
        assert resp.text == "pong"

    def test_404(self, serve_in_thread):
        router = Routes().build()
        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/does-not-exist", timeout=5)
        assert resp.status_code == 404
        assert resp.text == "Not Found"

    def test_405_with_allow_header(self, serve_in_thread):
        routes = Routes()
        routes.post("/resource")(_ok_handler(b"x"))
        router = routes.build()
        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/resource", timeout=5)
        assert resp.status_code == 405
        assert resp.headers.get("allow") == "POST"


class TestRouteMatchAttached:
    def test_match_in_attrs(self, serve_in_thread):
        captured: dict = {}

        def handler(ctx: HTTPReqCtx) -> None:
            m = route_match(ctx)
            assert isinstance(m, RouteMatch)
            captured["method"] = m.method
            captured["template"] = m.matched_template.template
            captured["path_args"] = dict(m.path_args)
            ctx.complete(
                Response(
                    status_code=200,
                    headers=[(b"content-length", b"2")],
                ),
                b"ok",
            )
            return None

        routes = Routes()
        routes.get("/books/{book_id}")(handler)
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            httpx.get(f"http://127.0.0.1:{port}/books/xyz-123", timeout=5)

        assert captured["method"] == HTTPMethod.GET
        assert captured["template"] == "/books/{book_id}"
        assert captured["path_args"] == {"book_id": "xyz-123"}

    def test_route_match_outside_router_raises(self):
        # ``route_match`` reads ``ctx.attrs[RouteMatch]``; outside a router
        # the key is missing and we get the natural KeyError. Documented.
        ctx = Mock()
        ctx.attrs = {}
        with pytest.raises(KeyError):
            route_match(ctx)


class TestRoutesRegistration:
    def test_explicit_add(self):
        routes = Routes()
        routes.add("GET", "/thing", _ok_handler())
        assert len(routes.paths) == 1

    def test_same_template_multiple_methods_stored_once(self):
        routes = Routes()
        routes.get("/r")(_ok_handler(b"g"))
        routes.post("/r")(_ok_handler(b"p"))
        assert len(routes.paths) == 1
        methods = next(iter(routes.paths.values()))
        assert set(methods) == {HTTPMethod.GET, HTTPMethod.POST}

    def test_decorator_returns_original_handler(self):
        routes = Routes()
        h = _ok_handler()
        assert routes.get("/x")(h) is h

    def test_method_string_normalized(self):
        routes = Routes()
        routes.add("get", "/a", _ok_handler())
        routes.add("PoSt", "/a", _ok_handler())
        methods = next(iter(routes.paths.values()))
        assert set(methods) == {HTTPMethod.GET, HTTPMethod.POST}


class TestRouterBuild:
    def test_empty_routes_produce_empty_router(self, serve_in_thread):
        """An empty Routes still builds a valid (never-matching) Router."""
        router = Routes().build()
        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5)
        assert resp.status_code == 404

    def test_longest_literal_prefix_first(self):
        """More specific routes (longer literal prefix before the first var)
        match before less specific ones."""
        routes = Routes()
        routes.get("/{anything}")(_ok_handler(b"any"))
        routes.get("/specific")(_ok_handler(b"specific"))
        router = routes.build()
        # The first route in dispatch order should be ``/specific`` (longer literal prefix).
        assert router.routes[0].template.template == "/specific"
        assert router.routes[1].template.template == "/{anything}"

    def test_dispatch_order_specific_before_general(self, serve_in_thread):
        routes = Routes()
        routes.get("/{anything}")(_ok_handler(b"any"))
        routes.get("/specific")(_ok_handler(b"specific"))
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            r_specific = httpx.get(f"http://127.0.0.1:{port}/specific", timeout=5)
            r_other = httpx.get(f"http://127.0.0.1:{port}/foo", timeout=5)

        assert r_specific.text == "specific"
        assert r_other.text == "any"


class TestMethodDispatch:
    def test_get_post_separate_handlers(self, serve_in_thread):
        routes = Routes()
        routes.get("/item")(_ok_handler(b"got"))
        routes.post("/item")(_ok_handler(b"posted"))
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            r_get = httpx.get(f"http://127.0.0.1:{port}/item", timeout=5)
            r_post = httpx.post(f"http://127.0.0.1:{port}/item", timeout=5)

        assert r_get.text == "got"
        assert r_post.text == "posted"

    def test_unknown_method_returns_405(self, serve_in_thread):
        routes = Routes()
        routes.get("/r")(_ok_handler())
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.request("PATCH", f"http://127.0.0.1:{port}/r", timeout=5)
        assert resp.status_code == 405

    def test_overlapping_templates_scan_for_matching_method(self, serve_in_thread):
        routes = Routes()
        routes.get("/users/{id}")(_ok_handler(b"get"))
        routes.post("/users/{name}")(_ok_handler(b"post"))
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.post(f"http://127.0.0.1:{port}/users/42", timeout=5)

        assert resp.status_code == 200
        assert resp.text == "post"

    def test_overlapping_templates_no_matching_method_returns_405(self, serve_in_thread):
        routes = Routes()
        routes.get("/users/{id}")(_ok_handler(b"get"))
        routes.post("/users/{name}")(_ok_handler(b"post"))
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.patch(f"http://127.0.0.1:{port}/users/42", timeout=5)

        assert resp.status_code == 405

    def test_overlapping_templates_405_allow_header_is_sorted_union(self, serve_in_thread):
        routes = Routes()
        routes.post("/users/{name}")(_ok_handler(b"post"))
        routes.delete("/users/{id}")(_ok_handler(b"delete"))
        routes.get("/users/{slug}")(_ok_handler(b"get"))
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            resp = httpx.patch(f"http://127.0.0.1:{port}/users/42", timeout=5)

        assert resp.status_code == 405
        assert resp.headers.get("allow") == "DELETE, GET, POST"


class TestPathVariableEncoding:
    def test_url_encoded_path_arg_passed_through(self, serve_in_thread):
        captured: dict = {}

        def handler(ctx: HTTPReqCtx) -> None:
            m = route_match(ctx)
            captured["name"] = m.path_args["name"]
            ctx.complete(Response(200, [(b"content-length", b"2")]), b"ok")
            return None

        routes = Routes()
        routes.get("/u/{name}")(handler)
        router = routes.build()

        with serve_in_thread(router.as_handler()) as port:
            # Two requests: simple value, and url-encoded value.
            httpx.get(f"http://127.0.0.1:{port}/u/alice", timeout=5)
            assert captured["name"] == "alice"

            httpx.get(f"http://127.0.0.1:{port}/u/al%20ice", timeout=5)
            # Router doesn't decode — the handler sees the raw URL-encoded value.
            # That's the lean-dispatcher contract; the framework layer can decode.
            assert captured["name"] == "al%20ice"
