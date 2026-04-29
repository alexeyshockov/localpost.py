"""Worker-pool wrappers for HTTP request handlers.

Two public wrappers, both async context managers built on a shared
internal pool primitive:

- :func:`thread_pool_handler` — wraps a :data:`RequestHandler` so the
  :data:`BodyHandler` continuation it returns runs on a worker thread.
  The pre-body phase (routing, auth, 404/405) stays on the selector.
  This is the right wrapper for the JSON-API common case: body is
  buffered into ``ctx.body`` by the selector, the user handler does
  the JSON parse + work on the worker.

- :func:`streaming_pool_handler` — wraps a "raw" handler that runs on
  a worker thread on a *borrowed* connection. The body is **not**
  buffered up-front; the handler reads it via ``ctx.receive(...)``
  chunk by chunk. Use for streaming uploads / large bodies where
  buffering is undesirable.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator, Callable
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

__all__ = ["thread_pool_handler", "streaming_pool_handler"]


# --- Internal pool primitive --------------------------------------------


_WorkFn = Callable[[HTTPReqCtx], None]
_WorkItem = tuple[HTTPReqCtx, RequestCancel, _WorkFn]


class _Pool:
    """Shared channel + workers, plus dispatch helpers.

    Created/torn-down by :func:`_pool_context`. Two ``dispatch_*`` methods
    let callers wire either a post-body :data:`BodyHandler` or a
    pre-body streaming handler onto the same worker pool.
    """

    __slots__ = ("_shutdown_event", "_tx")

    def __init__(
        self,
        tx: threadtools.SendChannel[_WorkItem],
        shutdown_event: threading.Event,
    ) -> None:
        self._tx = tx
        self._shutdown_event = shutdown_event

    def dispatch_buffered(self, body_handler: BodyHandler) -> BodyHandler:
        """Wrap a :data:`BodyHandler` so when it's invoked post-body it
        borrows the conn and queues for a worker. The worker runs
        ``body_handler(ctx)`` with ``ctx.body`` already populated."""
        return self._make_dispatcher(body_handler)

    def dispatch_streaming(self, handler: _WorkFn) -> RequestHandler:
        """Wrap a streaming handler so it runs in a worker on a borrowed
        conn (body **not** pre-buffered).

        The returned :data:`RequestHandler` borrows the conn pre-body
        and queues ``handler`` for a worker. Worker runs ``handler(ctx)``
        on a blocking-with-timeout socket; the handler reads the body
        via ``ctx.receive(...)`` and completes the response."""
        dispatcher = self._make_dispatcher(handler)

        def pre_body(ctx: HTTPReqCtx) -> BodyHandler | None:
            dispatcher(ctx)
            return None  # we borrowed; selector free

        return pre_body

    def _make_dispatcher(self, fn: _WorkFn) -> _WorkFn:
        tx = self._tx
        shutdown_event = self._shutdown_event

        def dispatched(ctx: HTTPReqCtx) -> None:
            ctx._server.stop_tracking(ctx._conn)
            cancel = RequestCancel(_sock=ctx._conn.sock, _shutdown_event=shutdown_event)
            try:
                tx.put((ctx, cancel, fn))
            except (ClosedResourceError, BrokenResourceError):
                with suppress(Exception):
                    ctx._conn.close()

        return dispatched


@asynccontextmanager
async def _pool_context(max_concurrency: int) -> AsyncGenerator[_Pool]:
    """Open a worker pool for ``max_concurrency`` concurrent requests.

    Yields a :class:`_Pool` whose ``dispatch_*`` helpers can be used to
    wire handlers onto the shared channel. On exit, signals in-flight
    handlers via the cancel event and waits for workers to drain.
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be >= 1")

    logger = logging.getLogger(LOGGER_NAME)
    tx, rx = threadtools.Channel[_WorkItem].create(capacity=max_concurrency)
    shutdown_event = threading.Event()
    # Dedicated limiter so workers don't consume slots from AnyIO's global
    # default thread pool. Each worker holds a slot for its full lifetime.
    workers_limiter = CapacityLimiter(max_concurrency)

    def worker(my_rx: threadtools.ReceiveChannel[_WorkItem]) -> None:
        with my_rx:
            while True:
                try:
                    ctx, cancel, fn = my_rx.get()
                except (EndOfStream, ClosedResourceError):
                    return
                try:
                    with _enter_request(cancel):
                        try:
                            fn(ctx)
                        except RequestCancelled:
                            # Handler bailed cleanly. Connection state is uncertain — close.
                            with suppress(Exception):
                                ctx._conn.close()
                        except Exception:
                            logger.exception(
                                "Pool handler raised for %s %r",
                                ctx.request.method,
                                ctx.request.target,
                            )
                            emit_handler_error(ctx)
                finally:
                    # Conn-release policy:
                    #
                    # On the success path the handler's ``finish_response``
                    # already re-tracked the conn via ``_maybe_give_back`` —
                    # we MUST NOT touch it here. We can't read
                    # ``ctx._conn.tracked`` either: that field is shared
                    # with the next request's dispatcher, which clears it
                    # via ``stop_tracking`` before this finally runs.
                    #
                    # The only case left to handle here is per-request
                    # cancellation: the handler caught the signal and
                    # returned, but the conn is in an uncertain state.
                    # ``cancel.fired`` is the cheap (no-syscall) check.
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
            yield _Pool(tx, shutdown_event)
        finally:
            shutdown_event.set()
            tx.close()


# --- Public wrappers ----------------------------------------------------


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
      buffered, ``stop_tracking`` s the conn and queues the work for a
      worker. ``max_concurrency`` workers pull from the channel and
      run the continuation under a per-request cancel scope.

    Per-request cancellation surfaces through
    :func:`localpost.http.check_cancelled` (client disconnect via
    non-blocking ``MSG_PEEK``; pool shutdown via a single
    :class:`threading.Event` shared by every in-flight token).

    Example::

        async with thread_pool_handler(router.as_handler(), max_concurrency=8) as h:
            async with http_server(config, h):
                ...
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be >= 1")
    return _thread_pool_handler(inner, max_concurrency)


@asynccontextmanager
async def _thread_pool_handler(
    inner: RequestHandler, max_concurrency: int
) -> AsyncGenerator[RequestHandler]:
    async with _pool_context(max_concurrency) as pool:

        def wrapped(ctx: HTTPReqCtx) -> BodyHandler | None:
            result = inner(ctx)
            if result is None:
                return None
            return pool.dispatch_buffered(result)

        yield wrapped


def streaming_pool_handler(
    inner: _WorkFn,
    /,
    *,
    max_concurrency: int,
) -> AbstractAsyncContextManager[RequestHandler]:
    """Async context manager: yields a ``RequestHandler`` that runs
    ``inner`` on a worker thread with a *borrowed* conn — body **not**
    pre-buffered.

    Use for streaming uploads / large bodies. The worker reads via
    ``ctx.receive(...)`` on a blocking-with-timeout socket, then emits
    a response.

    ``inner`` shape: ``(ctx) -> None``. Must complete the response or
    close the conn.

    Per-request cancellation surfaces through
    :func:`localpost.http.check_cancelled` — same triggers as
    :func:`thread_pool_handler`.

    Example::

        def upload(ctx: HTTPReqCtx) -> None:
            with open("/tmp/u", "wb") as f:
                while chunk := ctx.receive(8192):
                    f.write(chunk)
            ctx.complete(NativeResponse(204, [(b"content-length", b"0")]), b"")

        async with streaming_pool_handler(upload, max_concurrency=4) as h:
            async with http_server(config, h):
                ...
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be >= 1")
    return _streaming_pool_handler(inner, max_concurrency)


@asynccontextmanager
async def _streaming_pool_handler(
    inner: _WorkFn, max_concurrency: int
) -> AsyncGenerator[RequestHandler]:
    async with _pool_context(max_concurrency) as pool:
        yield pool.dispatch_streaming(inner)
