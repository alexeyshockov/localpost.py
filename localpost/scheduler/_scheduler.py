from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import math
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, ExitStack
from typing import Any, Generic, ParamSpec, Protocol, TypeAlias, TypeVar, Union, cast, final

from anyio import BrokenResourceError, WouldBlock, create_memory_object_stream, create_task_group, to_thread
from anyio.from_thread import BlockingPortal
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
from localpost.flow import Handler, HandlerDecorator, HandlerManager
from localpost.hosting import Host, HostedServiceFunc, ServiceLifetimeManager

T = TypeVar("T")
T2 = TypeVar("T2")
ResT = TypeVar("ResT")

P = ParamSpec("P")

TaskHandler: TypeAlias = Union[
    Callable[[], Awaitable[ResT]],
    Callable[[T], Awaitable[ResT]],
    Callable[[], ResT],
    Callable[[T], ResT],
]

logger = logging.getLogger("localpost.scheduler")


@final
@dc.dataclass()
class Task(
    Generic[T, ResT],
    AbstractAsyncContextManager[Handler[T]],  # HandlerManager[T]
):
    name: str
    target: TaskHandler[T, ResT]
    event_aware: bool

    def __init__(self, target: TaskHandler[T, ResT], /, *, name: str | None = None):
        self.name = name or def_full_name(target)
        self.target = target
        e_aware = self.event_aware = len(inspect.signature(target).parameters) > 0

        def e_handler() -> Callable[[T], Awaitable[ResT]]:
            if is_async_callable(target):
                return target if e_aware else lambda _: target()  # type: ignore
            elif e_aware:
                return lambda e: to_thread.run_sync(target, e)  # type: ignore
            return lambda _: to_thread.run_sync(target)  # type: ignore

        self._handle = e_handler()

        self._cm = ExitStack()
        self._subscribers: list[MemoryObjectSendStream[Result[ResT]]] = []
        self._users = 0

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.name!r}>"

    def subscribe(self, buffer_max_size: float = math.inf) -> MemoryObjectReceiveStream[Result[ResT]]:
        # By default, a stream is created with a buffer size of 0, which means that any write will be blocked until
        # there is a free reader. We do not want to block the task execution flow in any way, so:
        #  - the buffer is unbounded by default
        #  - if the buffer is full, the result is dropped (see publish method below)
        send_stream, receive_stream = create_memory_object_stream[Result[ResT]](buffer_max_size)
        self._subscribers.append(self._cm.enter_context(send_stream))
        return receive_stream

    def _publish_result(self, result: Result[ResT]) -> None:
        for i, subscriber in enumerate(self._subscribers):
            try:
                subscriber.send_nowait(result)
            except BrokenResourceError:  # Subscriber is not active anymore
                del self._subscribers[i]
            except WouldBlock:
                logger.error("Subscriber's buffer is full, dropping the result")

    async def __call__(self, event: T):  # MessageHandler[T]
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
        self._tf_stack: tuple[TriggerFactoryDecorator, ...] = ()
        self._handler_stack: tuple[HandlerDecorator, ...] = ()

    # TriggerFactory[T]
    def __call__(self, *args, **kwargs) -> Trigger[T]:
        return self.tf(*args, **kwargs)

    def __truediv__(self, middleware: TriggerFactoryMiddleware[T, T2]) -> ScheduledTaskTemplate[T2]:
        from ._trigger import make_decorator

        return self // make_decorator(middleware)

    def __floordiv__(self, decorator: TriggerFactoryDecorator[T, T2]) -> ScheduledTaskTemplate[T2]:
        n = ScheduledTaskTemplate(self._tf)
        n._tf_stack = self._tf_stack + (decorator,)
        return cast(ScheduledTaskTemplate[T2], n)

    def __rshift__(self, decorator: HandlerDecorator[P, T, T2]) -> ScheduledTaskTemplate[T2]:
        n = ScheduledTaskTemplate(self._tf)
        n._handler_stack = self._handler_stack + (decorator,)
        return cast(ScheduledTaskTemplate[T2], n)

    def resolve_handler(self, task: Task[T, Any]) -> HandlerManager[T]:
        def from_task(*_):
            return task

        handler: Callable[..., HandlerManager[T]] = from_task
        for decorator in self._handler_stack:
            handler = decorator(handler)
        return handler()

    @property
    def tf(self) -> TriggerFactory[T]:
        if not self._tf_stack:
            return self._tf
        tf = self._tf
        for decorator in self._tf_stack:
            tf = decorator(tf)
        return tf


class ScheduledTask(Protocol[T, ResT]):
    @property
    def scheduler(self) -> Scheduler: ...

    @property
    def task(self) -> Task[T, ResT]: ...

    @property
    def started(self) -> EventView: ...

    @property
    def shutting_down(self) -> EventView: ...

    @property
    def stopped(self) -> EventView: ...

    def shutdown(self) -> None: ...


@final
class _ScheduledTask(Generic[T, ResT]):
    def __init__(
        self,
        scheduler: Scheduler,
        task: Task[T, ResT],
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
        return f"<{self.__class__.__name__} {self.task.name!r}>"

    @property
    def _lifetime(self) -> ServiceLifetimeManager:
        if self._service_lifetime:
            return self._service_lifetime
        raise RuntimeError("Task has not been started")

    @_lifetime.setter
    def _lifetime(self, value: ServiceLifetimeManager):
        assert self._service_lifetime is None, "Task has already been started"
        self._service_lifetime = value
        self.started.resolve(value.started)
        self.shutting_down.resolve(value.shutting_down)
        self.stopped.resolve(value.stopped)

    def shutdown(self) -> None:
        self._lifetime.set_shutting_down()

    def create_runner(self) -> HostedServiceFunc:
        name = f"ScheduledTask({self.task.name!r})"
        assert not self._trigger, f"{name} runner has already been created"
        self._trigger = trigger = self._trigger_factory(self)

        async def run_task(service_lifetime: ServiceLifetimeManager):
            assert self._service_lifetime is None, f"{name} has already been started"
            self._lifetime = service_lifetime
            async with trigger as t_events, self._handler as message_handler:
                service_lifetime.set_started()
                async for t_event in t_events:
                    await message_handler(t_event)
                logger.debug(f"{name} trigger is completed")
            logger.debug(f"{name} is done")

        return run_task


Trigger: TypeAlias = AbstractAsyncContextManager[AsyncIterator[T]]
TriggerFactory: TypeAlias = Callable[
    [ScheduledTask[T, object]], AbstractAsyncContextManager[AsyncIterator[T]]  # Trigger[T]
]
TriggerFactoryMiddleware: TypeAlias = Callable[
    [
        AbstractAsyncContextManager[AsyncIterator[T]],  # Trigger[T] (source)
        ScheduledTask,
    ],
    AsyncIterable[T2],  # TODO AbstractAsyncContextManager[AsyncIterator[T]]
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

        self._tasks: list[Task] = []
        self._scheduled: list[_ScheduledTask] = []
        self._service_lifetime: ServiceLifetimeManager | None = None

    def __repr__(self):
        return f"<{self.__class__.__name__} with {len(self._scheduled)} scheduled tasks>"

    @property
    def scheduled_tasks(self) -> Sequence[ScheduledTask]:
        tasks: Sequence[ScheduledTask] = self._scheduled
        return tasks

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
        if self._service_lifetime:
            return self._service_lifetime
        raise RuntimeError("Scheduler has not been started")

    @_lifetime.setter
    def _lifetime(self, value: ServiceLifetimeManager):
        assert self._service_lifetime is None, "Scheduler has already been started"
        self._service_lifetime = value
        self._started.resolve(value.started)
        self._shutting_down.resolve(value.shutting_down)
        self._stopped.resolve(value.stopped)

    @property
    def host_portal(self) -> BlockingPortal:
        return self._lifetime.host.portal

    def shutdown(self) -> None:
        self._lifetime.set_shutting_down()

    def schedule(self, t: TriggerFactory[T], /):
        def _decorator(task: Task[T, ResT]) -> ScheduledTask[T, ResT]:
            tpl = ScheduledTaskTemplate.ensure(t)
            scheduled_task = _ScheduledTask(self, task, tpl.tf, tpl.resolve_handler(task))
            self._scheduled.append(scheduled_task)
            return scheduled_task

        return _decorator

    def as_task(self, *, name: str | None = None):
        def _decorator(func: TaskHandler[T, ResT]) -> Task[T, ResT]:
            t = Task(func, name=name)
            self._tasks.append(t)
            return t

        return _decorator

    def task(self, tpl: TriggerFactory[T], /, *, name: str | None = None):
        def _decorator(func: TaskHandler[T, ResT]) -> ScheduledTask[T, ResT]:
            t = self.as_task(name=name)(func)
            st = self.schedule(tpl)(t)
            return st

        return _decorator

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        assert self._service_lifetime is None, "Scheduler has already been started"
        self._lifetime = service_lifetime

        def start_task(task: _ScheduledTask):
            return service_lifetime.start_child_service(task.create_runner(), name=f"{self.name}/{task.task.name}")

        services = [start_task(t) for t in self._scheduled]
        if not services:
            logger.warning("Scheduler has no tasks")
            service_lifetime.set_started()
            return

        async def when_tasks_done():
            await wait_all(task_svc.stopped for task_svc in services)
            service_lifetime.set_shutting_down()

        async def when_tasks_started():
            await wait_all(task_svc.started for task_svc in services)
            # Consider the scheduler started only when all tasks are started
            service_lifetime.set_started()

        async with create_task_group() as tg:
            start_task_soon(tg, when_tasks_done)
            start_task_soon(tg, when_tasks_started)
            await service_lifetime.shutting_down.wait()
            tg.cancel_scope.cancel()


async def aserve(scheduler: Scheduler) -> AbstractAsyncContextManager[Host]:
    host = Host(scheduler, name=f"{scheduler.name}_host")
    return host.aserve()


def serve(scheduler: Scheduler) -> AbstractContextManager[Host]:
    host = Host(scheduler, name=f"{scheduler.name}_host")
    return host.serve()
