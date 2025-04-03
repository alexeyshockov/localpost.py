from __future__ import annotations

import math
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal, TypeVar, cast, overload

from anyio import (
    ClosedResourceError,
    EndOfStream,
    WouldBlock,
    create_memory_object_stream,
    create_task_group,
    move_on_after,
)
from anyio.abc import ObjectReceiveStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from localpost._utils import start_task_soon

from ._flow import (
    Handler,
    HandlerDecorator,
    logger,
    make_handler_decorator,
)

T = TypeVar("T")
TC = TypeVar("TC", bound=Sequence[Any])  # A collection of T objects (for batching)


@asynccontextmanager
async def stream_batch_consumer(  # noqa: C901 (ignore complexity)
    source: ObjectReceiveStream[T],
    h: Handler[Sequence[Any]],
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
            except EndOfStream:
                logger.debug("Source stream has been completed, no more items to consume")
                break
            except ClosedResourceError:
                logger.debug("Receiver has been closed (according to consumer's process_leftovers setting)")
                break

            if items:
                await h(items_f(items) if items_f is not None else items)

    async with source, create_task_group() as tg:
        start_task_soon(tg, consume)

        yield

        if process_leftovers:
            # Process all the remaining items (until the source stream is completed)
            pass
        else:
            # Immediately stop consuming (close the receiver) and ignore the remaining items
            await source.aclose()


@overload
def batch(
    batch_size: int,
    batch_window: int | float,  # Seconds
    /,
    *,
    capacity: int | float = 0,
    process_leftovers: bool = True,
    full_mode: Literal["wait", "drop"] = "wait",
) -> HandlerDecorator[Sequence[T], Awaitable[None], T]: ...


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
) -> HandlerDecorator[TC, Awaitable[None], T]: ...


def batch(
    batch_size: int,
    batch_window: int | float,  # Seconds
    items_f: Callable[[Sequence[T]], TC] | None = None,
    /,
    *,
    capacity: int | float = 0,
    process_leftovers: bool = True,
    full_mode: Literal["wait", "drop"] = "wait",
) -> HandlerDecorator[Sequence[object], Awaitable[None], T]:
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

    @asynccontextmanager
    async def _middleware(next_h: Handler[Sequence[object]]) -> AsyncGenerator[Handler[T]]:
        buffer_writer, buffer_reader = cast(  # For PyCharm, to properly recognize types
            tuple[MemoryObjectSendStream[T], MemoryObjectReceiveStream[T]], create_memory_object_stream(capacity)
        )

        async def send_or_drop(items: T):
            try:
                buffer_writer.send_nowait(items)
            except WouldBlock:
                pass

        consumer = stream_batch_consumer(buffer_reader, next_h, items_f, batch_size, batch_window, process_leftovers)
        async with consumer, buffer_writer:  # As usual, order matters
            if math.isinf(capacity) or full_mode == "drop":
                yield send_or_drop
            else:
                yield buffer_writer.send

    return make_handler_decorator(_middleware)
