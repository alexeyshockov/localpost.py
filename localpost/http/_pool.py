"""Thread-pool wrapper for HTTP request handlers.

Turns any ``RequestHandler`` into one that borrows the connection on the
selector thread, queues it, and runs the original handler on a worker. The
selector stays free to keep accepting / parsing / dispatching the next
request.

Compose explicitly with :func:`localpost.http.http_server` — handlers that
can answer synchronously (e.g. a :class:`Router`'s 404/405 path) stay on
the selector thread; only the handlers you wrap pay the worker hop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress

from anyio import (
    BrokenResourceError,
    CapacityLimiter,
    ClosedResourceError,
    EndOfStream,
    create_task_group,
    to_thread,
)

from localpost import threadtools
from localpost.http._cancel import RequestCancel, RequestCancelled, _enter_request
from localpost.http.config import LOGGER_NAME
from localpost.http.server import HTTPReqCtx, RequestHandler, emit_handler_error

__all__ = ["thread_pool_handler"]


def thread_pool_handler(
    inner: RequestHandler,
    /,
    *,
    max_concurrency: int,
) -> AbstractAsyncContextManager[RequestHandler]:
    """Async context manager: yields a ``RequestHandler`` that runs ``inner`` on a worker thread.

    On call, the wrapped handler borrows the connection from the selector
    (``stop_tracking``) and queues ``(ctx, cancel)`` on a bounded channel
    sized to ``max_concurrency``. ``max_concurrency`` worker threads pull
    from the channel, run ``inner(ctx)`` under a per-request cancel scope,
    and release the connection back to the selector via ``finish_response``
    (or close it on error / cancellation).

    Per-request cancellation surfaces through
    :func:`localpost.http.check_cancelled`. Two triggers feed it: client
    disconnect (via non-blocking ``MSG_PEEK`` on the socket) and pool
    shutdown (a single :class:`threading.Event` ORed into every in-flight
    token).

    Lifecycle:
      * Enter — spawn ``max_concurrency`` workers in a private task group.
      * Exit — set the shutdown event so in-flight handlers see cancellation
        on their next ``check_cancelled`` call, close the channel, and wait
        for workers to drain.

    Example::

        async with thread_pool_handler(router.as_handler(), max_concurrency=8) as h:
            async with http_server(config, h):
                await current_service().shutting_down.wait()
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be >= 1")

    return _thread_pool_handler(inner, max_concurrency)


@asynccontextmanager
async def _thread_pool_handler(
    inner: RequestHandler,
    max_concurrency: int,
) -> AsyncGenerator[RequestHandler]:
    logger = logging.getLogger(LOGGER_NAME)

    tx, rx = threadtools.Channel[tuple[HTTPReqCtx, RequestCancel]].create(capacity=max_concurrency)
    shutdown_event = threading.Event()
    # Dedicated limiter so workers don't consume slots from AnyIO's global
    # default thread pool. Each worker holds a slot for its full lifetime,
    # so we need exactly ``max_concurrency`` slots.
    workers_limiter = CapacityLimiter(max_concurrency)

    def submit(ctx: HTTPReqCtx) -> None:
        """Selector-thread callback: borrow the conn and queue it for a worker."""
        ctx._server.stop_tracking(ctx._conn)
        cancel = RequestCancel(_sock=ctx._conn.sock, _shutdown_event=shutdown_event)
        try:
            tx.put((ctx, cancel))
        except (ClosedResourceError, BrokenResourceError):
            with suppress(Exception):
                ctx._conn.close()

    def worker(my_rx: threadtools.ReceiveChannel[tuple[HTTPReqCtx, RequestCancel]]) -> None:
        with my_rx:
            while True:
                try:
                    ctx, cancel = my_rx.get()
                except (EndOfStream, ClosedResourceError):
                    return

                try:
                    with _enter_request(cancel):
                        try:
                            inner(ctx)
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
                    # On success ``finish_response`` already re-tracked the conn via
                    # ``_maybe_give_back`` — we MUST NOT touch it here. We can't
                    # read ``ctx._conn.tracked`` either: that field is shared with
                    # the next request's dispatcher, which clears it via
                    # ``stop_tracking`` before this finally runs.
                    #
                    # The case left to handle is per-request cancellation: the
                    # handler caught the signal and returned, but the conn is in
                    # an uncertain state. ``cancel.fired`` is the cheap (no-syscall)
                    # check — ``is_cancelled`` would actively re-probe the socket
                    # which is wasteful here. ``emit_handler_error`` already
                    # closes on the exception path.
                    if cancel.fired:
                        with suppress(Exception):
                            ctx._conn.close()

    async def run_worker(my_rx: threadtools.ReceiveChannel[tuple[HTTPReqCtx, RequestCancel]]) -> None:
        await to_thread.run_sync(worker, my_rx, limiter=workers_limiter)

    async with create_task_group() as tg:
        for _ in range(max_concurrency):
            tg.start_soon(run_worker, rx.clone())
        rx.close()
        try:
            yield submit
        finally:
            # Signal in-flight handlers (next ``check_cancelled`` raises) and
            # let workers drain via ``EndOfStream`` from the closed channel.
            # The task group's exit waits for all workers to return.
            shutdown_event.set()
            tx.close()
