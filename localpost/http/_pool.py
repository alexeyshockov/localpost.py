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

Both wrappers dispatch onto a process-wide
:class:`localpost.threadtools.ThreadTaskGroup` — workers are spawned
on demand and reused across all pool wrappers / HTTP servers in the
process. There is no concurrency cap.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from typing import cast

from anyio import to_thread

from localpost import threadtools
from localpost.http._base import (
    PAYLOAD_TOO_LARGE_BODY,
    PAYLOAD_TOO_LARGE_RESPONSE,
    BodyHandler,
    HTTPReqCtx,
    RequestHandler,
    _NativeReqCtx,
    emit_handler_error,
)
from localpost.http._cancel import RequestCancel, RequestCancelled, _enter_request
from localpost.http._types import BodyTooLarge
from localpost.http.config import LOGGER_NAME

# --------------------------------------------------------------------------
# Internal pool primitive
# --------------------------------------------------------------------------


_WorkFn = Callable[[_NativeReqCtx], None]


class _Pool:
    """Dispatcher that runs request handlers on a shared
    :class:`ThreadTaskGroup`.

    Two ``dispatch_*`` methods let callers wire either a post-body
    :data:`BodyHandler` or a pre-body streaming handler onto the same
    task group.
    """

    __slots__ = ("_shutdown_event", "_tg")

    def __init__(self, tg: threadtools.ThreadTaskGroup, shutdown_event: threading.Event) -> None:
        self._tg = tg
        self._shutdown_event = shutdown_event

    def dispatch_buffered(self, body_handler: BodyHandler) -> BodyHandler:
        """Wrap a :data:`BodyHandler` so when it's invoked post-body it
        borrows the conn and queues for a worker. The worker runs
        ``body_handler(ctx)`` with ``ctx.body`` already populated.

        Native-only: the returned ``BodyHandler`` is cast at the boundary
        — at runtime the ctx is always a :class:`_NativeReqCtx` because
        the pool is composed under :func:`http_server`."""
        return cast(BodyHandler, self._make_dispatcher(body_handler))

    def dispatch_streaming(self, handler: _WorkFn) -> RequestHandler:
        """Wrap a streaming handler so it runs in a worker on a borrowed
        conn (body **not** pre-buffered).

        The returned :data:`RequestHandler` borrows the conn pre-body
        and queues ``handler`` for a worker. Worker runs ``handler(ctx)``
        on a blocking-with-timeout socket; the handler reads the body
        via ``ctx.receive(...)`` and completes the response."""
        dispatcher = self._make_dispatcher(handler)

        def pre_body(ctx: _NativeReqCtx) -> BodyHandler | None:
            defer = getattr(ctx, "_defer_streaming_dispatch", None)
            if defer is not None:
                defer(dispatcher)
            else:
                dispatcher(ctx)
            return None  # we borrowed; selector free

        return cast(RequestHandler, pre_body)

    def _make_dispatcher(self, fn: _WorkFn) -> _WorkFn:
        tg = self._tg
        shutdown_event = self._shutdown_event

        def dispatched(ctx: _NativeReqCtx) -> None:
            ctx.conn.selector.stop_tracking(ctx.conn)
            cancel = RequestCancel(_sock=ctx.conn.sock, _shutdown_event=shutdown_event)
            tg.start_soon(_run_request, ctx, cancel, fn)

        return dispatched


def _run_request(ctx: _NativeReqCtx, cancel: RequestCancel, fn: _WorkFn) -> None:
    """Run a single request on the worker thread.

    Mirrors the failure handling the previous worker loop performed:
    body-too-large maps to 413; other exceptions are logged and turned
    into a generic 500 (or close on the spot if the response already
    started). On per-request cancellation the conn is closed because
    its protocol state is uncertain.
    """
    logger = logging.getLogger(LOGGER_NAME)
    try:
        with _enter_request(cancel):
            try:
                fn(ctx)
            except RequestCancelled:
                # Handler bailed cleanly. Connection state is uncertain — close.
                with suppress(Exception):
                    ctx.conn.close()
            except BodyTooLarge:
                _emit_body_too_large(ctx)
            except Exception:
                # The future captures the exception, but nothing reads it on
                # the HTTP path; log here so failures are visible immediately
                # rather than only at pool shutdown.
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
        # ``ctx.conn.tracked`` either: that field is shared
        # with the next request's dispatcher, which clears it
        # via ``stop_tracking`` before this finally runs.
        #
        # The only case left to handle here is per-request
        # cancellation: the handler caught the signal and
        # returned, but the conn is in an uncertain state.
        # ``cancel.fired`` is the cheap (no-syscall) check.
        if cancel.fired:
            with suppress(Exception):
                ctx.conn.close()


@asynccontextmanager
async def _pool_context() -> AsyncGenerator[_Pool]:
    """Open a worker pool backed by a :class:`ThreadTaskGroup`.

    The task group is process-wide in spirit (workers are shared across
    all pools), but each ``_pool_context`` owns its own group so its
    teardown drains exactly the requests it dispatched.

    On exit, signals in-flight handlers via the cancel event and waits
    for the group to drain. Drain is offloaded to a thread so the
    surrounding event loop stays responsive.
    """
    shutdown_event = threading.Event()
    tg = threadtools.ThreadTaskGroup(name="http-pool")
    tg.__enter__()
    try:
        yield _Pool(tg, shutdown_event)
    finally:
        shutdown_event.set()
        await to_thread.run_sync(tg.__exit__, None, None, None)


def _emit_body_too_large(ctx: _NativeReqCtx) -> None:
    """Map worker-side body-limit failures to 413 instead of generic 500."""
    if ctx.response_status is None:
        with suppress(Exception):
            ctx.complete(PAYLOAD_TOO_LARGE_RESPONSE, PAYLOAD_TOO_LARGE_BODY)
            return
    with suppress(Exception):
        ctx.conn.close()


# --------------------------------------------------------------------------
# Public wrappers
# --------------------------------------------------------------------------


@asynccontextmanager
async def thread_pool_handler(inner: RequestHandler, /) -> AsyncGenerator[RequestHandler]:
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
      worker. Workers run the continuation under a per-request cancel
      scope.

    Workers come from a process-wide
    :class:`localpost.threadtools.ThreadTaskGroup` and are reused across
    all pool wrappers; there is no concurrency cap.

    Per-request cancellation surfaces through
    :func:`localpost.http.check_cancelled` (client disconnect via
    non-blocking ``MSG_PEEK``; pool shutdown via a single
    :class:`threading.Event` shared by every in-flight token).

    Example::

        async with thread_pool_handler(router.as_handler()) as h:
            async with http_server(config, h):
                ...
    """
    async with _pool_context() as pool:

        def wrapped(ctx: HTTPReqCtx) -> BodyHandler | None:
            result = inner(ctx)
            if result is None:
                return None
            return pool.dispatch_buffered(result)

        yield wrapped


@asynccontextmanager
async def streaming_pool_handler(inner: _WorkFn, /) -> AsyncGenerator[RequestHandler]:
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
            ctx.complete(Response(204, [(b"content-length", b"0")]), b"")


        async with streaming_pool_handler(upload) as h:
            async with http_server(config, h):
                ...
    """
    async with _pool_context() as pool:
        yield pool.dispatch_streaming(inner)
