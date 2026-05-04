"""Server-Sent Events (SSE) with response compression.

Run::

    uv run examples/http/sse_compressed.py

Open another terminal::

    curl -i --no-buffer http://localhost:8000/events                            # uncompressed
    curl -i --no-buffer -H 'Accept-Encoding: gzip' --compressed http://localhost:8000/events
    curl -i --no-buffer -H 'Accept-Encoding: br'   --compressed http://localhost:8000/events

The handler emits one event per second. With ``Accept-Encoding`` set,
``compress_handler`` switches to streaming compression and sync-flushes
each event so the client sees them as they're produced (no buffering).
``Transfer-Encoding: chunked`` is auto-framed by the HTTP backend on
HTTP/1.1.

In a browser::

    const es = new EventSource('http://localhost:8000/events');
    es.onmessage = (e) => console.log(e.data);

The browser decompresses ``Content-Encoding: gzip`` / ``br`` transparently
before the ``EventSource`` parser sees the stream.
"""

from __future__ import annotations

import logging
import sys
import time

from localpost.hosting import run_app, service
from localpost.http import (
    HTTPReqCtx,
    Response,
    ServerConfig,
    check_cancelled,
    compress_handler,
    http_server,
    streaming_pool_handler,
)


def _events(ctx: HTTPReqCtx) -> None:
    ctx.start_response(
        Response(
            status_code=200,
            headers=[
                (b"content-type", b"text/event-stream"),
                (b"cache-control", b"no-cache"),
            ],
        )
    )
    n = 0
    while True:
        check_cancelled()
        msg = f"data: tick {n} at {time.time():.3f}\n\n".encode()
        ctx.send(msg)
        n += 1
        time.sleep(1)


@service
async def app():
    # Streaming SSE handlers run on a streaming pool — body is not
    # pre-buffered, the worker holds the borrowed conn for the duration.
    async with streaming_pool_handler(_events, max_concurrency=8) as inner:
        # Same compress_handler call as for JSON APIs; the middleware
        # picks the streaming path automatically when the response has no
        # Content-Length and the content type is in the allowlist (which
        # text/event-stream is, by default).
        wrapped = compress_handler(inner, algorithms=("br", "gzip"))

        config = ServerConfig(host="127.0.0.1", port=8000)
        async with http_server(config, wrapped):
            yield


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
