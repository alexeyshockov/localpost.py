from collections.abc import AsyncIterator
from contextlib import suppress

from anyio import CapacityLimiter, ClosedResourceError, create_task_group, from_thread, to_thread

from localpost import threadtools, hosting
from localpost._utils import is_async_callable
from localpost.consumers._utils import AnyHandler
from localpost.threadtools import Channel, ReceiveChannel

__all__ = ["channel_consumer"]


# noinspection DuplicatedCode
@hosting.service
async def channel_consumer[T](
    stream: ReceiveChannel[T],
    h: AnyHandler[T],
    /,
    *,
    max_concurrency: int,
    process_leftovers: bool = True,
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
        with suppress(ClosedResourceError):  # Receiver has been closed (according to process_leftovers setting)
            req_sem.acquire()
            for item in stream:
                from_thread.run_sync(tg.start_soon, handler, item)
                req_sem.acquire()

    def handle_items_thread():
        return to_thread.run_sync(handle_items, limiter=CapacityLimiter(1))

    async with stream, create_task_group() as tg:
        tg.start_soon(handle_items_thread)
        yield
        if not process_leftovers:
            # Immediately stop consuming (close the receiver) and ignore the remaining items
            stream.close()
        # Otherwise process all the remaining items (until the source stream is completed)


def _sample_usage():
    import logging
    import signal
    import time

    import anyio

    logging.basicConfig(level=logging.DEBUG)

    def log_item(x):
        time.sleep(0.5)
        logging.info(f"Received: {x}")

    async def _run():
        sender, receiver = Channel.create()
        consumer = channel_consumer(receiver, log_item, max_concurrency=5)

        async with consumer, sender:
            for i in range(10):
                sender.put_nowait(i)
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                async for _ in signals:
                    break  # Shutdown

    # noinspection PyTypeChecker
    anyio.run(_run)


if __name__ == "__main__":
    _sample_usage()
