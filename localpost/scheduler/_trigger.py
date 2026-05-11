from __future__ import annotations

import logging
from functools import wraps

from localpost._utils import DelayFactory, cancellable_from, ensure_delay_factory, maybe_closing, sleep, td_str

from ._scheduler import ScheduledTask, Trigger, TriggerFactory, TriggerFactoryDecorator, TriggerFactoryMiddleware

logger = logging.getLogger("localpost.scheduler.cond")


def trigger_factory_middleware[T, T2](
    middleware: TriggerFactoryMiddleware[T, T2],
) -> TriggerFactoryDecorator[T, T2]:
    """Adapt an async-generator middleware into a ``TriggerFactoryDecorator``.

    The middleware receives the source ``Trigger[T]`` and yields ``T2`` items; we wrap the resulting
    async generator with ``maybe_closing`` so it satisfies the ``Trigger[T2]`` (context manager) shape.
    """

    def _decorator(source: TriggerFactory[T]) -> TriggerFactory[T2]:
        @wraps(source)
        def _run(task) -> Trigger[T2]:
            return maybe_closing(middleware(source(task), task))

        return _run

    return _decorator


def take_first[T](n: int, /) -> TriggerFactoryDecorator[T, T]:
    if n < 0:
        raise ValueError("n must be a non-negative integer")

    @trigger_factory_middleware
    async def middleware(source: Trigger[T], _):
        if n == 0:
            return
        async with source as events:
            iter_n = 0
            async for event in events:
                iter_n += 1
                yield event
                if iter_n >= n:
                    break

    return middleware


def delay[T](value: DelayFactory, /) -> TriggerFactoryDecorator[T, T]:
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
