"""Worker-pool wrapper for HTTP request handlers.

:func:`thread_pool_handler` wraps a :data:`RequestHandler` so each
request runs on a worker thread (borrowed from a caller-owned
:class:`localpost.threadtools.Executor`) with a borrowed connection.
Body reads (``ctx.receive(size)`` / :func:`localpost.http.read_body`)
and syscalls like ``ctx.sendfile`` block the worker, not the selector.

The executor is *not* owned by this wrapper — pass an open executor in.
The wrapper opens an internal :class:`localpost.threadtools.TaskGroup`
on the caller's executor for the duration of the ``async with`` so it
can drain only the requests it dispatched at handler shutdown.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import cast

from anyio import to_thread

from localpost.http._base import (
    PAYLOAD_TOO_LARGE_BODY,
    PAYLOAD_TOO_LARGE_RESPONSE,
    RequestHandler,
    _NativeReqCtx,
    emit_handler_error,
)
from localpost.http._cancel import RequestCancel, RequestCancelled, _enter_request
from localpost.http._types import BodyTooLarge
from localpost.http.config import LOGGER_NAME
from localpost.threadtools import Executor, TaskGroup


class _Pool:
    """Dispatcher that runs request handlers on a shared :class:`TaskGroup`."""

    __slots__ = ("_shutdown_event", "_tg")

    def __init__(self, tg: TaskGroup, shutdown_event: threading.Event) -> None:
        self._tg = tg
        self._shutdown_event = shutdown_event

    def dispatch(self, inner: RequestHandler) -> RequestHandler:
        """Wrap ``inner`` so each request borrows the conn and queues
        for a worker. Worker runs ``inner(ctx)`` on a blocking-with-timeout
        socket; ``inner`` reads the body via ``ctx.receive(...)`` and
        completes the response."""
        tg = self._tg
        shutdown_event = self._shutdown_event

        def dispatcher(ctx: _NativeReqCtx) -> None:
            ctx.conn.selector.stop_tracking(ctx.conn)
            cancel = RequestCancel(_sock=ctx.conn.sock, _shutdown_event=shutdown_event)
            tg.start_soon(_run_request, ctx, cancel, inner)

        def pre_body(ctx: _NativeReqCtx) -> None:
            # httptools fires callbacks inside ``parser.feed_data`` — defer
            # the worker dispatch until the parser feed returns so the
            # worker doesn't race the parser. h11 has no such constraint.
            defer = getattr(ctx, "_defer_streaming_dispatch", None)
            if defer is not None:
                defer(dispatcher)
            else:
                dispatcher(ctx)

        return cast(RequestHandler, pre_body)


def _run_request(ctx: _NativeReqCtx, cancel: RequestCancel, fn: RequestHandler) -> None:
    """Run a single request on the worker thread.

    Body-too-large maps to 413; other exceptions are logged and turned
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
                with suppress(Exception):
                    ctx.conn.close()
            except BodyTooLarge:
                _emit_body_too_large(ctx)
            except Exception:
                logger.exception(
                    "Pool handler raised for %s %r",
                    ctx.request.method,
                    ctx.request.target,
                )
                emit_handler_error(ctx)
    finally:
        # On the success path the handler's response-write code already re-tracked the conn
        # via ``_maybe_give_back``; we MUST NOT touch it here. The only case left is per-request
        # cancellation: the handler caught the signal and returned, but the conn is in an
        # uncertain state. ``cancel.fired`` is the cheap (no-syscall) check.
        if cancel.fired:
            with suppress(Exception):
                ctx.conn.close()


def _emit_body_too_large(ctx: _NativeReqCtx) -> None:
    """Map worker-side body-limit failures to 413 instead of generic 500."""
    if ctx.response_status is None:
        with suppress(Exception):
            ctx.complete(PAYLOAD_TOO_LARGE_RESPONSE, PAYLOAD_TOO_LARGE_BODY)
            return
    with suppress(Exception):
        ctx.conn.close()


@asynccontextmanager
async def thread_pool_handler(
    inner: RequestHandler,
    executor: Executor,
    /,
) -> AsyncGenerator[RequestHandler]:
    """Yields a :data:`RequestHandler` that runs each request on a worker
    thread borrowed from ``executor``.

    ``inner`` runs on a worker on a blocking-with-timeout socket — body
    reads (``ctx.receive(...)`` / :func:`localpost.http.read_body`) and
    other blocking syscalls don't stall the selector. The executor is
    caller-owned; this wrapper does not enter / close it.

    Per-request cancellation surfaces through
    :func:`localpost.http.check_cancelled` (client disconnect via
    non-blocking ``MSG_PEEK``; handler shutdown via a single
    :class:`threading.Event` shared by every in-flight token).

    On exit, signals in-flight handlers via the cancel event and waits
    for the internal task group to drain. The drain is offloaded to a
    thread so the surrounding event loop stays responsive.

    Example::

        with WorkerExecutor() as ex:
            async with thread_pool_handler(router.as_handler(), ex) as h:
                async with http_server(config, h):
                    ...
    """
    shutdown_event = threading.Event()
    tg = TaskGroup(executor, name="http-pool")
    tg.__enter__()
    try:
        yield _Pool(tg, shutdown_event).dispatch(inner)
    finally:
        shutdown_event.set()
        await to_thread.run_sync(tg.__exit__, None, None, None)


# Streaming wrapping shape ended up identical to ``thread_pool_handler`` after the dispatch
# unification — keep the alias so callers that prefer the streaming-shaped name still resolve.
streaming_pool_handler = thread_pool_handler
