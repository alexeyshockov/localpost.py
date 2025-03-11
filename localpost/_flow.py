from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import TypeVar

from anyio import ClosedResourceError, EndOfStream, create_task_group, move_on_after
from anyio.abc import ObjectReceiveStream

from ._utils import start_task_soon
from .flow import Handler

T = TypeVar("T")

logger = logging.getLogger("localpost.flow")


@asynccontextmanager
async def stream_consumer(
    source: ObjectReceiveStream[T],
    handler: Handler[T],
    concurrency: int = 1,
    process_leftovers: bool = True,
):
    async def consume(handle_soon: bool):
        while True:
            try:
                item = await source.receive()
            except EndOfStream:
                logger.debug("Source stream has been completed, no more items to consume")
                break
            except ClosedResourceError:
                logger.debug("Receiver has been closed (according to consumer's process_leftovers setting)")
                break

            if handle_soon:  # Infinite concurrency, just spawn a new task for each item
                tg.start_soon(handler, item)
            else:
                await handler(item)

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
    handler: Handler[Sequence[T]],
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
                await handler(items)

    async with source, create_task_group() as tg:
        start_task_soon(tg, consume)

        yield

        if process_leftovers:
            # Process all the remaining items (until the source stream is completed)
            pass
        else:
            # Immediately stop consuming (close the receiver) and ignore the remaining items
            await source.aclose()
