"""Thread-pool wrapper for HTTP request handlers.

Wraps a :data:`RequestHandler` so that, when it returns a
:data:`BodyHandler` continuation, the continuation is dispatched on a
worker thread instead of running on the selector. The pre-body phase
(routing, auth, 404/405) stays on the selector, where it's free; only
post-body work that the user wants offloaded pays the worker hop.

Compose explicitly with :func:`localpost.http.http_server` — handlers
that complete inline (e.g. a :class:`Router`'s 404/405 path or a
body-free GET) never enter the pool.
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
from localpost.http.server import BodyHandler, HTTPReqCtx, RequestHandler, emit_handler_error

__all__ = ["thread_pool_handler"]


def thread_pool_handler(
    inner: RequestHandler,
    /,
    *,
    max_concurrency: int,
) -> AbstractAsyncContextManager[RequestHandler]:
    """Async context manager: yields a ``RequestHandler`` that offloads
    body-handler continuations to a worker thread.

    The wrapper invokes ``inner(ctx)`` on the selector thread (pre-body
    phase). If ``inner`` returns:

    - ``None`` — pass-through. ``inner`` already completed inline (e.g.
      a 404 or a body-free GET) or borrowed the conn itself. No worker
      hop.
    - :data:`BodyHandler` — the wrapper returns a *new* continuation
      that, when invoked by the selector after the body has been
      buffered, ``stop_tracking`` s the conn and queues
      ``(ctx, cancel, inner_continuation)`` on a bounded channel sized
      to ``max_concurrency``. ``max_concurrency`` workers pull from the
      channel, run ``inner_continuation(ctx)`` under a per-request
      cancel scope, and release the connection back to the selector via
      ``finish_response`` (or close it on error / cancellation).

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


_WorkItem = tuple[HTTPReqCtx, RequestCancel, BodyHandler]


@asynccontextmanager
async def _thread_pool_handler(
    inner: RequestHandler,
    max_concurrency: int,
) -> AsyncGenerator[RequestHandler]:
    logger = logging.getLogger(LOGGER_NAME)

    tx, rx = threadtools.Channel[_WorkItem].create(capacity=max_concurrency)
    shutdown_event = threading.Event()
    # Dedicated limiter so workers don't consume slots from AnyIO's global
    # default thread pool. Each worker holds a slot for its full lifetime,
    # so we need exactly ``max_concurrency`` slots.
    workers_limiter = CapacityLimiter(max_concurrency)

    def make_dispatch(body_handler: BodyHandler) -> BodyHandler:
        """Return a continuation that, given a ctx with body buffered,
        borrows the conn and queues the work for a worker."""

        def dispatched(ctx: HTTPReqCtx) -> None:
            ctx._server.stop_tracking(ctx._conn)
            cancel = RequestCancel(_sock=ctx._conn.sock, _shutdown_event=shutdown_event)
            try:
                tx.put((ctx, cancel, body_handler))
            except (ClosedResourceError, BrokenResourceError):
                with suppress(Exception):
                    ctx._conn.close()

        return dispatched

    def wrapped(ctx: HTTPReqCtx) -> BodyHandler | None:
        """The pre-body :data:`RequestHandler` exposed to the server."""
        result = inner(ctx)
        if result is None:
            return None  # inner completed inline or borrowed itself
        return make_dispatch(result)

    def worker(my_rx: threadtools.ReceiveChannel[_WorkItem]) -> None:
        with my_rx:
            while True:
                try:
                    ctx, cancel, body_handler = my_rx.get()
                except (EndOfStream, ClosedResourceError):
                    return

                try:
                    with _enter_request(cancel):
                        try:
                            body_handler(ctx)
                        except RequestCancelled:
                            # Handler bailed cleanly. Connection state is uncertain — close.
                            with suppress(Exception):
                                ctx._conn.close()
                        except Exception:
                            logger.exception(
                                "Body handler raised for %s %r", ctx.request.method, ctx.request.target
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

    async def run_worker(my_rx: threadtools.ReceiveChannel[_WorkItem]) -> None:
        await to_thread.run_sync(worker, my_rx, limiter=workers_limiter)

    async with create_task_group() as tg:
        for _ in range(max_concurrency):
            tg.start_soon(run_worker, rx.clone())
        rx.close()
        try:
            yield wrapped
        finally:
            # Signal in-flight handlers (next ``check_cancelled`` raises) and
            # let workers drain via ``EndOfStream`` from the closed channel.
            # The task group's exit waits for all workers to return.
            shutdown_event.set()
            tx.close()
