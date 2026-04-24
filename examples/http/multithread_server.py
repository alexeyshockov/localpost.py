"""Multi-threaded HTTP app.

Run::

    uv run examples/http/multithread_server.py

    curl http://localhost:8000/
    curl http://localhost:8000/hello/world
    curl http://localhost:8000/slow    # sleeps; spam a few of these in parallel

Handlers run in AnyIO worker threads (``to_thread.run_sync``), bounded by
``max_concurrency``. Each request becomes its own async task in the service's task
group, so SIGINT / SIGTERM cancels in-flight work cleanly.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

from localpost.hosting import run_app
from localpost.http import (
    RequestCtx,
    Response,
    Router,
    ServerConfig,
    http_server,
)


def _root(_: RequestCtx) -> Response:
    return Response(200, {"content-type": "text/plain"}, [b"hello from localpost\n"])


def _hello(ctx: RequestCtx) -> Response:
    name = ctx.path_args["name"]
    body = f"Hello, {name}! (thread={threading.current_thread().name})\n".encode()
    return Response(200, {"content-type": "text/plain"}, [body])


def _slow(_: RequestCtx) -> Response:
    time.sleep(1.0)  # exercises concurrency: several of these run in parallel
    body = f"done on thread={threading.current_thread().name}\n".encode()
    return Response(200, {"content-type": "text/plain"}, [body])


def build_router() -> Router:
    router = Router()
    router.get("/")(_root)
    router.get("/hello/{name}")(_hello)
    router.get("/slow")(_slow)
    return router


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    router = build_router()
    config = ServerConfig(host="127.0.0.1", port=8000)
    return run_app(http_server(config, router.as_handler(), max_concurrency=16))


if __name__ == "__main__":
    sys.exit(main())
