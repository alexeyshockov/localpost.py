"""JSON API with response compression.

Run::

    uv run examples/http/compressed_api.py

    curl -H 'Accept-Encoding: gzip' --compressed http://localhost:8000/items
    curl -H 'Accept-Encoding: br'   --compressed http://localhost:8000/items
    curl -i http://localhost:8000/items                                           # uncompressed (no Accept-Encoding)

Brotli requires::

    pip install localpost[http-compress]

Compression only intercepts ``ctx.complete(...)`` responses; streaming
paths and ``sendfile`` pass through unchanged. Behind a CDN you usually
don't need this — the CDN compresses at the edge from an uncompressed
origin.
"""

from __future__ import annotations

import json
import logging
import sys

from localpost.hosting import run_app, service
from localpost.http import (
    BodyHandler,
    HTTPReqCtx,
    Response,
    Routes,
    ServerConfig,
    compress_handler,
    http_server,
    thread_pool_handler,
)


def _items(ctx: HTTPReqCtx) -> BodyHandler | None:
    # Big-ish JSON so the response crosses the default ``min_size=1024``.
    payload = {"items": [{"id": i, "name": f"item-{i}", "tag": "x" * 32} for i in range(64)]}
    body = json.dumps(payload).encode("utf-8")
    ctx.complete(
        Response(
            status_code=200,
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        ),
        body if ctx.request.method != b"HEAD" else None,
    )
    return None


def build_router():
    routes = Routes()
    routes.get("/items")(_items)
    return routes.build()


@service
async def app():
    # Brotli first if available, else gzip.
    try:
        h = compress_handler(build_router().as_handler(), algorithms=("br", "gzip"))
    except ImportError:
        h = compress_handler(build_router().as_handler(), algorithms=("gzip",))

    config = ServerConfig(host="127.0.0.1", port=8000)
    async with thread_pool_handler(h) as wrapped:
        async with http_server(config, wrapped):
            yield


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
