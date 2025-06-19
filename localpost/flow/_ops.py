from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Literal

from typing_extensions import TypeVar

from localpost._utils import (
    DelayFactory,
    MemoryStream,
    ensure_async_callable,
    ensure_delay_factory,
    ensure_sync_callable,
    sleep,
)

from ._flow import (
    FlowHandler,
    HandlerDecorator,
    ensure_async_handler,
    handler_middleware,
    logger,
    stream_consumer,
)

T = TypeVar("T", default=Any)
T2 = TypeVar("T2", default=Any)
R = TypeVar("R", Awaitable[None], None)


def delay(value: DelayFactory, /) -> HandlerDecorator[Any, Any]:
    jitter_f = ensure_delay_factory(value)

    @handler_middleware
    async def middleware(next_h: FlowHandler):
        async def _handle_async(item):
            item_jitter = jitter_f()
            await sleep(item_jitter)
            await next_h.async_h(item)

        def _handle_sync(item):
            item_jitter = jitter_f()
            time.sleep(item_jitter.total_seconds())
            next_h.sync_h(item)

        yield next_h.create(async_h=_handle_async, sync_h=_handle_sync)

    return middleware


def log_errors(custom_logger=None, /) -> HandlerDecorator[Any, Any]:
    h_logger = custom_logger or logger

    @handler_middleware
    async def middleware(next_h: FlowHandler):
        async def _handle_async(item):
            try:
                await next_h.async_h(item)
            except Exception:  # noqa
                h_logger.exception("Error while processing a message")

        def _handle_sync(item):
            try:
                next_h.sync_h(item)
            except Exception:  # noqa
                h_logger.exception("Error while processing a message")

        yield next_h.create(async_h=_handle_async, sync_h=_handle_sync)

    return middleware


# Does NOT work, as we cannot _stop_ the source (events) from the handler
# def take_first(n: int, /): ...


def skip_first(n: int, /) -> HandlerDecorator[Any, Any]:
    if n < 1:
        raise ValueError("n must be greater than or equal to 1")

    @handler_middleware
    async def middleware(next_h: FlowHandler):
        iter_n = 0

        async def _handle_async(item):
            nonlocal iter_n
            if iter_n < n:
                iter_n += 1
            else:
                await next_h.async_h(item)

        def _handle_sync(item):
            nonlocal iter_n
            if iter_n < n:
                iter_n += 1
            else:
                next_h.sync_h(item)

        yield next_h.create(async_h=_handle_async, sync_h=_handle_sync)

    return middleware


def buffer(
    capacity: float,
    /,
    *,
    concurrency: int = 1,
    process_leftovers: bool = True,
    full_mode: Literal["wait", "drop"] = "wait",
) -> HandlerDecorator[Any, Any]:
    """
    Buffer items in an in-memory stream.
    """
    if capacity < 0:
        raise ValueError("Buffer capacity must be greater than or equal to 0")
    if concurrency < 1:
        raise ValueError("Concurrency must be greater than or equal to 1")

    @handler_middleware
    async def middleware(next_h: FlowHandler):
        buffer_writer, buffer_reader = MemoryStream.create(capacity)
        stream_h = ensure_async_handler(next_h)
        consumer = stream_consumer(buffer_reader, stream_h, concurrency, process_leftovers)
        async with consumer, buffer_writer:  # As usual, order matters
            if math.isinf(capacity) or full_mode == "drop":
                yield next_h.create(async_h=buffer_writer.send_or_drop_async, sync_h=buffer_writer.send_or_drop)
            else:
                yield FlowHandler.create_async(async_h=buffer_writer.send)

    return middleware


def filter(  # noqa
    func: Callable[[T], Awaitable[bool]] | Callable[[T], bool],
) -> HandlerDecorator[T, T]:
    async_filter = ensure_async_callable(func)
    sync_filter = ensure_sync_callable(func)

    @handler_middleware
    async def middleware(next_h: FlowHandler[T]):
        async def _handle_async(item: T):
            if await async_filter(item):
                await next_h.async_h(item)

        def _handle_sync(item: T):
            if sync_filter(item):
                next_h.sync_h(item)

        yield next_h.create(async_h=_handle_async, sync_h=_handle_sync)

    return middleware


def map(  # noqa
    func: Callable[[T2], Awaitable[T]] | Callable[[T2], T],
) -> HandlerDecorator[T, T2]:
    async_mapper = ensure_async_callable(func)
    sync_mapper = ensure_sync_callable(func)

    @handler_middleware
    async def middleware(next_h: FlowHandler[T]):
        async def _handle_async(item: T2):
            mapped = await async_mapper(item)
            await next_h.async_h(mapped)

        def _handle_sync(item: T2):
            mapped = sync_mapper(item)
            next_h.sync_h(mapped)

        yield next_h.create(async_h=_handle_async, sync_h=_handle_sync)

    return middleware


def flatmap(
    func: Callable[[T2], Awaitable[Iterable[T]]] | Callable[[T2], Iterable[T]],
) -> HandlerDecorator[T, T2]:
    @handler_middleware
    async def middleware(next_h: FlowHandler[T]):
        async_mapper: Callable[[T2], Awaitable[Iterable[T]]] = ensure_async_callable(func)
        sync_mapper: Callable[[T2], Iterable[T]] = ensure_sync_callable(func)

        async def _handle_async(item: T2) -> None:
            mapped = await async_mapper(item)
            for mapped_item in mapped:
                await next_h.async_h(mapped_item)

        def _handle_sync(item: T2) -> None:
            mapped = sync_mapper(item)
            for mapped_item in mapped:
                next_h.sync_h(mapped_item)

        yield next_h.create(async_h=_handle_async, sync_h=_handle_sync)

    return middleware
