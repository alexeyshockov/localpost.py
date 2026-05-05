"""Static-file server with a separate worker pool from the API.

Run::

    uv run examples/http/static_files.py

    curl http://localhost:8000/api/hello
    curl http://localhost:8000/static/<some-file-under-cwd>
    curl -I http://localhost:8000/static/<some-file>      # HEAD: just headers
    curl -H 'Range: bytes=0-99' http://localhost:8000/static/<some-file>

The static handler uses ``socket.sendfile()`` for the body (zero-copy)
and lives in its own thread pool sized for I/O-bound concurrency, so a
slow client downloading a large file can't pin the small API pool.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from localpost.hosting import run_app, service
from localpost.http import (
    BodyHandler,
    HTTPReqCtx,
    RequestHandler,
    Response,
    Routes,
    ServerConfig,
    http_server,
    static_handler,
    thread_pool_handler,
)


def _hello(ctx: HTTPReqCtx) -> BodyHandler | None:
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
    return None


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

    async with (
        thread_pool_handler(build_api()) as api_h,
        thread_pool_handler(static) as static_h,
    ):
        def root_handler(ctx: HTTPReqCtx) -> BodyHandler | None:
            return (static_h if ctx.request.path.startswith(b"/static/") else api_h)(ctx)

        async with http_server(config, root_handler):
            yield


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
