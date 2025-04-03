from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import math
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, ExitStack
from functools import partial
from typing import Any, Generic, ParamSpec, Protocol, TypeAlias, TypeVar, Union, cast, final

from anyio import BrokenResourceError, WouldBlock, create_memory_object_stream, create_task_group, to_thread
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from localpost._utils import (
    EventView,
    EventViewProxy,
    Result,
    def_full_name,
    is_async_callable,
    start_task_soon,
    wait_all,
)
from localpost.flow import HandlerDecorator, HandlerManager
from localpost.hosting import Host, HostedServiceFunc, ServiceLifetimeManager

T = TypeVar("T")
T2 = TypeVar("T2")
R = TypeVar("R")

TaskHandler: TypeAlias = Union[
    Callable[[T], Awaitable[R]],
    Callable[[], Awaitable[R]],
    Callable[[T], R],
    Callable[[], R],
]

logger = logging.getLogger("localpost.scheduler")


@final
@dc.dataclass()
class Task(
    Generic[T, R],
    AbstractAsyncContextManager[Callable[[T], Awaitable[None]]],  # HandlerManager[T]
):
    name: str
    event_aware: bool

    def __init__(self, target: TaskHandler[T, R], /, *, name: str | None = None):
        self.name = name or def_full_name(target)
        self._target = target
        e_aware = self.event_aware = len(inspect.signature(target).parameters) > 0

        def e_handler(t) -> Callable[[T], Awaitable[R]]:
            if is_async_callable(t):
                return t if e_aware else lambda _: t()  # type: ignore[misc]
            return (lambda e: to_thread.run_sync(t, e)) if e_aware else (lambda _: to_thread.run_sync(t))

        self._handle = e_handler(target)

        self._cm = ExitStack()
        self._subscribers: list[MemoryObjectSendStream[Result[R]]] = []
        self._users = 0

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.name!r}>"

    def subscribe(self, buffer_max_size: float = math.inf) -> MemoryObjectReceiveStream[Result[R]]:
        # By default, a stream is created with a buffer size of 0, which means that any write will be blocked until
        # there is a free reader. We do not want to block the task execution flow in any way, so:
        #  - the buffer is unbounded by default
        #  - if the buffer is full, the result is dropped (see publish method below)
        send_stream, receive_stream = create_memory_object_stream[Result[R]](buffer_max_size)
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

    async def __call__(self, event: T) -> None:  # MessageHandler[T]
        try:
            result = Result.ok(await self._handle(event))
            self._publish_result(result)
        except TypeError:
            raise
        except Exception as e:
            result = Result.failure(e)
            self._publish_result(result)
            raise

    async def __aenter__(self):
        self._users += 1
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool | None:
        self._users -= 1
        # A task can be scheduled multiple times, so we need to keep the results streams open until all the scheduled
        # tasks are completed
        if self._users == 0:
            return self._cm.__exit__(exc_type, exc_value, traceback)
        return False  # Do not suppress exceptions


@final
class ScheduledTaskTemplate(Generic[T]):
    @classmethod
    def ensure(cls, tpl: TriggerFactory[T]) -> ScheduledTaskTemplate[T]:
        if isinstance(tpl, ScheduledTaskTemplate):
            return tpl
        return cls(tpl)

    def __init__(self, tf: TriggerFactory[T]):
        self._tf = tf
        self._tf_queue: tuple[TriggerFactoryDecorator, ...] = ()
        self._handler_queue: tuple[HandlerDecorator, ...] = ()

    # TriggerFactory[T]
    def __call__(self, *args, **kwargs) -> Trigger[T]:
        return self.tf(*args, **kwargs)

    def __truediv__(self, middleware: TriggerFactoryMiddleware[T, T2]) -> ScheduledTaskTemplate[T2]:
        from ._trigger import make_decorator

        return self // make_decorator(middleware)

    def __floordiv__(self, decorator: TriggerFactoryDecorator[T, T2]) -> ScheduledTaskTemplate[T2]:
        n = ScheduledTaskTemplate(self._tf)
        n._tf_queue = self._tf_queue + (decorator,)
        return cast(ScheduledTaskTemplate[T2], n)

    def __rshift__(self, decorator: HandlerDecorator) -> ScheduledTaskTemplate[T]:
        n = ScheduledTaskTemplate[T](self._tf)
        n._handler_queue = self._handler_queue + (decorator,)
        return n

    def resolve_handler(self, task: Task[T, Any]) -> HandlerManager[T]:
        handler: HandlerManager[T] = task
        for decorator in self._handler_queue:
            handler = decorator(handler)
        return handler

    @property
    def tf(self) -> TriggerFactory[T]:
        tf = self._tf
        for decorator in self._tf_queue:
            tf = decorator(tf)
        return tf


class ScheduledTask(Protocol[T, R]):
    @property
    def scheduler(self) -> Scheduler: ...

    @property
    def task(self) -> Task[T, R]: ...

    @property
    def started(self) -> EventView: ...

    @property
    def shutting_down(self) -> EventView: ...

    @property
    def stopped(self) -> EventView: ...

    def shutdown(self) -> None: ...


@final
class _ScheduledTask(Generic[T, R]):
    def __init__(
        self,
        scheduler: Scheduler,
        task: Task[T, R],
        tf: TriggerFactory[T],
        handler: HandlerManager[T] | None = None,
    ):
        self.scheduler = scheduler
        self.task = task

        self.started = EventViewProxy()
        self.shutting_down = EventViewProxy()
        self.stopped = EventViewProxy()

        self._trigger_factory = tf
        self._handler = handler if handler else self.task

        self._trigger: Trigger[T] | None = None
        self._service_lifetime: ServiceLifetimeManager | None = None

    def __repr__(self):
        return f"ScheduledTask({self.name!r})"

    @property
    def name(self) -> str:
        return self.task.name

    @property
    def _lifetime(self) -> ServiceLifetimeManager:
        assert self._service_lifetime, "Task has not started yet"
        return self._service_lifetime

    @_lifetime.setter
    def _lifetime(self, value: ServiceLifetimeManager):
        assert self._service_lifetime is None, "Task has already started"
        self._service_lifetime = value
        self.started.resolve(value.started)
        self.shutting_down.resolve(value.shutting_down)
        self.stopped.resolve(value.stopped)

    def shutdown(self) -> None:
        self._lifetime.set_shutting_down()

    def create_runner(self) -> HostedServiceFunc:
        if self._trigger is None:
            trigger = self._trigger = self._trigger_factory(self)
        else:
            trigger = self._trigger

        async def run_task(service_lifetime: ServiceLifetimeManager):
            assert self._service_lifetime is None, f"{self!r} has already started"
            self._lifetime = service_lifetime
            async with trigger as t_events, self._handler as message_handler:
                service_lifetime.set_started()
                async for t_event in t_events:
                    await message_handler(t_event)
                logger.debug(f"{self!r} trigger is completed")
            logger.debug(f"{self!r} is done")

        return run_task


Trigger: TypeAlias = AbstractAsyncContextManager[AsyncIterator[T]]
TriggerFactory: TypeAlias = Callable[
    [ScheduledTask[T, Any]], AbstractAsyncContextManager[AsyncIterator[T]]  # Trigger[T]
]
TriggerFactoryMiddleware: TypeAlias = Callable[
    [
        AbstractAsyncContextManager[AsyncIterator[T]],  # Trigger[T] (source)
        ScheduledTask,
    ],
    AsyncIterable[T2],  # TODO AbstractAsyncContextManager[AsyncIterator[T2]]
]
TriggerFactoryDecorator: TypeAlias = Callable[
    [Callable[[ScheduledTask], AbstractAsyncContextManager[AsyncIterator[T]]]],  # TriggerFactory[T]
    Callable[[ScheduledTask], AbstractAsyncContextManager[AsyncIterator[T2]]],  # TriggerFactory[T2]
]


@final
class Scheduler:
    def __init__(self, name: str = "scheduler"):
        self.name = name

        self._started = EventViewProxy()
        self._shutting_down = EventViewProxy()
        self._stopped = EventViewProxy()

        self._scheduled: list[_ScheduledTask] = []
        self._service_lifetime: ServiceLifetimeManager | None = None

    def __repr__(self):
        return f"{self.__class__.__name__}(len={len(self._scheduled)})"

    @property
    def scheduled_tasks(self) -> Sequence[ScheduledTask]:
        return self._scheduled

    @property
    def started(self) -> EventView:
        return self._started

    @property
    def shutting_down(self) -> EventView:
        return self._shutting_down

    @property
    def stopped(self) -> EventView:
        return self._stopped

    @property
    def _lifetime(self) -> ServiceLifetimeManager:
        assert self._service_lifetime, "Scheduler has not started yet"
        return self._service_lifetime

    @_lifetime.setter
    def _lifetime(self, value: ServiceLifetimeManager):
        assert not self._service_lifetime, "Scheduler has already started"
        self._service_lifetime = value
        self._started.resolve(value.started)
        self._shutting_down.resolve(value.shutting_down)
        self._stopped.resolve(value.stopped)

    @property
    def host(self) -> Host:
        return self._lifetime.host

    def shutdown(self) -> None:  # TODO Reason
        self._lifetime.set_shutting_down()

    def schedule(self, t: TriggerFactory[T], /) -> Callable[[Task[T, R]], ScheduledTask[T, R]]:
        def _decorator(task: Task[T, R]) -> ScheduledTask[T, R]:
            tpl = ScheduledTaskTemplate.ensure(t)
            scheduled_task = _ScheduledTask(self, task, tpl.tf, tpl.resolve_handler(task))
            self._scheduled.append(scheduled_task)
            return scheduled_task

        return _decorator

    @staticmethod
    def as_task(*, name: str | None = None) -> Callable[[TaskHandler[T, R]], Task[T, R]]:
        """
        Create a task from the given callable (the same task can be scheduled multiple times).
        """
        return partial(Task, name=name)  # type: ignore[return-value]

    def task(
        self, tpl: TriggerFactory[T], /, *, name: str | None = None
    ) -> Callable[[TaskHandler[T, R]], ScheduledTask[T, R]]:
        """
        Schedule a task with the given trigger.
        """

        def _decorator(func: TaskHandler[T, R]) -> ScheduledTask[T, R]:
            t = self.as_task(name=name)(func)
            st = self.schedule(tpl)(t)
            return st

        return _decorator

    async def __call__(self, service_lifetime: ServiceLifetimeManager) -> None:
        self._lifetime = service_lifetime

        def start_task(task: _ScheduledTask):
            return service_lifetime.start_child_service(task.create_runner(), name=f"{self.name}/{task.task.name}")

        services = [start_task(t) for t in self._scheduled]
        if not services:
            logger.warning("Scheduler has no tasks to run")
            service_lifetime.set_started()
            return

        async def when_tasks_done():
            await wait_all(task_svc.stopped for task_svc in services)
            service_lifetime.set_shutting_down()

        async def when_tasks_started():
            await wait_all(task_svc.started for task_svc in services)
            # Consider the scheduler started only when all the tasks are started
            service_lifetime.set_started()

        async with create_task_group() as tg:
            start_task_soon(tg, when_tasks_done)
            start_task_soon(tg, when_tasks_started)
            await service_lifetime.shutting_down
            tg.cancel_scope.cancel()
