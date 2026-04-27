from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable
from contextlib import suppress
from wsgiref.types import WSGIApplication

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
from localpost.http.server import HTTPReqCtx, RequestHandler, emit_handler_error, start_http_server
from localpost.http.wsgi import wrap_wsgi

__all__ = ["http_server", "wsgi_server"]


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
      * Client disconnected mid-request (pull-based ``MSG_PEEK`` on the
        request socket from inside ``check_cancelled``).
      * Service is shutting down (a single shared ``threading.Event`` is OR-ed
        into every in-flight token's ``is_cancelled`` check).

    Worker / service cancellation (a separate concern) flows through the
    AnyIO machinery as elsewhere in :mod:`localpost`.
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be >= 1")

    logger = logging.getLogger(LOGGER_NAME)

    def run(lt: ServiceLifetime) -> Awaitable[None]:
        tx, rx = threadtools.Channel[tuple[HTTPReqCtx, RequestCancel]].create(capacity=max_concurrency)

        # Single shutdown signal shared by every in-flight cancel token. No
        # per-request registry needed — workers see shutdown via their token's
        # ``is_cancelled`` check.
        shutdown_event = threading.Event()
        # Dedicated thread limiter so workers + selector don't consume slots from
        # AnyIO's default global limiter.
        threads_limiter = CapacityLimiter(max_concurrency + 1)

        def dispatch(ctx: HTTPReqCtx) -> None:
            """Selector-thread callback: parse done, hand off to a worker.

            Always uses ``stop_tracking`` (the original borrow flow): the conn
            leaves the selector entirely while the worker owns it. Client
            disconnect detection while the worker is running happens inside
            :func:`localpost.http.check_cancelled` via a non-blocking
            ``MSG_PEEK``.
            """
            ctx._server.stop_tracking(ctx._conn)
            cancel = RequestCancel(_sock=ctx._conn.sock, _shutdown_event=shutdown_event)
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
                        # Conn-release policy:
                        #
                        # On the success path, ``finish_response`` already re-tracked the conn
                        # via ``_maybe_give_back`` — we MUST NOT touch it here. Specifically,
                        # we cannot read ``ctx._conn.tracked``: that field is shared with the
                        # next request's dispatcher, which clears it via ``stop_tracking``
                        # before this finally runs.
                        #
                        # The only case left to handle is per-request cancellation: the
                        # handler caught the signal and returned, but the conn is in an
                        # uncertain state. ``cancel.fired`` is the cheap (no-syscall)
                        # check — ``is_cancelled`` would actively re-probe the socket
                        # which is wasteful here.
                        # ``emit_handler_error`` already closes on the exception path.
                        if cancel.fired:
                            with suppress(Exception):
                                ctx._conn.close()

        async def shutdown_watcher() -> None:
            await lt.shutting_down.wait()
            shutdown_event.set()
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
