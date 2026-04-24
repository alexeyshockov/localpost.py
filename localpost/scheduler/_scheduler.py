from __future__ import annotations

import inspect
import logging
import math
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from dataclasses import dataclass as define
from functools import partial
from typing import Any, TypeAlias, TypeVar, cast, final

import anyio
from anyio import BrokenResourceError, WouldBlock, create_task_group, open_signal_receiver, to_thread
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from localpost._utils import (
    HANDLED_SIGNALS,
    Event,
    EventView,
    MemoryStream,
    Result,
    choose_anyio_backend,
    def_full_name,
    is_async_callable,
    start_task_soon,
    wait_any,
)
from localpost.hosting import ServiceLifetime

# T = TypeVar("T")
# T2 = TypeVar("T2")
# R = TypeVar("R")
DecF = TypeVar("DecF", bound=Callable[..., Any])

type AnyTaskHandler[T, R] = Callable[[T], Awaitable[R]] | Callable[[T], R]
type TaskHandler[T, R] = Callable[[T], Awaitable[R]]

logger = logging.getLogger("localpost.scheduler")


def task_handler[T, R](func: AnyTaskHandler[T, R]) -> TaskHandler[T, R]:
    return func if is_async_callable(func) else partial(to_thread.run_sync, func)


@final
@define()
class ScheduledTaskRun[T, R]:
    task: ScheduledTask[T, R]
    sl: ServiceLifetime
    handle: TaskHandler[T, R]

    def subscribe(self, buffer_max_size: float = math.inf) -> MemoryObjectReceiveStream[Result[R]]:
        # By default, a stream is created with a buffer size of 0, which means that any write will be blocked until
        # there is a free reader. We do not want to block the task execution flow in any way, so:
        #  - the buffer is unbounded by default
        #  - if the buffer is full, the result is dropped (see publish method below)
        send_stream, receive_stream = MemoryStream[Result[R]].create(buffer_max_size)
        self._subscribers.append(self._cm.enter_context(send_stream))
        return receive_stream

    def _publish_result(self, result: Result[R]) -> None:
        for i, subscriber in enumerate(self._subscribers):
            try:
                subscriber.send_nowait(result)
            except BrokenResourceError:  # Subscriber is not active anymore
                del self._subscribers[i]
            except WouldBlock:
                logger.error("Subscriber's buffer is full, dropping the result")

    # MessageHandler[T]
    async def __call__(self, event: T) -> None:
        try:
            result = Result.ok(await self._handle(event))
            self._publish_result(result)
        except TypeError:
            raise
        except Exception as e:
            result = Result.failure(e)
            self._publish_result(result)
            raise

    @property
    def shutting_down(self) -> EventView:
        return self._shutting_down

    async def run(self, shutting_down: EventView) -> None:
        self._shutting_down = shutting_down
        trigger = self._trigger_factory(self)

        async with trigger as t_events, self._handler as message_handler:
            async for t_event in t_events:
                await message_handler(t_event)
            logger.debug(f"{self!r} trigger is completed")
        logger.debug(f"{self!r} is done")


@final
@define()
class ScheduledTask[T, R]:
    name: str
    _until_running: EventView
    current_run: ScheduledTaskRun[T, R] | None = None

    def __init__(self, h: TaskHandler[T, R], tf: TriggerFactory[T], /, *, name: str | None = None):
        self.name = name or def_full_name(h)
        self._handler = h
        self._trigger_f = tf

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.name!r}>"

    async def get_current_run(self) -> ScheduledTaskRun[T, R]:
        await self._until_running.wait()
        assert self.current_run is not None
        return self.current_run

    async def run(self, sl: ServiceLifetime) -> None:
        # TODO
        #  - sl.set_started()
        #  - create and set current_run
        #  -
        pass


type TriggerEvents[T] = AbstractAsyncContextManager[AsyncIterator[T]]
type TriggerFactory[T] = Callable[[TaskGroup, EventView], TriggerEvents[T]]


def scheduled_task[T](tf: TriggerFactory[T], /, *, name: str | None = None) -> Callable[..., ScheduledTask[T, Any]]:
    """Schedule a task with the given trigger."""

    def _decorator[R](func: AnyTaskHandler[T, R]) -> ScheduledTask[T, R]:
        st = ScheduledTask(task_handler(func), tf)
        if name:
            st.name = name
        return st

    return _decorator
