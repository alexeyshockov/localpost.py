"""Multi-threaded HTTP app.

Run::

    uv run examples/http/multithread_server.py

    curl http://localhost:8000/
    curl http://localhost:8000/hello/world
    curl http://localhost:8000/slow    # sleeps; spam a few of these in parallel

The whole router is wrapped with :func:`localpost.http.thread_pool_handler` so
every matched route runs on a worker thread (bounded by ``max_concurrency``).
SIGINT / SIGTERM signals shutdown; in-flight handlers see ``RequestCancelled``
on the next ``check_cancelled`` call and the pool drains before the process
exits.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

from localpost.hosting import run_app, service
from localpost.http import (
    BodyHandler,
    HTTPReqCtx,
    NativeResponse,
    Router,
    Routes,
    ServerConfig,
    http_server,
    route_match,
    thread_pool_handler,
)


def _emit(ctx: HTTPReqCtx, body: bytes) -> None:
    ctx.complete(
        NativeResponse(
            status_code=200,
            headers=[(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode("ascii"))],
        ),
        body,
    )


def _root(ctx: HTTPReqCtx) -> BodyHandler | None:
    _emit(ctx, b"hello from localpost\n")
    return None


def _hello(ctx: HTTPReqCtx) -> BodyHandler | None:
    name = route_match(ctx).path_args["name"]
    _emit(ctx, f"Hello, {name}! (thread={threading.current_thread().name})\n".encode())
    return None


def _slow(ctx: HTTPReqCtx) -> BodyHandler | None:
    time.sleep(1.0)  # exercises concurrency: several of these run in parallel
    _emit(ctx, f"done on thread={threading.current_thread().name}\n".encode())
    return None


def build_router() -> Router:
    routes = Routes()
    routes.get("/")(_root)
    routes.get("/hello/{name}")(_hello)
    routes.get("/slow")(_slow)
    return routes.build()


@service
async def app():
    config = ServerConfig(host="127.0.0.1", port=8000)
    async with thread_pool_handler(build_router().as_handler(), max_concurrency=16) as wrapped:
        async with http_server(config, wrapped):
            yield


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
