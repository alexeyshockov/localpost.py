from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import final
from wsgiref.types import WSGIApplication

import anyio
from anyio import to_thread, CancelScope, from_thread, create_task_group

from .config import WorkerConfig
from .server import start_http_server, ClientConn, Server

__all__ = ['Worker', 'serve']


@asynccontextmanager
async def serve(app: WSGIApplication, config: WorkerConfig, /):
    """Run multiple servers (workers)."""
    threads_limiter = anyio.CapacityLimiter(config.max_concurrent_requests)
    conn_sem = threading.BoundedSemaphore(config.max_concurrent_requests)

    def handle_client(c: ClientConn) -> None:
        try:
            c()
        finally:
            conn_sem.release()

    def handle_client_thread(c: ClientConn) -> Awaitable[None]:
        return to_thread.run_sync(handle_client, c, limiter=threads_limiter)

    def handle_clients_thread() -> Awaitable[None]:
        return to_thread.run_sync(handle_clients, limiter=anyio.CapacityLimiter(1))

    def handle_clients() -> None:
        for client_conn in server:
            conn_sem.acquire()  # TODO Check from_thread.check_cancelled() periodically
            # Process the client in a separate thread, to support multiple concurrent connections
            from_thread.run_sync(tg.start_soon, handle_client_thread, client_conn)

    with start_http_server(app, config.server) as server:
        async with create_task_group() as tg:
            tg.start_soon(handle_clients_thread)
            sm = Worker(server, config, tg.cancel_scope)
            yield sm
            sm.shutdown()


@final
@dataclass(frozen=True, slots=True)
class Worker:
    server: Server
    config: WorkerConfig
    _cancel_scope: CancelScope

    def shutdown(self) -> None:
        """Graceful shutdown (stop handling new connections, wait for in-flight requests)."""
        self.server.shutdown()


def _sample_usage():
    logging.basicConfig(level=logging.DEBUG)

    def simple_app(_, start_response):
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [f'Hello from worker thread {threading.get_ident()}!\n'.encode('utf-8')]

    async def _run():
        async with serve(simple_app, WorkerConfig()) as w:
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                async for _ in signals:
                    w.shutdown()
                    break

    # noinspection PyTypeChecker
    anyio.run(_run)


if __name__ == '__main__':
    _sample_usage()
