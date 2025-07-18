from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from typing import Any, Literal, overload

from anyio import fail_after
from typing_extensions import TypeVar

from localpost._utils import (
    DelayFactory,
    MemoryStream,
    ensure_async_callable,
    ensure_delay_factory,
    ensure_int_or_inf,
    ensure_sync_callable,
    sleep,
)

from ._flow import (
    FlowHandler,
    HandlerDecorator,
    ensure_async_handler,
    handler_middleware,
    logger,
)
from ._stream import BatchReceiver, create_stream_consumer

T = TypeVar("T", default=Any)
T2 = TypeVar("T2", default=Any)
TC = TypeVar("TC", bound=Collection[object], default=Collection[object])
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

        yield _handle_async, _handle_sync

    return middleware


def log_errors(custom_logger=None, /) -> HandlerDecorator[Any, Any]:
    h_logger = custom_logger or logger

    @handler_middleware
    async def middleware(next_h: FlowHandler):
        async def _handle_async(item):
            try:
                await next_h.async_h(item)
            except Exception:
                h_logger.exception("Error while processing a message")

        def _handle_sync(item):
            try:
                next_h.sync_h(item)
            except Exception:
                h_logger.exception("Error while processing a message")

        yield _handle_async, _handle_sync

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

        yield _handle_async, _handle_sync

    return middleware


def buffer(
    capacity: int | float,
    /,
    *,
    concurrency: int | float = 1,
    process_leftovers: bool = True,
    full_mode: Literal["wait", "drop"] = "wait",
) -> HandlerDecorator[Any, Any]:
    """Buffer items in an async in-memory stream."""

    @handler_middleware
    async def middleware(next_h: FlowHandler):
        buffer_writer, buffer_reader = MemoryStream.create(capacity)
        stream_h = ensure_async_handler(next_h, max_threads=concurrency)
        # As usual, order matters
        async with create_stream_consumer(buffer_reader, stream_h, concurrency=concurrency):
            async with buffer_writer:
                if math.isinf(capacity) or full_mode == "drop":
                    yield buffer_writer.send_or_drop, buffer_writer.send_or_drop_from_thread
                else:
                    yield buffer_writer.send
            if not process_leftovers:
                buffer_reader.close()

    ensure_int_or_inf(capacity, min_value=0, name="Buffer capacity")
    ensure_int_or_inf(concurrency, min_value=1, name="Concurrency")
    return middleware


# Maybe implement later
# def sync_buffer(
#     capacity: float,
#     /,
#     *,
#     consumers: int = 1,
#     process_leftovers: bool = True,
#     full_mode: Literal["wait", "drop"] = "wait",
# ) -> HandlerDecorator[Any, Any]:
#     pass


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
    items_f: Callable[[list[T]], TC] | None = None,
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
    if batch_window <= 0:
        raise ValueError("Batch window must be greater than 0")
    ensure_int_or_inf(capacity, min_value=0, name="Buffer capacity")

    @handler_middleware
    async def _middleware(next_h: FlowHandler[Collection[object]]):
        buffer_writer, buffer_reader = MemoryStream[T].create(capacity)
        buffer_batch_reader = BatchReceiver[T, TC](
            buffer_reader, batch_size=batch_size, batch_window=batch_window, items_f=items_f
        )
        stream_h = ensure_async_handler(next_h)
        async with create_stream_consumer(buffer_batch_reader, stream_h, concurrency=1):
            async with buffer_writer:
                if math.isinf(capacity) or full_mode == "drop":
                    yield buffer_writer.send_or_drop, buffer_writer.send_or_drop_from_thread
                else:
                    yield buffer_writer.send
            if not process_leftovers:
                buffer_reader.close()

    return _middleware


def timeout(duration: float, /) -> HandlerDecorator[T, T]:
    """Async timeout middleware."""

    @handler_middleware
    async def middleware(next_h: FlowHandler[T]):
        async def _handle_async(item: T):
            with fail_after(duration):
                await next_h.async_h(item)

        yield _handle_async

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

        yield _handle_async, _handle_sync

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

        yield _handle_async, _handle_sync

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

        yield _handle_async, _handle_sync

    return middleware
