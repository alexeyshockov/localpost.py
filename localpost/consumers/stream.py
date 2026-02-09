import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import anyio
from anyio import create_task_group, Semaphore, ClosedResourceError, CancelScope
from anyio.abc import ObjectReceiveStream

from localpost._utils import NullSemaphore, ensure_int_or_inf
from localpost.consumers import ensure_async_handler
from localpost.consumers._utils import AsyncHandler

__all__ = ["stream_consumer"]


@asynccontextmanager
async def stream_consumer[T](
    stream: ObjectReceiveStream[T],
    h: AsyncHandler[T],
    /,
    *,
    max_concurrency: int | float = math.inf,
    process_leftovers: bool = True,
) -> AsyncIterator[None]:
    max_concurrency = ensure_int_or_inf(max_concurrency, min_value=1)
    req_sem = Semaphore(max_concurrency) if max_concurrency != math.inf else NullSemaphore()
    handler = ensure_async_handler(h)

    async def handle_item(item):
        try:
            await handler(item)
        finally:
            req_sem.release()

    async def handle_items():
        with suppress(ClosedResourceError):  # Receiver has been closed (according to process_leftovers setting)
            await req_sem.acquire()
            async for item in stream:
                tg.start_soon(handle_item, item)
                await req_sem.acquire()

    async with stream, create_task_group() as tg:
        tg.start_soon(handle_items)
        yield
        if not process_leftovers:
            # Immediately stop consuming (close the receiver) and ignore the remaining items
            await stream.aclose()
        # Otherwise process all the remaining items (until the source stream is completed)


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
