"""Per-IP sliding-window rate-limiter middleware example.

Demonstrates a :data:`localpost.http.Middleware` that counts requests per
client IP within a rolling time window and returns 429 when the budget is
exceeded. The check runs on the selector thread pre-body — no worker hop
for rejected requests.

Run::

    uv run examples/http/middleware_rate_limit.py

    # Spam quickly to trigger the limiter:
    for i in $(seq 1 20); do curl -s -o /dev/null -w "%{http_code}\\n" http://localhost:8000/; done
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import defaultdict, deque

from localpost.hosting import run_app, service
from localpost.http import (
    HTTPReqCtx,
    Middleware,
    RequestHandler,
    Response,
    Routes,
    ServerConfig,
    compose,
    http_server,
    thread_pool_handler,
)
from localpost.threadtools import WorkerExecutor

# ----------- rate-limit state (selector-thread-safe via lock) -------------

_TOO_MANY = Response(
    status_code=429,
    headers=[(b"content-length", b"0"), (b"retry-after", b"1")],
)


class _SlidingWindowCounter:
    """Thread-safe per-key sliding-window request counter."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._timestamps: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._timestamps[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            return True


# ----------- middleware ---------------------------------------------------


def rate_limit(max_requests: int = 10, window_seconds: float = 1.0) -> Middleware:
    """Limit each remote IP to ``max_requests`` within ``window_seconds``."""
    counter = _SlidingWindowCounter(max_requests, window_seconds)

    def middleware(inner: RequestHandler) -> RequestHandler:
        def wrapped(ctx: HTTPReqCtx) -> None:
            client_ip = (ctx.remote_addr or "").rpartition(":")[0] or "unknown"
            if not counter.is_allowed(client_ip):
                ctx.complete(_TOO_MANY, b"")
                return
            inner(ctx)

        return wrapped

    return middleware


# ----------- routes -------------------------------------------------------


def _root(ctx: HTTPReqCtx) -> None:
    body = b"ok\n"
    ctx.complete(
        Response(
            status_code=200,
            headers=[(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())],
        ),
        body,
    )


def build_handler() -> RequestHandler:
    routes = Routes()
    routes.get("/")(_root)
    return compose(rate_limit(max_requests=10, window_seconds=1.0))(routes.build().as_handler())


# ----------- app ----------------------------------------------------------


@service
async def app():
    config = ServerConfig(host="127.0.0.1", port=8000)
    with WorkerExecutor() as ex:
        async with thread_pool_handler(build_handler(), ex) as h:
            async with http_server(config, h):
                yield


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return run_app(app())


if __name__ == "__main__":
    sys.exit(main())
