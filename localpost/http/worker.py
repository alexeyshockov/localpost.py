from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Awaitable, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import final
from wsgiref.types import WSGIApplication

import anyio
from anyio import CancelScope, create_task_group, from_thread, to_thread

from localpost._sync_utils import _acquire
from localpost.http.config import WorkerConfig
from localpost.http.server import Server, start_http_server
from localpost.http.wsgi import wrap_wsgi

__all__ = ["Worker", "serve"]


@asynccontextmanager
async def serve(app: WSGIApplication, config: WorkerConfig, /) -> AsyncIterator[Worker]:
    """Run multiple servers (workers)."""
    handler = wrap_wsgi(app)
    req_threads = anyio.CapacityLimiter(config.max_requests)
    req_sem = threading.BoundedSemaphore(config.max_requests)

    def handle_client(c):
        try:
            c(handler)
        finally:
            req_sem.release()

    def handle_client_thread(c) -> Awaitable[None]:
        return to_thread.run_sync(handle_client, c, limiter=req_threads)

    def schedule_client_handler(c):
        # Handle each client connection in a separate thread
        from_thread.run_sync(tg.start_soon, handle_client_thread, c)

    def handle_clients():
        _acquire(req_sem)
        for c in server.accept():
            schedule_client_handler(c)
            _acquire(req_sem)

    def handle_clients_thread() -> Awaitable[None]:
        return to_thread.run_sync(handle_clients, limiter=anyio.CapacityLimiter(1))

    async with create_task_group() as tg:
        with start_http_server(config.server) as server:
            tg.start_soon(handle_clients_thread)
            yield Worker(server, config, tg.cancel_scope)


@final
@dataclass(frozen=True, slots=True)
class Worker:
    server: Server
    config: WorkerConfig
    _cancel_scope: CancelScope

    def shutdown(self) -> None:
        """Graceful shutdown (stop handling new connections, wait for in-flight requests)."""
        self.server.close()


def _sample_usage():
    logging.basicConfig(level=logging.DEBUG)

    def simple_app(_, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [f"Hello from worker thread {threading.get_ident()}!\n".encode()]

    async def _run():
        async with serve(simple_app, WorkerConfig()) as w:
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                async for _ in signals:
                    w.shutdown()
                    break

    # noinspection PyTypeChecker
    anyio.run(_run)


if __name__ == "__main__":
    _sample_usage()
