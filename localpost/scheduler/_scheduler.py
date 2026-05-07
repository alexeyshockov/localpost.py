from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, ExitStack
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, cast, final

from anyio import BrokenResourceError, WouldBlock, to_thread
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from localpost._utils import (
    Event,
    EventView,
    MemoryStream,
    Result,
    def_full_name,
    is_async_callable,
    wait_all,
)

if TYPE_CHECKING:
    from localpost.hosting import ServiceLifetime

# We keep module-level TypeVars (and ``Generic[...]``) for the entire module
# rather than mixing PEP 695 ``class Foo[T]:`` with these TypeVars. ty's
# checker treats them as distinct identifiers — a class declared with
# ``Foo[T]`` does NOT see the module-level ``T`` and ends up with
# ``T@Foo`` vs ``T@module``, which breaks variance inside nested
# generic functions like ``scheduled_task → _decorator``. Once ty supports
# better outer-scope capture, the whole module can move to PEP 695.
T = TypeVar("T")
T2 = TypeVar("T2")
R = TypeVar("R")

type TaskHandler[T, R] = Callable[[T], Awaitable[R]] | Callable[[], Awaitable[R]] | Callable[[T], R] | Callable[[], R]

logger = logging.getLogger("localpost.scheduler")


@final
@dc.dataclass()
class Task(
    Generic[T, R],  # noqa: UP046 — see TypeVar comment above
    AbstractAsyncContextManager[Callable[[T], Awaitable[None]]],
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
            result: Result[R] = Result.ok(await self._handle(event))
        except Exception as e:
            logger.exception("Task %r raised", self.name)
            result = Result.failure(e)
        self._publish_result(result)

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
class ScheduledTaskTemplate[T]:
    @classmethod
    def ensure(cls, tpl: TriggerFactory[T]) -> ScheduledTaskTemplate[T]:
        if isinstance(tpl, cls):
            return tpl
        return cls(tpl)

    def __init__(self, tf: TriggerFactory[T]):
        self._tf = tf
        self._tf_queue: tuple[TriggerFactoryDecorator, ...] = ()

    # TriggerFactory[T]
    def __call__(self, *args, **kwargs) -> Trigger[T]:
        return self.tf(*args, **kwargs)

    def __truediv__(self, middleware: TriggerFactoryMiddleware[T, T2]) -> ScheduledTaskTemplate[T2]:
        from ._trigger import trigger_factory_middleware  # noqa: PLC0415

        return self // trigger_factory_middleware(middleware)

    def __floordiv__(self, decorator: TriggerFactoryDecorator[T, T2]) -> ScheduledTaskTemplate[T2]:
        n = ScheduledTaskTemplate(self._tf)
        n._tf_queue = self._tf_queue + (decorator,)
        return cast(ScheduledTaskTemplate[T2], n)

    @property
    def tf(self) -> TriggerFactory[T]:
        tf = self._tf
        for decorator in self._tf_queue:
            tf = decorator(tf)
        return tf


class ScheduledTask(Protocol[T, R]):
    @property
    def shutting_down(self) -> EventView: ...

    @property
    def task(self) -> Task[T, R]: ...


@final
class _ScheduledTask[T, R]:
    def __init__(self, task: Task[T, R], tf: TriggerFactory[T]):
        self.task = task
        self._trigger_factory = tf
        self._shutting_down: EventView = Event()  # Placeholder, resolved in run()

    def __repr__(self):
        return f"ScheduledTask({self.name!r})"

    @property
    def shutting_down(self) -> EventView:
        return self._shutting_down

    @property
    def name(self) -> str:
        return self.task.name

    async def run(self, shutting_down: EventView) -> None:
        self._shutting_down = shutting_down
        trigger = self._trigger_factory(self)

        async with trigger as t_events, self.task as message_handler:
            async for t_event in t_events:
                await message_handler(t_event)
            logger.debug("%r trigger is completed", self)
        logger.debug("%r is done", self)

    async def __call__(self, lt: ServiceLifetime) -> None:
        """ServiceF entry point: drives the trigger lifecycle from a hosting lifetime."""
        lt.set_started()
        await self.run(lt.shutting_down)


type Trigger[T] = AbstractAsyncContextManager[AsyncIterator[T]]
type TriggerFactory[T] = Callable[[ScheduledTask[T, Any]], Trigger[T]]
# Middleware is written as an async generator function: it consumes the source ``Trigger[T]`` (a context
# manager so it can install/clean up a task group) and yields ``T2`` items. ``trigger_factory_middleware``
# wraps the returned async generator into a ``Trigger[T2]`` via ``maybe_closing``.
type TriggerFactoryMiddleware[T, T2] = Callable[[Trigger[T], ScheduledTask], AsyncIterator[T2]]
type TriggerFactoryDecorator[T, T2] = Callable[[TriggerFactory[T]], TriggerFactory[T2]]


def scheduled_task(  # noqa: UP047 — see TypeVar comment above
    tf: TriggerFactory[T], /, *, name: str | None = None
) -> Callable[[TaskHandler[T, R] | Task[T, R]], _ScheduledTask[T, R]]:
    """
    Schedule a task with the given trigger.
    """

    def _decorator(func: TaskHandler[T, R] | Task[T, R]) -> _ScheduledTask[T, R]:
        t = func if isinstance(func, Task) else Task(func)
        if name:
            t.name = name
        return _ScheduledTask(t, tf)

    return _decorator


class Scheduler:
    """
    Manages a collection of periodic tasks. ``Scheduler`` is itself a ``ServiceF`` —
    pass it to ``localpost.hosting.run_app(...)`` or ``serve(...)`` to run.
    """

    def __init__(self, name: str = "scheduler"):
        self._name = name
        self._scheduled_tasks: list[_ScheduledTask[Any, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    def task(
        self, tf: TriggerFactory[T], /, *, name: str | None = None
    ) -> Callable[[TaskHandler[T, R] | Task[T, R] | _ScheduledTask[T, R]], _ScheduledTask[T, R]]:
        """
        Schedule a task with the given trigger.
        """

        def _decorator(func: TaskHandler[T, R] | Task[T, R] | _ScheduledTask[T, R]):
            if isinstance(func, _ScheduledTask):
                func = func.task
            st = scheduled_task(tf, name=name)(cast(TaskHandler[T, R] | Task[T, R], func))
            self._scheduled_tasks.append(st)
            return st

        return _decorator

    async def __call__(self, lt: ServiceLifetime) -> None:
        """ServiceF entry point: starts every registered task as a child service."""
        children = [lt.start(st) for st in self._scheduled_tasks]
        lt.set_started()
        if not children:
            await lt.shutting_down.wait()
            return

        async def propagate_shutdown() -> None:
            await lt.shutting_down.wait()
            for c in children:
                c.shutdown()

        lt.tg.start_soon(propagate_shutdown)
        await wait_all(c.stopped for c in children)
