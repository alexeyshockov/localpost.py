"""Static-file server alongside an API on the same worker pool.

Run::

    uv run examples/http/static_files.py

    curl http://localhost:8000/api/hello
    curl http://localhost:8000/static/<some-file-under-cwd>
    curl -I http://localhost:8000/static/<some-file>      # HEAD: just headers
    curl -H 'Range: bytes=0-99' http://localhost:8000/static/<some-file>

The static handler uses ``socket.sendfile()`` for the body (zero-copy).
Both API and static handlers go through the same ``thread_pool_handler``
— workers come from the process-wide pool and grow on demand.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from localpost.hosting import run_app, service
from localpost.http import (
    HTTPReqCtx,
    RequestHandler,
    Response,
    Routes,
    ServerConfig,
    http_server,
    static_handler,
    thread_pool_handler,
)
from localpost.threadtools import WorkerExecutor


def _hello(ctx: HTTPReqCtx) -> None:
    body = b"hello from the API\n"
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


def build_api() -> RequestHandler:
    routes = Routes()
    routes.get("/api/hello")(_hello)
    return routes.build().as_handler()


@service
async def app():
    root = Path(os.environ.get("STATIC_ROOT", "."))
    config = ServerConfig(host="127.0.0.1", port=8000)

    static = static_handler(
        root,
        prefix=b"/static/",
        cache_control="public, max-age=3600",
    )
    api = build_api()

    def dispatch(ctx: HTTPReqCtx) -> None:
        (static if ctx.request.path.startswith(b"/static/") else api)(ctx)

    with WorkerExecutor() as ex:
        async with thread_pool_handler(dispatch, ex) as wrapped:
            async with http_server(config, wrapped):
                yield


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
