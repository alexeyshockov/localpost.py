from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from typing import Literal, TypeVar, cast

from anyio import WouldBlock, create_memory_object_stream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from localpost._utils import DelayFactory, ensure_delay_factory, is_async_callable, sleep

from ._flow import (
    Handler,
    HandlerDecorator,
    HandlerMiddleware,
    handler_decorator_from_wrapper,
    logger,
    make_handler_decorator,
    stream_consumer,
)

T = TypeVar("T")
R = TypeVar("R", Awaitable[None], None)


def delay(value: DelayFactory, /) -> HandlerDecorator[T, Awaitable[None], T]:
    jitter_f = ensure_delay_factory(value)

    @asynccontextmanager
    async def _middleware(next_h: Handler[T]):
        async def _handle(item: T):
            item_jitter = jitter_f()
            await sleep(item_jitter)
            await next_h(item)

        yield _handle

    return make_handler_decorator(_middleware)


def log_errors(custom_logger=None, /) -> HandlerDecorator[T, R, T]:
    h_logger = custom_logger or logger

    @contextmanager
    def wrapper(_):
        try:
            yield
        except Exception:  # noqa
            h_logger.exception("Error while processing a message")

    return handler_decorator_from_wrapper(wrapper)


# Does NOT work, as we cannot _stop_ the source (events) from the handler
# def take_first(n: int, /): ...


def skip_first(n: int, /) -> HandlerDecorator[T, R, T]:
    if n < 1:
        raise ValueError("n must be greater than or equal to 1")

    @asynccontextmanager
    async def _middleware(next_h: Callable[[T], Awaitable[None]] | Callable[[T], None]):
        iter_n = 0

        if is_async_callable(next_h):

            async def _handle_async(item: T):
                nonlocal iter_n
                if iter_n < n:
                    iter_n += 1
                else:
                    await next_h(item)

            yield _handle_async
        else:

            def _handle_sync(item: T):
                nonlocal iter_n
                if iter_n < n:
                    iter_n += 1
                else:
                    next_h(item)

            yield _handle_sync

    return make_handler_decorator(cast(HandlerMiddleware[T, R, T], _middleware))


def buffer(
    capacity: float,
    /,
    *,
    concurrency: int = 1,
    process_leftovers: bool = True,
    full_mode: Literal["wait", "drop"] = "wait",
) -> HandlerDecorator[T, Awaitable[None], T]:
    """
    Buffer items in an in-memory stream.
    """
    if capacity < 0:
        raise ValueError("Buffer capacity must be greater than or equal to 0")
    if concurrency < 1:
        raise ValueError("Concurrency must be greater than or equal to 1")

    @asynccontextmanager
    async def _middleware(next_h: Handler[T]):
        buffer_writer, buffer_reader = cast(  # For PyCharm, to properly recognize types
            tuple[MemoryObjectSendStream[T], MemoryObjectReceiveStream[T]], create_memory_object_stream(capacity)
        )

        async def send_or_drop(item: T):
            try:
                buffer_writer.send_nowait(item)
            except WouldBlock:
                pass

        consumer = stream_consumer(buffer_reader, next_h, concurrency, process_leftovers)
        async with consumer, buffer_writer:  # As usual, order matters
            if math.isinf(capacity) or full_mode == "drop":
                yield send_or_drop
            else:
                yield buffer_writer.send

    return make_handler_decorator(_middleware)
