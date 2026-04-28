import queue
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from queue import Queue, SimpleQueue

import anyio
from anyio import CancelScope, CapacityLimiter, create_task_group, from_thread, to_thread

from localpost import threadtools
from localpost._utils import is_async_callable
from localpost.experimental.consumers._utils import AnyHandler

if sys.version_info >= (3, 13):
    from queue import ShutDown
else:

    class ShutDown(Exception):
        pass


__all__ = ["queue_consumer"]


def _pull_queue[T](q: Queue[T] | SimpleQueue[T]) -> T:
    while True:
        threadtools.check_cancelled()
        try:
            return q.get(timeout=threadtools.CHECK_TIMEOUT)
        except queue.Empty:
            continue


# noinspection DuplicatedCode
@asynccontextmanager
async def queue_consumer[T](
    stream: Queue[T] | SimpleQueue[T],
    h: AnyHandler[T],
    /,
    *,
    max_concurrency: int,
    process_leftovers: bool = True,
    shutdown_timeout: float = 5.0,
) -> AsyncIterator[None]:
    req_sem = threadtools.cancellable_semaphore(max_concurrency)

    if is_async_callable(h):

        async def handle_item(item):
            try:
                await h(item)
            finally:
                req_sem.release()

        handler = handle_item
    else:
        req_threads = CapacityLimiter(max_concurrency)

        def handle_item(item):
            try:
                h(item)
            finally:
                req_sem.release()

        def handler(item):
            return to_thread.run_sync(handle_item, item, limiter=req_threads)

    def handle_items():
        with suppress(ShutDown):
            req_sem.acquire()
            while True:
                from_thread.run_sync(tg.start_soon, handler, _pull_queue(stream))
                req_sem.acquire()

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
