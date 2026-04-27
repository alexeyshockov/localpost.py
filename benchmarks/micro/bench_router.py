"""Micro-benchmarks for ``Router`` build + dispatch.

Run::

    just bench-micro
    # or
    pytest benchmarks/micro/bench_router.py --benchmark-only

Build cost is measured separately from dispatch. Dispatch is exercised via
the WSGI surface (synthetic ``environ`` dict) — it's the simplest synchronous
entry point that goes through the full match → handler → response pipeline.
"""

from __future__ import annotations

from io import BytesIO

from localpost.http.router import RequestCtx, Response, Routes


def _ok(_: RequestCtx) -> Response:
    return Response(200, {"content-type": "text/plain"}, [b"ok"])


def _build_routes_20() -> Routes:
    """A mixed set of 20 routes — 10 literal, 10 parameterised."""
    routes = Routes()
    # Literal routes
    for path in (
        "/",
        "/health",
        "/ready",
        "/metrics",
        "/version",
        "/login",
        "/logout",
        "/me",
        "/admin",
        "/admin/dashboard",
    ):
        routes.get(path)(_ok)
    # Parameterised routes
    for path in (
        "/books/{id}",
        "/users/{id}",
        "/users/{id}/posts",
        "/users/{id}/posts/{post}",
        "/orgs/{org}/users/{user}",
        "/orgs/{org}/users/{user}/posts/{post}",
        "/files/{bucket}/{key}",
        "/v1/items/{id}",
        "/v1/items/{id}/comments",
        "/v1/items/{id}/comments/{cid}",
    ):
        routes.get(path)(_ok)
    return routes


def _wsgi_environ(method: str, path: str, body: bytes = b"") -> dict:
    return {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "wsgi.input": BytesIO(body),
    }


def _noop_start_response(*_args, **_kwargs) -> None:
    return None


def bench_routes_build_20(benchmark) -> None:
    routes = _build_routes_20()
    benchmark(routes.build)


def bench_dispatch_literal_first(benchmark) -> None:
    """Dispatch a literal route — should be the cheapest path."""
    router = _build_routes_20().build()
    env = _wsgi_environ("GET", "/health")
    benchmark(router.wsgi, env, _noop_start_response)


def bench_dispatch_param_shallow(benchmark) -> None:
    """Dispatch ``/books/{id}`` — one variable."""
    router = _build_routes_20().build()
    env = _wsgi_environ("GET", "/books/00a7a2d4-18e4-11f1-899b-d33838f3bef0")
    benchmark(router.wsgi, env, _noop_start_response)


def bench_dispatch_param_deep(benchmark) -> None:
    """Dispatch ``/orgs/{org}/users/{user}/posts/{post}`` — three variables, longest path."""
    router = _build_routes_20().build()
    env = _wsgi_environ("GET", "/orgs/anthropic/users/alex/posts/42")
    benchmark(router.wsgi, env, _noop_start_response)


def bench_dispatch_404(benchmark) -> None:
    """Path matches no route."""
    router = _build_routes_20().build()
    env = _wsgi_environ("GET", "/nope/nothing/here")
    benchmark(router.wsgi, env, _noop_start_response)


def bench_dispatch_405(benchmark) -> None:
    """Path matches but method doesn't — exercises the method-not-allowed branch."""
    router = _build_routes_20().build()
    env = _wsgi_environ("DELETE", "/health")
    benchmark(router.wsgi, env, _noop_start_response)
