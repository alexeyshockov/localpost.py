"""Sentry tracing wrapper for :class:`localpost.http.Router`.

Each request becomes a Sentry transaction named after the matched URI template
(e.g. ``"GET /books/{id}"``) — a low-cardinality identifier suitable for
Sentry's transaction metrics. Unmatched URLs fall back to the raw path with
``source="url"``.

Install::

    pip install localpost[http-sentry]

Usage::

    routes = Routes()
    @routes.get("/books/{id}")
    def get_book(ctx): ...
    router = routes.build()

    sentry_sdk.init(dsn=..., traces_sample_rate=1.0)
    handler = sentry_router_handler(router)

    async with thread_pool_handler(handler, max_concurrency=16) as wrapped:
        async with http_server(config, wrapped):
            ...

Pure Router instrumentation — no Flask import. Use
:mod:`localpost.http.flask_sentry` for Flask apps.
"""

from __future__ import annotations

import sys

import sentry_sdk

from localpost.http.router import Router, _MatchOk
from localpost.http.server import BodyHandler, HTTPReqCtx, RequestHandler

__all__ = ["sentry_router_handler"]


def sentry_router_handler(router: Router, *, op: str = "http.server") -> RequestHandler:
    """Wrap a :class:`Router` with a Sentry transaction per request.

    The transaction spans the full request, including the post-body
    continuation when the router defers to one. 404 / 405 paths complete
    inline and end the transaction in the same call.
    """
    inner = router.as_handler()

    def handle(ctx: HTTPReqCtx) -> BodyHandler | None:
        req = ctx.request
        method = req.method.decode("ascii")
        target = req.target.decode("iso-8859-1")
        path = target.split("?", 1)[0] if "?" in target else target

        # Pre-match so the transaction name uses the URI template (low
        # cardinality). Router's dispatch will match again — cheap.
        match = router._match(path, method)
        if isinstance(match, _MatchOk):
            tx_name = f"{method} {match.matched_template.template}"
            source = "route"
        else:
            tx_name = f"{method} {path}"
            source = "url"

        scope_cm = sentry_sdk.isolation_scope()
        scope_cm.__enter__()
        tx_cm = sentry_sdk.start_transaction(op=op, name=tx_name, source=source)
        tx = tx_cm.__enter__()
        tx.set_tag("http.method", method)
        tx.set_data("http.url", target)

        def finalize(exc_info: tuple = (None, None, None)) -> None:
            if ctx.response_status is not None:
                tx.set_http_status(ctx.response_status)
            tx_cm.__exit__(*exc_info)
            scope_cm.__exit__(*exc_info)

        try:
            result = inner(ctx)
        except BaseException:
            finalize(sys.exc_info())
            raise

        if result is None:
            finalize()
            return None

        def wrapped(req_ctx: HTTPReqCtx) -> None:
            try:
                result(req_ctx)
            except BaseException:
                finalize(sys.exc_info())
                raise
            finalize()

        return wrapped

    return handle
