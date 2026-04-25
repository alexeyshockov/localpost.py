from __future__ import annotations

import logging
from collections.abc import Awaitable
from contextlib import ExitStack
from wsgiref.types import WSGIApplication

from anyio import CapacityLimiter, from_thread, to_thread

from localpost import hosting, threadtools
from localpost.hosting import ServiceLifetime
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
    """Run an :func:`start_http_server` loop inside a hosted service.

    Each accepted request is dispatched as an AnyIO task in the service's task group,
    so every request has its own cancellation scope and shutdown cancels in-flight
    handlers. ``max_concurrency`` bounds concurrent handlers and applies back-pressure
    on the accept loop. With the default ``max_concurrency=1`` requests are still
    handed off to the task group (and therefore gain a cancel scope), they just never
    run concurrently with each other.

    ``handler`` runs on an AnyIO worker thread via ``to_thread.run_sync``.
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be >= 1")

    logger = logging.getLogger(LOGGER_NAME)

    def run(lt: ServiceLifetime) -> Awaitable[None]:
        req_slots = threadtools.cancellable_semaphore(max_concurrency)
        handler_limiter = CapacityLimiter(max_concurrency)

        async def handle_request(ctx: HTTPReqCtx, borrow_stack: ExitStack) -> None:
            try:
                try:
                    await to_thread.run_sync(handler, ctx, limiter=handler_limiter)
                except Exception:
                    logger.exception("Handler raised for %s %r", ctx.request.method, ctx.request.target)
                    await to_thread.run_sync(emit_handler_error, ctx, limiter=handler_limiter)
            finally:
                borrow_stack.close()
                req_slots.release()

        def dispatch(ctx: HTTPReqCtx) -> None:
            req_slots.acquire()  # back-pressure
            stack = ExitStack()
            stack.enter_context(ctx.borrow())  # detach the socket from the accept loop
            try:
                from_thread.run_sync(lt.tg.start_soon, handle_request, ctx, stack)
            except BaseException:
                stack.close()
                req_slots.release()
                raise

        def run_server() -> None:
            with start_http_server(config, dispatch) as server:
                lt.set_started()
                while not lt.shutting_down.is_set():
                    from_thread.check_cancelled()
                    server.run()

        return to_thread.run_sync(run_server, limiter=CapacityLimiter(1))

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
