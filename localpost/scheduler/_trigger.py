from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from functools import wraps
from typing import TypeVar, cast

from localpost._utils import ClosingContext, DelayFactory, cancellable_from, ensure_delay_factory, sleep, td_str

from ._scheduler import ScheduledTask, Trigger, TriggerFactory, TriggerFactoryDecorator, TriggerFactoryMiddleware

T = TypeVar("T")
T2 = TypeVar("T2")

logger = logging.getLogger("localpost.scheduler.cond")


def trigger_factory_middleware(middleware: TriggerFactoryMiddleware[T, T2]) -> TriggerFactoryDecorator[T, T2]:
    def _decorator(source: TriggerFactory[T]) -> TriggerFactory[T2]:
        @wraps(source)
        def _run(task):
            source_events = source(task)
            events = middleware(source_events, task)
            return cast(
                Trigger[T2], events if isinstance(events, AbstractAsyncContextManager) else ClosingContext(events)
            )

        return _run

    return _decorator


def take_first(n: int, /) -> TriggerFactoryDecorator[T, T]:
    if n < 0:
        raise ValueError("n must be a non-negative integer")

    @trigger_factory_middleware
    async def middleware(source: Trigger[T], _):
        iter_n = 0
        if iter_n >= n:
            return
        async with source as events:
            async for event in events:
                iter_n += 1
                yield event
                if iter_n >= n:
                    break

    return middleware


def delay(value: DelayFactory, /) -> TriggerFactoryDecorator[T, T]:
    delay_f = ensure_delay_factory(value)

    @trigger_factory_middleware
    async def middleware(source: Trigger[T], task: ScheduledTask):
        shutdown_aware_sleep = cancellable_from(task.shutting_down)(sleep)
        async with source as events:
            async for event in events:
                item_jitter = delay_f()
                logger.debug("Sleeping for %s (delay)", td_str(item_jitter))
                await shutdown_aware_sleep(item_jitter)
                if task.shutting_down:
                    break
                yield event

    return middleware
