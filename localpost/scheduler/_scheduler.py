from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import math
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, ExitStack
from typing import Any, Generic, Protocol, TypeAlias, TypeVar, Union, cast, final

from anyio import BrokenResourceError, WouldBlock, create_memory_object_stream, to_thread
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from localpost._utils import (
    Result,
    def_full_name,
    is_async_callable,
)
from localpost.flow import AsyncHandlerManager, FlowHandlerManager, HandlerDecorator, ensure_async_handler_manager
from localpost.hosting import (
    AbstractHost,
    ExposedService,
    ExposedServiceBase,
    HostedService,
    HostedServiceSet,
    ServiceLifetimeManager,
)

T = TypeVar("T")
T2 = TypeVar("T2")
R = TypeVar("R")
DecF = TypeVar("DecF", bound=Callable[..., Any])

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
    AbstractAsyncContextManager[Callable[[T], Awaitable[None]]],  # AsyncHandlerManager[T]
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
        if isinstance(tpl, cls):
            return tpl
        return cls(tpl)

    def __init__(self, tf: TriggerFactory[T]):
        self._tf = tf
        self._tf_queue: tuple[TriggerFactoryDecorator, ...] = ()
        self._handler_decorators: tuple[HandlerDecorator, ...] = ()

    # TriggerFactory[T]
    def __call__(self, *args, **kwargs) -> Trigger[T]:
        return self.tf(*args, **kwargs)

    def __truediv__(self, middleware: TriggerFactoryMiddleware[T, T2]) -> ScheduledTaskTemplate[T2]:
        from ._trigger import trigger_factory_middleware

        return self // trigger_factory_middleware(middleware)

    def __floordiv__(self, decorator: TriggerFactoryDecorator[T, T2]) -> ScheduledTaskTemplate[T2]:
        n = ScheduledTaskTemplate(self._tf)
        n._tf_queue = self._tf_queue + (decorator,)
        return cast(ScheduledTaskTemplate[T2], n)

    def __rshift__(self, decorator: HandlerDecorator) -> ScheduledTaskTemplate[T]:
        n = ScheduledTaskTemplate[T](self._tf)
        n._handler_decorators = self._handler_decorators + (decorator,)
        return n

    def resolve_handler(self, task: Task[T, Any]) -> AsyncHandlerManager[T]:
        if not self._handler_decorators:
            return task
        hm = FlowHandlerManager(lambda: task)
        for decorator in self._handler_decorators:
            hm = decorator(hm)
        return ensure_async_handler_manager(hm)

    @property
    def tf(self) -> TriggerFactory[T]:
        tf = self._tf
        for decorator in self._tf_queue:
            tf = decorator(tf)
        return tf


class ScheduledTask(ExposedService, Protocol[T, R]):
    @property
    def task(self) -> Task[T, R]: ...


@final
class _ScheduledTask(Generic[T, R], ExposedServiceBase):
    def __init__(self, task: Task[T, R], tf: TriggerFactory[T]):
        super().__init__()
        self.task = task
        self._trigger_factory = tf
        tpl = ScheduledTaskTemplate.ensure(tf)
        self._handler = tpl.resolve_handler(task)

    def __repr__(self):
        return f"ScheduledTask({self.name!r})"

    @property
    def name(self) -> str:
        return self.task.name

    async def __call__(self, service_lifetime: ServiceLifetimeManager) -> None:
        self._lifetime = service_lifetime
        trigger = self._trigger_factory(self)

        async with trigger as t_events, self._handler as message_handler:
            service_lifetime.set_started()
            async for t_event in t_events:
                await message_handler(t_event)
            logger.debug(f"{self!r} trigger is completed")
        logger.debug(f"{self!r} is done")


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


def scheduled_task(
    tf: TriggerFactory[T], /, *, name: str | None = None
) -> Callable[[TaskHandler[T, R] | Task[T, R]], ScheduledTask[T, R]]:
    """
    Schedule a task with the given trigger.
    """

    def _decorator(func: TaskHandler[T, R] | Task[T, R]) -> ScheduledTask[T, R]:
        t = func if isinstance(func, Task) else Task(func)
        if name:
            t.name = name
        return _ScheduledTask(t, tf)

    return _decorator


class Scheduler(AbstractHost):
    """
    Custom host type, tailored to schedule periodic tasks.

    If you need to combine multiple different services together (like a Kafka consumer and a scheduled task), use the
    generic Host instead.
    """

    def __init__(self, name: str = "scheduler"):
        super().__init__()
        self._name = name
        self._scheduled_tasks: list[ScheduledTask[Any, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    def _prepare_for_run(self) -> HostedService:
        return HostedService(HostedServiceSet(*self._scheduled_tasks))

    def task(
        self, tf: TriggerFactory[T], /, *, name: str | None = None
    ) -> Callable[[TaskHandler[T, R] | Task[T, R] | ScheduledTask[T, R]], ScheduledTask[T, R]]:
        """
        Schedule a task with the given trigger.
        """

        def _decorator(func: TaskHandler[T, R] | Task[T, R] | ScheduledTask[T, R]):
            if isinstance(func, _ScheduledTask):
                func = func.task
            st = scheduled_task(tf, name=name)(cast(TaskHandler[T, R] | Task[T, R], func))
            self._scheduled_tasks.append(st)
            return st

        return _decorator
