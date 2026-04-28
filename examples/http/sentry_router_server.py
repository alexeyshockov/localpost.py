"""Router app with Sentry tracing.

Run::

    SENTRY_DSN='https://...@.../...' uv run --group dev-sentry examples/http/sentry_router_server.py

    curl http://localhost:8000/books/42        # transaction "GET /books/{id}" (route source)
    curl http://localhost:8000/nope            # transaction "GET /nope"       (url source)

If ``SENTRY_DSN`` is unset, Sentry is initialised in disabled mode — the app
still works, transactions are just no-ops. Useful to demo the wiring without
sending data.
"""

from __future__ import annotations

import logging
import os
import sys
import time

import sentry_sdk

from localpost.hosting import run_app, service
from localpost.http import (
    RequestCtx,
    Response,
    Routes,
    ServerConfig,
    http_server,
    thread_pool_handler,
)
from localpost.http.router_sentry import sentry_router_handler


def _root(_: RequestCtx) -> Response:
    return Response(200, {"content-type": "text/plain"}, [b"hello\n"])


def _get_book(ctx: RequestCtx) -> Response:
    book_id = ctx.path_args["id"]
    # Spans inside the handler land on the request transaction.
    with sentry_sdk.start_span(op="db.query", name="select book"):
        time.sleep(0.01)
    return Response(200, {"content-type": "text/plain"}, [f"book={book_id}\n".encode()])


def build_router():
    routes = Routes()
    routes.get("/")(_root)
    routes.get("/books/{id}")(_get_book)
    return routes.build()


@service
async def app():
    handler = sentry_router_handler(build_router())
    config = ServerConfig(host="127.0.0.1", port=8000)
    async with thread_pool_handler(handler, max_concurrency=8) as wrapped:
        async with http_server(config, wrapped):
            yield


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    sentry_sdk.init(
        dsn=os.environ.get("SENTRY_DSN"),  # None → Sentry runs in disabled mode
        traces_sample_rate=1.0,
    )
    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
