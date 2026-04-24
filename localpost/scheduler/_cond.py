from __future__ import annotations

import dataclasses as dc
import itertools
import logging
from collections.abc import AsyncGenerator, Iterable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from itertools import chain, cycle
from typing import Any, Generic, Self, TypeVar, cast, final

from anyio import (
    BrokenResourceError,
    EndOfStream,
    WouldBlock,
    create_memory_object_stream,
    create_task_group,
    get_cancelled_exc_class,
)
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from localpost._utils import (
    TD_ZERO,
    ClosingContext,
    EventView,
    MemoryStream,
    Result,
    cancellable_from,
    ensure_td,
    sleep,
    start_task_soon,
    td_str,
)

from ._scheduler import ScheduledTask, ScheduledTaskTemplate, Task, Trigger

T = TypeVar("T")
ResT = TypeVar("ResT")

logger = logging.getLogger("localpost.scheduler.cond")


import logging
from collections.abc import AsyncIterator, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from localpost._utils import (
    DelayFactory,
    ensure_delay_factory,
    maybe_closing,
)

# from ._scheduler import TriggerFactoryDecorator

# P = ParamSpec("P")
# T = TypeVar("T")
# Trigger = AsyncIterator[T]
# TriggerDecorator = Callable[[Callable[P, AsyncIterator[T]]], Callable[P, AsyncIterator[T]]]
# TriggerMiddleware = Callable[[AsyncIterator[T]], AsyncIterator[T]]

type TriggerMiddleware[T1, T2] = Callable[[AsyncIterator[T1]], AsyncIterator[T2]]


async def wait_trigger(time_spans: Iterable[timedelta], events: MemoryObjectSendStream[None]) -> None:
    try:
        for iter_n, iter_delay in enumerate(time_spans):
            logger.debug("Sleeping for %s (iteration: %s)", td_str(iter_delay), iter_n)
            await sleep(iter_delay)
            try:
                events.send_nowait(None)
            except WouldBlock:
                logger.warning("All executors are busy, skipping the event")
    except BrokenResourceError:
        logger.debug("All executors have been closed, stopping")
    except get_cancelled_exc_class():
        logger.debug("Task is shutting down, stopping")
        raise
    finally:
        events.close()


@asynccontextmanager
async def apply_middlewares[T](source: T, middlewares: tuple[Callable[[T], T], ...]) -> AsyncGenerator[T]:
    def as_acm(t) -> AbstractAsyncContextManager[T]:
        return t if isinstance(t, AbstractAsyncContextManager) else maybe_closing(t)

    async with AsyncExitStack() as scope:
        source = await scope.enter_async_context(as_acm(source))
        for middleware in middlewares:
            source = await scope.enter_async_context(as_acm(middleware(source)))
        yield source


@final
@dc.dataclass(frozen=True, slots=True)
class Every[T]:
    period: timedelta
    middlewares: tuple[TriggerMiddleware[Any, Any], ...] = ()

    def __repr__(self):
        return f"every({td_str(self.period)})"

    def __rshift__[T2](self, other: TriggerMiddleware[T, T2]) -> Every[T2]:
        return cast(Every[T2], replace(self, middlewares=self.middlewares + (other,)))

    def __call__(self, tg: TaskGroup, shutting_down: EventView) -> AbstractAsyncContextManager[AsyncIterator[T]]:
        sender, receiver = create_memory_object_stream[None]()
        tg.start_soon(wait_trigger, chain([0], cycle([self.period])), sender)

        return apply_middlewares(receiver, self.middlewares)


def every(period: timedelta | str, /) -> ScheduledTaskTemplate[None]:
    """Trigger an event every `period`."""
    return Every(ensure_td(period))


@final
@dc.dataclass(frozen=True, slots=True)
class After[ResT, T]:
    target: Task[Any, ResT]
    middlewares: tuple[TriggerMiddleware[Any, Any], ...] = ()

    def __repr__(self):
        return f"after({self.target!r})"

    def __rshift__[T2](self, other: TriggerMiddleware[T, T2]) -> After[ResT, T2]:
        return cast(After[ResT, T2], replace(self, middlewares=self.middlewares + (other,)))

    def __call__(self, _: TaskGroup, shutting_down: EventView) -> AbstractAsyncContextManager[AsyncIterator[T]]:
        def generate():
            async with self.target.subscribe() as 
                    async for task_exec_results.receive()
                    if res.error:
                        logger.warning("Target task failed, skipping")  # TODO extra
                    else:
                        yield res.value
        
        task_exec_results = self.target.subscribe()
        return apply_middlewares(source, self.middlewares)


def after[T](target: ScheduledTask[Any, T], /) -> ScheduledTaskTemplate[T]:
    """
    Trigger an event every time the target task completes successfully.
    """
    return ScheduledTaskTemplate(After(target if isinstance(target, Task) else target.task))


@final
@dc.dataclass(frozen=True, slots=True)
class AfterAll(Generic[ResT]):
    target: Task[Any, ResT]

    def __repr__(self):
        return f"after_all({self.target!r})"

    def __call__(self, this_task: ScheduledTask) -> Trigger[Result[ResT]]:
        task_exec_results = self.target.subscribe()

        async def run():
            try:
                while True:
                    yield await task_exec_results.receive()
            except EndOfStream:
                logger.info("Target task completed, stopping")
            finally:
                task_exec_results.close()

        return ClosingContext(run())


def after_all(target: ScheduledTask[Any, Result[T]] | Task[Any, Result[T]], /) -> ScheduledTaskTemplate[Result[T]]:
    """
    Trigger an event every time the target task completes (successfully or not).
    """
    return ScheduledTaskTemplate(AfterAll(target if isinstance(target, Task) else target.task))


def take_first(n: int, /) -> TriggerMiddleware:
    if n < 0:
        raise ValueError("N must be a non-negative integer")

    async def middleware(events: AsyncIterator[T]) -> AsyncIterator[T]:
        iter_n = 0
        async with maybe_closing(events):
            async for event in events:
                if iter_n >= n:
                    break
                iter_n += 1
                yield event

    return middleware


def delay(value: DelayFactory, /) -> TriggerFactoryDecorator[T, T]:
    delay_f = ensure_delay_factory(value)

    async def middleware(events: AsyncIterator[T]) -> AsyncIterator[T]:
        async with maybe_closing(events):
            async for event in events:
                item_jitter = delay_f()
                logger.debug("Sleeping for %s (delay)", td_str(item_jitter))
                await sleep(item_jitter)
                yield event

    return middleware
