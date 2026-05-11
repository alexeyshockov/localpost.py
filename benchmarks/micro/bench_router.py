"""Micro-benchmarks for ``Router`` build + dispatch.

Run::

    just bench-micro
    # or
    pytest benchmarks/micro/bench_router.py --benchmark-only

Build cost is measured separately from dispatch. Dispatch is exercised via
the bare ``Router._match`` — the dispatch heart, with no I/O, ctx
construction, or response writing.
"""

from __future__ import annotations

from localpost.http import HTTPReqCtx, Response, Routes


def _ok(ctx: HTTPReqCtx):
    ctx.complete(Response(status_code=200, headers=[(b"content-length", b"2")]), b"ok")


def _build_routes_20() -> Routes:
    """A mixed set of 20 routes — 10 literal, 10 parameterised."""
    routes = Routes()
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


def bench_routes_build_20(benchmark) -> None:
    routes = _build_routes_20()
    benchmark(routes.build)


def bench_dispatch_literal_first(benchmark) -> None:
    """Match a literal route — should be the cheapest path."""
    router = _build_routes_20().build()
    benchmark(router._match, "/health", "GET")


def bench_dispatch_param_shallow(benchmark) -> None:
    """Match ``/books/{id}`` — one variable."""
    router = _build_routes_20().build()
    benchmark(router._match, "/books/00a7a2d4-18e4-11f1-899b-d33838f3bef0", "GET")


def bench_dispatch_param_deep(benchmark) -> None:
    """Match ``/orgs/{org}/users/{user}/posts/{post}`` — three variables, longest path."""
    router = _build_routes_20().build()
    benchmark(router._match, "/orgs/anthropic/users/alex/posts/42", "GET")


def bench_dispatch_404(benchmark) -> None:
    """Path matches no route."""
    router = _build_routes_20().build()
    benchmark(router._match, "/nope/nothing/here", "GET")


def bench_dispatch_405(benchmark) -> None:
    """Path matches but method doesn't — exercises the method-not-allowed branch."""
    router = _build_routes_20().build()
    benchmark(router._match, "/health", "DELETE")
