import sys
import threading
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from queue import Queue, SimpleQueue

import anyio
from anyio import create_task_group, CancelScope, to_thread, CapacityLimiter, from_thread

from localpost._sync_utils import acquire_sem, pull_queue
from localpost.consumers._utils import AsyncHandler, ensure_sync_handler

if sys.version_info >= (3, 13):
    from queue import ShutDown
else:
    class ShutDown(Exception):
        pass


__all__ = ["queue_consumer"]


@asynccontextmanager
async def queue_consumer[T](
    stream: Queue[T] | SimpleQueue[T],
    h: AsyncHandler[T],
    /,
    *,
    max_concurrency: int,
    process_leftovers: bool = True,
    shutdown_timeout: float = 5.0,
) -> AsyncIterator[None]:
    req_threads = CapacityLimiter(max_concurrency)
    req_sem = threading.BoundedSemaphore(max_concurrency)
    handler = ensure_sync_handler(h)

    def handle_item(item):
        try:
            handler(item)
        finally:
            req_sem.release()

    def handle_item_thread(i) -> Awaitable[None]:
        return to_thread.run_sync(handle_item, i, limiter=req_threads)

    def handle_items():
        acquire_sem(req_sem)
        while True:
            from_thread.run_sync(tg.start_soon, handle_item_thread, pull_queue(stream))
            acquire_sem(req_sem)

    async def handle_items_thread():
        with shutdown_scope:
            await to_thread.run_sync(handle_items, limiter=CapacityLimiter(1))

    async with create_task_group() as tg:
        shutdown_scope = CancelScope()
        tg.start_soon(handle_items_thread)
        yield
        if not process_leftovers:
            # Immediately stop consuming and ignore the remaining items
            shutdown_scope.cancel()
        else:
            # Wait for the remaining items to be processed (until the source queue is closed) or
            # until the shutdown timeout is reached
            shutdown_scope.deadline = anyio.current_time() + shutdown_timeout


def _sample_usage():
    import logging
    import anyio

    logging.basicConfig(level=logging.DEBUG)

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
