"""Sentry tracing wrapper for :func:`localpost.http.flask.flask_handler`.

Each request becomes a Sentry transaction that **covers the entire request
lifetime including response-body streaming**. Sentry's stock
``FlaskIntegration`` ends the transaction when the WSGI ``wsgi_app`` returns,
which is *before* the body is iterated — so any spans / errors inside a
streaming generator land outside the transaction. This adapter doesn't have
that bug because we hold the transaction open through ``response.iter_encoded()``.

The transaction is named after Flask's matched URL rule (e.g.
``"GET /books/<id>"``) once routing has run; before then it's ``METHOD path``.

Install::

    pip install localpost[http-flask,http-sentry]

Usage::

    sentry_sdk.init(dsn=..., traces_sample_rate=1.0)
    handler = sentry_flask_handler(my_flask_app)
    async with thread_pool_handler(handler, max_concurrency=8) as wrapped:
        async with http_server(config, wrapped):
            ...
"""

from __future__ import annotations

import sentry_sdk
from flask import Flask
from flask import request as flask_request

from localpost.http.flask import _write_response
from localpost.http.server import BodyHandler, HTTPReqCtx, RequestHandler
from localpost.http.wsgi import _build_environ

__all__ = ["sentry_flask_handler"]


def sentry_flask_handler(app: Flask, *, op: str = "http.server") -> RequestHandler:
    """Wrap a Flask app with a Sentry transaction covering the full request, streaming included.

    Always returns a :data:`BodyHandler` continuation — Flask reads
    ``request`` body internally, so the selector must buffer it before
    the Flask pipeline runs.
    """

    def run_flask_with_sentry(ctx: HTTPReqCtx) -> None:
        environ = _build_environ(ctx)
        method = environ["REQUEST_METHOD"]
        path = environ["PATH_INFO"]

        with (
            sentry_sdk.isolation_scope(),
            sentry_sdk.start_transaction(
                op=op,
                name=f"{method} {path}",
                source="url",
            ) as tx,
        ):
            tx.set_tag("http.method", method)
            tx.set_data("http.url", path)

            with app.request_context(environ):
                # Once routing has matched, rename the transaction to the route rule
                # for low cardinality.
                rule = flask_request.url_rule
                if rule is not None:
                    sentry_sdk.get_current_scope().set_transaction_name(
                        f"{method} {rule.rule}",
                        source="route",
                    )

                try:
                    response = app.full_dispatch_request()
                except Exception as exc:  # noqa: BLE001
                    response = app.handle_exception(exc)
                tx.set_http_status(response.status_code)
                try:
                    _write_response(ctx, response)
                finally:
                    response.close()

    def pre_body(_ctx: HTTPReqCtx) -> BodyHandler:
        return run_flask_with_sentry

    return pre_body
