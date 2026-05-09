"""Worker-pool wrapper for HTTP request handlers.

:func:`thread_pool_handler` wraps a :data:`RequestHandler` so each
request runs on a worker thread with a *borrowed* connection. Body
reads (``ctx.receive(size)`` / :func:`localpost.http.read_body`) and
syscalls like ``ctx.sendfile`` block the worker, not the selector.

Workers come from a process-wide
:class:`localpost.threadtools.TaskGroup` and are reused across
all pool wrappers / HTTP servers in the process. There is no
concurrency cap — admission control is the deployment's job
(front-LB / OS limits).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from typing import cast

from anyio import to_thread

from localpost import threadtools
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

# --------------------------------------------------------------------------
# Internal pool primitive
# --------------------------------------------------------------------------


class _Pool:
    """Dispatcher that runs request handlers on a shared
    :class:`TaskGroup`.
    """

    __slots__ = ("_shutdown_event", "_tg")

    def __init__(self, tg: threadtools.TaskGroup, shutdown_event: threading.Event) -> None:
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
        # On the success path the handler's response-write code already
        # re-tracked the conn via ``_maybe_give_back`` — we MUST NOT touch
        # it here. We can't read ``ctx.conn.tracked`` either: that field
        # is shared with the next request's dispatcher, which clears it
        # via ``stop_tracking`` before this finally runs.
        #
        # The only case left to handle here is per-request cancellation:
        # the handler caught the signal and returned, but the conn is in
        # an uncertain state. ``cancel.fired`` is the cheap (no-syscall)
        # check.
        if cancel.fired:
            with suppress(Exception):
                ctx.conn.close()


@asynccontextmanager
async def _pool_context() -> AsyncGenerator[_Pool]:
    """Open a worker pool backed by a :class:`TaskGroup`.

    The task group is process-wide in spirit (workers are shared across
    all pools), but each ``_pool_context`` owns its own group so its
    teardown drains exactly the requests it dispatched.

    Reuses the ambient :class:`localpost.threadtools.ThreadPool` when
    one is set (the normal case under :func:`localpost.hosting.run_app`
    / :func:`localpost.hosting.serve`); otherwise opens a private one
    for the duration of this context so :func:`thread_pool_handler` can
    be used standalone.

    On exit, signals in-flight handlers via the cancel event and waits
    for the group to drain. Drain is offloaded to a thread so the
    surrounding event loop stays responsive.
    """
    # Imported lazily to avoid importing threadtools internals at
    # module load time.
    from localpost.threadtools import thread_pool
    from localpost.threadtools._task_group import _current_pool

    async with AsyncExitStack() as stack:
        if _current_pool.get(None) is None:
            await stack.enter_async_context(thread_pool())
        shutdown_event = threading.Event()
        tg = threadtools.TaskGroup(name="http-pool")
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

