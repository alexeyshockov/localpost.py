import math
from collections.abc import Sequence, Callable
from contextlib import asynccontextmanager
from typing import Any

from anyio import EndOfStream, ClosedResourceError, create_task_group, move_on_after
from anyio.abc import ObjectReceiveStream
from typing_extensions import TypeVar

from ._flow import (
    AsyncHandler,
    logger,
)
from .._utils import start_task_soon

T = TypeVar("T", default=Any)
TC = TypeVar("TC", bound=Sequence[object], default=Sequence[object])  # A collection of T objects (for batching)


@asynccontextmanager
async def stream_consumer(
    source: ObjectReceiveStream[T],
    h: AsyncHandler[T],
    concurrency: int = 1,
    process_leftovers: bool = True,
):
    async def consume(handle_soon: bool):
        while True:
            try:
                item = await source.receive()

                if handle_soon:  # Infinite concurrency, just spawn a new task for each item
                    tg.start_soon(h, item)
                else:
                    await h(item)
            except EndOfStream:
                logger.debug("Source stream has been completed, no more items to consume")
                break
            except ClosedResourceError:
                logger.debug("Receiver has been closed (according to consumer's process_leftovers setting)")
                break

    async with source, create_task_group() as tg:
        if math.isinf(concurrency):
            tg.start_soon(consume, True)
        else:
            for _ in range(concurrency):
                tg.start_soon(consume, False)

        yield

        if process_leftovers:
            # Process all the remaining items (until the source stream is completed)
            pass
        else:
            # Immediately stop consuming (close the receiver) and ignore the remaining items
            await source.aclose()


@asynccontextmanager
async def stream_batch_consumer(  # noqa: C901 (ignore complexity)
    source: ObjectReceiveStream[T],
    h: AsyncHandler[Sequence[object]],
    items_f: Callable[[Sequence[T]], TC] | None,
    batch_size: int,
    batch_window: int | float,  # Seconds
    process_leftovers: bool = True,
):
    async def read_batch() -> Sequence[T]:
        items: list[T] = []
        try:
            with move_on_after(batch_window):
                while len(items) < batch_size:
                    message = await source.receive()
                    items.append(message)
            return items
        except EndOfStream:
            if items:
                return items  # Return the last batch first
            raise

    async def consume():
        while True:
            try:
                items = await read_batch()
                if items:
                    await h(items_f(items) if items_f is not None else items)
            except EndOfStream:
                logger.debug("Source stream has been completed, no more items to consume")
                break
            except ClosedResourceError:
                logger.debug("Receiver has been closed (according to consumer's process_leftovers setting)")
                break

    async with source, create_task_group() as tg:
        start_task_soon(tg, consume)

        yield

        if process_leftovers:
            # Process all the remaining items (until the source stream is completed)
            pass
        else:
            # Immediately stop consuming (close the receiver) and ignore the remaining items
            await source.aclose()
