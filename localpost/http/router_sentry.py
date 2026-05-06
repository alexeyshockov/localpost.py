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

    async with thread_pool_handler(handler) as wrapped:
        async with http_server(config, wrapped):
            ...

Pure Router instrumentation — no Flask import. Use
:mod:`localpost.http.flask_sentry` for Flask apps.
"""

from __future__ import annotations

from localpost.http._base import HTTPReqCtx, RequestHandler
from localpost.http._sentry import request_transaction
from localpost.http.router import Router, _MatchOk

__all__ = ["sentry_router_handler"]


def sentry_router_handler(router: Router, *, op: str = "http.server") -> RequestHandler:
    """Wrap a :class:`Router` with a Sentry transaction per request.

    The transaction spans the full request handler invocation. 404 / 405
    paths complete inline and end the transaction in the same call.
    """
    inner = router.as_handler()

    def handle(ctx: HTTPReqCtx) -> None:
        req = ctx.request
        method = req.method.decode("ascii")
        target = req.target.decode("iso-8859-1")
        path = req.path.decode("iso-8859-1")

        # Pre-match so the transaction name uses the URI template (low
        # cardinality). Router's dispatch will match again — cheap.
        match = router._match(path, method)
        if isinstance(match, _MatchOk):
            tx_name = f"{method} {match.match.matched_template.template}"
            source = "route"
        else:
            tx_name = f"{method} {path}"
            source = "url"

        with request_transaction(op=op, name=tx_name, source=source, method=method, url=target) as tx:
            try:
                inner(ctx)
            finally:
                if ctx.response_status is not None:
                    tx.set_http_status(ctx.response_status)

    return handle
