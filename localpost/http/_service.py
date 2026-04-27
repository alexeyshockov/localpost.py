from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable
from contextlib import suppress
from wsgiref.types import WSGIApplication

import h11
from anyio import (
    BrokenResourceError,
    CapacityLimiter,
    ClosedResourceError,
    EndOfStream,
    from_thread,
    to_thread,
)

from localpost import hosting, threadtools
from localpost.hosting import ServiceLifetime
from localpost.http._cancel import RequestCancel, RequestCancelled, _enter_request
from localpost.http.config import LOGGER_NAME, ServerConfig
from localpost.http.server import ConnMode, HTTPReqCtx, RequestHandler, emit_handler_error, start_http_server
from localpost.http.wsgi import wrap_wsgi

__all__ = ["http_server", "wsgi_server"]


def _request_has_body(request: h11.Request) -> bool:
    """``True`` iff the request advertises a non-empty body (``Content-Length > 0`` or chunked)."""
    for name, value in request.headers:
        n = bytes(name).lower()
        if n == b"content-length":
            try:
                return int(value) > 0
            except ValueError:
                return True
        if n == b"transfer-encoding":
            return True
    return False




@hosting.service
def http_server(
    config: ServerConfig,
    handler: RequestHandler,
    /,
    *,
    max_concurrency: int = 1,
):
    """Run an HTTP server inside a hosted service.

    The selector thread accepts and parses requests, then hands each one to a
    worker thread via a bounded :class:`localpost.threadtools.Channel`. Workers
    run sync handlers directly — no event-loop hop, no per-request portal call.
    Once the channel buffer is full, the selector blocks on ``put`` and applies
    back-pressure on accept.

    Per-request cancellation is HTTP-native and lives behind
    :func:`localpost.http.check_cancelled`. Triggers:
      * Client disconnected mid-request (selector watchdog detected EOF).
      * Service is shutting down.

    Worker / service cancellation (a separate concern) flows through the
    AnyIO machinery as elsewhere in :mod:`localpost`.
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be >= 1")

    logger = logging.getLogger(LOGGER_NAME)

    def run(lt: ServiceLifetime) -> Awaitable[None]:
        tx, rx = threadtools.Channel[tuple[HTTPReqCtx, RequestCancel]].create(capacity=max_concurrency)

        # Tokens for in-flight requests, so service shutdown can flip them all at once.
        in_flight: set[RequestCancel] = set()
        in_flight_lock = threading.Lock()
        # Dedicated thread limiter so workers + selector don't consume slots from
        # AnyIO's default global limiter.
        threads_limiter = CapacityLimiter(max_concurrency + 1)

        def dispatch(ctx: HTTPReqCtx) -> None:
            """Selector-thread callback: parse done, hand off to a worker."""
            cancel = RequestCancel()
            if _request_has_body(ctx.request):
                # The worker will do blocking I/O to read the body. We can't
                # arm the watchdog here — buffered body bytes would make the
                # selector spin on a level-triggered ``EVENT_READ``.
                ctx._server.stop_tracking(ctx._conn)
            else:
                # No body to read — arm the watchdog. EOF on the socket = client
                # walked away while the handler was running. ``MSG_PEEK`` is safe
                # alongside the worker's ``send`` (different ops at the kernel level).
                ctx._server.to_watchdog(ctx._conn, cancel.cancel)
            try:
                tx.put((ctx, cancel))
            except (ClosedResourceError, BrokenResourceError):
                with suppress(Exception):
                    ctx._conn.close()

        def worker(my_rx: threadtools.ReceiveChannel[tuple[HTTPReqCtx, RequestCancel]]) -> None:
            """Worker-thread loop. Pulls from ``my_rx`` until the channel ends."""
            with my_rx:
                while True:
                    try:
                        ctx, cancel = my_rx.get()
                    except (EndOfStream, ClosedResourceError):
                        return

                    with in_flight_lock:
                        in_flight.add(cancel)
                    try:
                        with _enter_request(cancel):
                            try:
                                handler(ctx)
                            except RequestCancelled:
                                # Handler bailed cleanly. Connection state is uncertain — close.
                                with suppress(Exception):
                                    ctx._conn.close()
                            except Exception:
                                logger.exception(
                                    "Handler raised for %s %r", ctx.request.method, ctx.request.target
                                )
                                emit_handler_error(ctx)
                    finally:
                        with in_flight_lock:
                            in_flight.discard(cancel)
                        # Conn-release policy:
                        #
                        # On the success path, ``finish_response`` already drained h11 events and
                        # re-tracked the conn (mode → NORMAL). We MUST NOT call ``track`` here
                        # because ``track`` flips the socket back to non-blocking via
                        # ``settimeout(0)`` — which would race with the next request's worker
                        # already using the shared socket in blocking mode (response-body
                        # writes), causing spurious ``BlockingIOError`` and dropped requests.
                        #
                        # The only thing left to do is close the conn for the unhappy paths:
                        # client disconnected, watchdog fired, or the handler returned without
                        # completing the response (mode != NORMAL).
                        if (
                            cancel.is_cancelled
                            or ctx._conn.recv_closed
                            or ctx._conn.mode is not ConnMode.NORMAL
                        ):
                            with suppress(Exception):
                                ctx._conn.close()

        async def shutdown_watcher() -> None:
            await lt.shutting_down.wait()
            with in_flight_lock:
                tokens = list(in_flight)
            for token in tokens:
                token.cancel()
            tx.close()

        def run_server() -> None:
            with start_http_server(config, dispatch) as server:
                lt.set_started()
                while not lt.shutting_down.is_set():
                    from_thread.check_cancelled()
                    server.run()

        async def run_worker(my_rx: threadtools.ReceiveChannel[tuple[HTTPReqCtx, RequestCancel]]) -> None:
            await to_thread.run_sync(worker, my_rx, limiter=threads_limiter)

        async def main() -> None:
            try:
                for _ in range(max_concurrency):
                    lt.tg.start_soon(run_worker, rx.clone())
                rx.close()
                lt.tg.start_soon(shutdown_watcher)
                await to_thread.run_sync(run_server, limiter=threads_limiter)
            finally:
                # Selector exited (clean or otherwise) — drain workers.
                tx.close()

        return main()

    return run


def wsgi_server(
    config: ServerConfig,
    app: WSGIApplication,
    /,
    *,
    max_concurrency: int = 1,
):
    """Same as :func:`http_server`, but serves a WSGI application."""
    return http_server(config, wrap_wsgi(app), max_concurrency=max_concurrency)
