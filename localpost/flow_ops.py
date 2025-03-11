from __future__ import annotations

import logging
import math
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Literal, ParamSpec, TypeVar, cast

from anyio import WouldBlock, create_memory_object_stream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from localpost._utils import DelayFactory, ensure_delay_factory, sleep

from ._flow import stream_batch_consumer, stream_consumer
from .flow import Handler, HandlerDecorator, make_handler_decorator

T = TypeVar("T")
P = ParamSpec("P")

__all__ = [
    "delay",
    "log_errors",
    "skip_first",
    "buffer",
    "batch",
]

logger = logging.getLogger(__name__)


def delay(value: DelayFactory, /):
    jitter_f = ensure_delay_factory(value)

    @asynccontextmanager
    async def _middleware(next_mh: Handler[T]):
        async def _handle(item: T):
            item_jitter = jitter_f()
            await sleep(item_jitter)
            await next_mh(item)

        yield _handle

    return make_handler_decorator(_middleware)


def log_errors(custom_logger=None, /):
    @asynccontextmanager
    async def _middleware(next_mh: Handler[T]):
        mh_logger = custom_logger or logger

        async def _handle(item: T):
            try:
                await next_mh(item)
            except Exception:  # noqa
                mh_logger.exception("Error while processing a message")

        yield _handle

    return make_handler_decorator(_middleware)


# Does NOT work, as we cannot _stop_ the source (events) from the handler
# def take_first(n: int, /): ...


def skip_first(n: int, /):
    if n < 1:
        raise ValueError("n must be greater than or equal to 1")

    @asynccontextmanager
    async def _middleware(next_mh: Handler[T]):
        iter_n = 0

        async def _handle(item: T):
            nonlocal iter_n
            if iter_n < n:
                iter_n += 1
            else:
                await next_mh(item)

        yield _handle

    return make_handler_decorator(_middleware)


def buffer(
    capacity: float,
    /,
    *,
    concurrency: int = 1,
    process_leftovers: bool = True,
    full_mode: Literal["wait", "drop"] = "wait",
) -> HandlerDecorator[P, T, T]:
    """
    Buffer items in an in-memory stream.
    """
    if capacity < 0:
        raise ValueError("Buffer capacity must be greater than or equal to 0")
    if concurrency < 1:
        raise ValueError("Concurrency must be greater than or equal to 1")

    @asynccontextmanager
    async def _middleware(next_mh: Handler[T]):
        buffer_writer, buffer_reader = cast(  # For PyCharm, to properly recognize types
            tuple[MemoryObjectSendStream[T], MemoryObjectReceiveStream[T]], create_memory_object_stream(capacity)
        )

        async def send_or_drop(item: T):
            try:
                buffer_writer.send_nowait(item)
            except WouldBlock:
                pass

        consumer = stream_consumer(buffer_reader, next_mh, concurrency, process_leftovers)
        async with consumer, buffer_writer:  # As usual, order matters
            if math.isinf(capacity) or full_mode == "drop":
                yield send_or_drop
            else:
                yield buffer_writer.send

    return make_handler_decorator(_middleware)


def batch(
    batch_size: int,
    batch_window: int | float,  # Seconds
    /,
    *,
    capacity: int | float = 0,
    process_leftovers: bool = True,
    full_mode: Literal["wait", "drop"] = "wait",
) -> HandlerDecorator[P, Sequence[T], T]:
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
    async def _middleware(next_mh: Handler[Sequence[T]]) -> AsyncGenerator[Handler[T]]:
        buffer_writer, buffer_reader = cast(  # For PyCharm, to properly recognize types
            tuple[MemoryObjectSendStream[T], MemoryObjectReceiveStream[T]], create_memory_object_stream(capacity)
        )

        async def send_or_drop(items: T):
            try:
                buffer_writer.send_nowait(items)
            except WouldBlock:
                pass

        consumer = stream_batch_consumer(buffer_reader, next_mh, batch_size, batch_window, process_leftovers)
        async with consumer, buffer_writer:  # As usual, order matters
            if math.isinf(capacity) or full_mode == "drop":
                yield send_or_drop
            else:
                yield buffer_writer.send

    return make_handler_decorator(_middleware)
