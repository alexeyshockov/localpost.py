from __future__ import annotations

import dataclasses as dc
import itertools
import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Generic, TypeVar, final

from anyio import (
    BrokenResourceError,
    EndOfStream,
    WouldBlock,
    create_task_group,
    get_cancelled_exc_class,
)

from localpost._utils import (
    TD_ZERO,
    ClosingContext,
    EventView,
    MemoryStream,
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


@asynccontextmanager
async def wait_trigger(time_spans: Iterable[timedelta], shutting_down: EventView):
    events, events_reader = MemoryStream[None].create(0)

    @cancellable_from(shutting_down)  # DO NOT cancel the main task group
    async def generate():
        try:
            iter_n = 1
            logger.debug("Sleeping for %s (iteration: %s)", td_str(TD_ZERO), iter_n)
            await events.send(None)  # Execute the first iteration immediately
            for iter_delay in time_spans:
                iter_n += 1
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

    # Order matters, the reader should be closed first (so the run loop can stop by itself)
    async with create_task_group() as main_tg, events_reader:
        start_task_soon(main_tg, generate)
        try:
            yield events_reader
        except GeneratorExit:
            # Can happen, if a trigger is wrapped in a middleware, backed by a generator
            # (if we don't do that, it will be an unhandled exception in the task group)
            pass


@final
@dc.dataclass(frozen=True, slots=True)
class Every:
    period: timedelta

    def __repr__(self):
        return f"every({td_str(self.period)})"

    def __call__(self, task: ScheduledTask) -> Trigger[None]:
        return wait_trigger(itertools.cycle([self.period]), task.shutting_down)


def every(period: timedelta | str, /) -> ScheduledTaskTemplate[None]:
    """
    Trigger an event every `period`.
    """
    # return ScheduledTaskTemplate(Every(ensure_td(period))) >> buffer(0, full_mode="drop")
    return ScheduledTaskTemplate(Every(ensure_td(period)))


@final
@dc.dataclass(frozen=True, slots=True)
class After(Generic[ResT]):
    target: Task[Any, ResT]

    def __repr__(self):
        return f"after({self.target!r})"

    def __call__(self, this_task: ScheduledTask) -> Trigger[ResT]:
        task_exec_results = self.target.subscribe()

        async def run():
            try:
                while True:
                    res = await task_exec_results.receive()
                    if res.error:
                        logger.warning("Target task failed, skipping")  # TODO extra
                    else:
                        yield res.value
            except EndOfStream:
                logger.info("Target task completed, stopping")
            finally:
                task_exec_results.close()

        return ClosingContext(run())


def after(target: ScheduledTask[Any, T] | Task[Any, T], /) -> ScheduledTaskTemplate[T]:
    """
    Trigger an event every time the target task completes successfully.
    """
    return ScheduledTaskTemplate(After(target if isinstance(target, Task) else target.task))
