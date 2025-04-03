from __future__ import annotations

import math
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Literal, TypeVar, cast

from anyio import WouldBlock, create_memory_object_stream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from localpost._utils import DelayFactory, ensure_delay_factory, is_async_callable, sleep

from ._flow import (
    Handler,
    HandlerDecorator,
    handler_middleware,
    handler_wrapper,
    logger,
    stream_consumer,
)

T = TypeVar("T")
R = TypeVar("R", Awaitable[None], None)


def delay(value: DelayFactory, /) -> HandlerDecorator[T, Awaitable[None], T, Awaitable[None]]:
    jitter_f = ensure_delay_factory(value)

    @handler_middleware
    async def middleware(next_h: Handler[T]):
        async def _handle(item: T):
            item_jitter = jitter_f()
            await sleep(item_jitter)
            await next_h(item)

        yield _handle

    return middleware


def log_errors(custom_logger=None, /) -> HandlerDecorator[T, R, T, R]:
    h_logger = custom_logger or logger

    @handler_wrapper
    def wrapper(_):
        try:
            yield
        except Exception:  # noqa
            h_logger.exception("Error while processing a message")

    return wrapper


# Does NOT work, as we cannot _stop_ the source (events) from the handler
# def take_first(n: int, /): ...


def skip_first(n: int, /) -> HandlerDecorator[T, R, T, R]:
    if n < 1:
        raise ValueError("n must be greater than or equal to 1")

    @handler_middleware
    async def middleware(next_h: Callable[[T], R]) -> AsyncGenerator[Callable[[T], Any], None]:
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

    return middleware


def buffer(
    capacity: float,
    /,
    *,
    concurrency: int = 1,
    process_leftovers: bool = True,
    full_mode: Literal["wait", "drop"] = "wait",
) -> HandlerDecorator[T, Awaitable[None], T, Awaitable[None]]:
    """
    Buffer items in an in-memory stream.
    """
    if capacity < 0:
        raise ValueError("Buffer capacity must be greater than or equal to 0")
    if concurrency < 1:
        raise ValueError("Concurrency must be greater than or equal to 1")

    @handler_middleware
    async def middleware(next_h: Handler[T]):
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

    return middleware
