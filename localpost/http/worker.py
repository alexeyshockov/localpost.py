from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Awaitable
from wsgiref.types import WSGIApplication

import anyio
from anyio import create_task_group, from_thread, to_thread
from anyio.abc import TaskGroup

from localpost import hosting, threadtools
from localpost.http.config import WorkerConfig
from localpost.http.server import start_http_server, RequestHandler
from localpost.http.wsgi import wrap_wsgi

__all__ = ["http_server"]





@hosting.service
async def http_server(app: WSGIApplication, config: WorkerConfig, /) -> AsyncIterator[None]:
    """Run multiple servers (workers)."""
    handler = wrap_wsgi(app)
    req_threads = anyio.CapacityLimiter(config.max_requests)
    req_sem = threadtools.cancellable_semaphore(config.max_requests)

    def handle_client(c):
        try:
            c(handler)
        finally:
            req_sem.release()

    def handle_client_thread(c) -> Awaitable[None]:
        return to_thread.run_sync(handle_client, c, limiter=req_threads)

    def handle_clients():
        req_sem.acquire()
        for c in server.accept():
            # Handle each client connection in a separate thread
            from_thread.run_sync(tg.start_soon, handle_client_thread, c)
            req_sem.acquire()

    def handle_clients_thread() -> Awaitable[None]:
        return to_thread.run_sync(handle_clients, limiter=anyio.CapacityLimiter(1))

    async with create_task_group() as tg:
        with start_http_server(config.server) as server:
            tg.start_soon(handle_clients_thread)
            yield

