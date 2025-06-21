from __future__ import annotations

import math
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal, overload

from anyio import (
    ClosedResourceError,
    EndOfStream,
    create_task_group,
    move_on_after,
)
from anyio.abc import ObjectReceiveStream
from typing_extensions import TypeVar

from localpost._utils import MemoryStream, start_task_soon

from ._flow import (
    AsyncHandler,
    FlowHandler,
    HandlerDecorator,
    ensure_async_handler,
    handler_middleware,
    logger,
)

T = TypeVar("T", default=Any)
TC = TypeVar("TC", bound=Sequence[object], default=Sequence[object])  # A collection of T objects (for batching)


@overload
def batch(
    batch_size: int,
    batch_window: int | float,  # Seconds
    /,
    *,
    capacity: int | float = 0,
    process_leftovers: bool = True,
    full_mode: Literal["wait", "drop"] = "wait",
) -> HandlerDecorator[Sequence[Any], Any]: ...


@overload
def batch(
    batch_size: int,
    batch_window: int | float,  # Seconds
    items_f: Callable[[Sequence[T]], TC],
    /,
    *,
    capacity: int | float = 0,
    process_leftovers: bool = True,
    full_mode: Literal["wait", "drop"] = "wait",
) -> HandlerDecorator[TC, T]: ...


def batch(
    batch_size: int,
    batch_window: int | float,  # Seconds
    items_f: Callable[[Sequence[T]], TC] | None = None,
    /,
    *,
    capacity: int | float = 0,
    process_leftovers: bool = True,
    full_mode: Literal["wait", "drop"] = "wait",
) -> HandlerDecorator[Any, T]:
    """
    Collect items into batches.

    A new batch is produced when `batch_size` is reached or `batch_window` expires.
    """
    if batch_size < 1:
        raise ValueError("Batch size must be greater than or equal to 1")
    if batch_window < 0:
        raise ValueError("Batch window must be greater than 0")
    if capacity < 0:
        raise ValueError("Buffer capacity must be greater than or equal to 0")

    @handler_middleware
    async def _middleware(next_h: FlowHandler[Sequence[object]]) -> AsyncGenerator[FlowHandler[T]]:
        buffer_writer, buffer_reader = MemoryStream.create(capacity)
        stream_h = ensure_async_handler(next_h)
        consumer = stream_batch_consumer(buffer_reader, stream_h, items_f, batch_size, batch_window, process_leftovers)
        async with consumer, buffer_writer:  # As usual, order matters
            if math.isinf(capacity) or full_mode == "drop":
                yield next_h.create(async_h=buffer_writer.send_or_drop_async, sync_h=buffer_writer.send_or_drop)
            else:
                yield FlowHandler.create_async(async_h=buffer_writer.send)

    return _middleware


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
