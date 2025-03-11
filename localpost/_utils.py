from __future__ import annotations

import dataclasses as dc
import functools
import inspect
import random
import signal
import sys
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from datetime import timedelta
from functools import wraps
from typing import (
    Any,
    Generic,
    ParamSpec,
    Protocol,
    TypeAlias,
    TypedDict,
    TypeVar,
    Union,
    cast,
    final,
)

import anyio
from anyio import CancelScope, Event, create_task_group
from anyio.abc import TaskGroup, TaskStatus
from typing_extensions import NotRequired, Self

if sys.version_info >= (3, 11):
    from builtins import ExceptionGroup  # noqa
else:
    from exceptiongroup import ExceptionGroup

T = TypeVar("T")
P = ParamSpec("P")
R = TypeVar("R")


# PyCharm has a bug when calling a TypeVarTuple-parameterized function with 0 arguments,
# see https://youtrack.jetbrains.com/issue/PY-63820
def start_task_soon(tg: TaskGroup, func: Callable[[], Awaitable[Any]], name: object = None) -> None:
    tg.start_soon(func, name=name)  # type: ignore


class _IgnoredTaskStatus(TaskStatus[Any]):
    def started(self, value: Any = None) -> None:
        pass


NO_OP_TS = _IgnoredTaskStatus()

TD_ZERO = timedelta(0)

HANDLED_SIGNALS = (
    signal.SIGINT,  # Unix signal 2. Sent by Ctrl+C.
    signal.SIGTERM,  # Unix signal 15. Sent by `kill <pid>`.
)
if sys.platform == "win32":  # pragma: py-not-win32
    HANDLED_SIGNALS += (signal.SIGBREAK,)  # Windows signal 21. Sent by Ctrl+Break.


def unwrap_exc(exc: Exception) -> Exception:
    if isinstance(exc, ExceptionGroup) and len(exc.exceptions) == 1:
        return unwrap_exc(exc.exceptions[0])
    return exc


class _SupportsClose(Protocol):
    def close(self) -> object: ...


class _SupportsAsyncClose(Protocol):
    async def aclose(self) -> object: ...


class ClosingContext(Generic[T], AbstractContextManager[T, None], AbstractAsyncContextManager[T, None]):
    def __init__(self, enter_result: T):
        self.enter_result = enter_result

    def __enter__(self) -> T:
        return self.enter_result

    async def __aenter__(self) -> T:
        return self.enter_result

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if hasattr(t := self.enter_result, "close"):
            cast(_SupportsClose, t).close()

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        t = self.enter_result
        if hasattr(t, "aclose"):
            await cast(_SupportsAsyncClose, t).aclose()
        elif hasattr(t, "close"):
            cast(_SupportsClose, t).close()


# Sentinel object, to indicate that a value is not set (see https://python-patterns.guide/python/sentinel-object)
NO_VALUE = object()


@final
# Actually immutable, but frozen=True has noticeable performance impact
@dc.dataclass(slots=True, eq=True)
class Result(Generic[T]):
    value: T
    error: BaseException | None = None

    @classmethod
    def ok(cls, value: T) -> Result[T]:
        return cls(value)

    @classmethod
    def failure(cls, error: BaseException) -> Result[T]:
        return cls(cast(T, NO_VALUE), error)


# Inspired by Starlette, see https://github.com/encode/starlette/issues/886
def is_async_callable(obj: Callable[..., Any], /) -> bool:
    while isinstance(obj, functools.partial):
        obj = obj.func
    return inspect.iscoroutinefunction(obj) or (callable(obj) and inspect.iscoroutinefunction(obj.__call__))  # type: ignore


def def_full_name(func: Any, /) -> str:
    try:  # Function case
        module = func.__module__
        name = func.__qualname__
    except AttributeError:  # Object case
        object_type = type(func)
        module = object_type.__module__
        name = object_type.__qualname__
    if module is None or module == "__builtin__" or module == "__main__":
        return name
    return module + "." + name


def ensure_td(value: timedelta | str, /) -> timedelta:
    if isinstance(value, timedelta):
        return value
    elif isinstance(value, str):
        try:
            import pytimeparse2

            use_dateutil = pytimeparse2.HAS_RELITIVE_TIMEDELTA

            try:
                # Make sure to get timedelta, not relativedelta from dateutil
                pytimeparse2.HAS_RELITIVE_TIMEDELTA = False
                return cast(timedelta, pytimeparse2.parse(value, as_timedelta=True))
            finally:
                pytimeparse2.HAS_RELITIVE_TIMEDELTA = use_dateutil
        except ImportError:
            raise ValueError("pytimeparse2 package is required to parse a time period string") from None
    else:
        raise ValueError(f"Invalid time period: {value!r}")


def td_str(td: timedelta, /) -> str:
    try:
        from humanize import precisedelta

        # 23 seconds or 0.24 seconds
        return precisedelta(td)
    except ImportError:
        # 0:00:23 or 0:00:00.240000
        return str(td)


# TODO Rename to DurationFactory
DelayFactory: TypeAlias = Union[
    Callable[[], timedelta], tuple[int, int], tuple[float, float], int, float, timedelta, None
]


@final
@dc.dataclass(frozen=True, slots=True)
class RandomDelay:  # https://en.wikipedia.org/wiki/Jitter#Types
    bounds: tuple[int, int] | tuple[float, float]

    def __repr__(self):
        return f"{self.__class__.__name__}{self.bounds!r}"

    def __call__(self) -> timedelta:
        a, b = self.bounds
        delay = random.randint(a, b) if isinstance(a, int) and isinstance(b, int) else random.uniform(a, b)
        return timedelta(seconds=delay)


@final
@dc.dataclass(frozen=True, slots=True)
class FixedDelay:
    value: timedelta

    @classmethod
    def create(cls, value: int | float | timedelta | None) -> Self:
        if value is None or value == 0:
            delay = TD_ZERO
        elif isinstance(value, (int, float)):
            delay = timedelta(seconds=value)
        elif isinstance(value, timedelta):
            delay = value
        else:
            raise ValueError(f"Invalid delay: {value!r}")

        return cls(delay)

    def __repr__(self):
        return f"{self.__class__.__name__}({td_str(self.value)!r})"

    def __call__(self) -> timedelta:
        return self.value


# TODO Rename to ensure_duration_factory()
def ensure_delay_factory(delay: DelayFactory, /) -> Callable[[], timedelta]:
    if isinstance(delay, tuple):  # tuple[int, int] | tuple[float, float]
        return RandomDelay(delay)  # type: ignore
    elif callable(delay):
        return delay
    else:  # int | float | timedelta | None
        return FixedDelay.create(delay)


# sleep(0) is used to return control to the event loop (in both Trio and AsyncIO)
def sleep(i: timedelta | int | float | None, /) -> Coroutine[Any, Any, None]:
    interval_sec: float = i.total_seconds() if isinstance(i, timedelta) else 0 if i is None else i
    return anyio.sleep(interval_sec)


class AsyncBackendConfig(TypedDict):
    backend: str
    backend_options: NotRequired[dict[str, Any]]


def choose_anyio_backend() -> AsyncBackendConfig:  # pragma: no cover
    try:
        import uvloop  # noqa  # type: ignore
    except ImportError:
        return {"backend": "asyncio"}
    else:
        return {"backend": "asyncio", "backend_options": {"use_uvloop": True}}


class EventView(Protocol):
    """
    Read-only view on an async event.
    """

    async def wait(self) -> None: ...

    def is_set(self) -> bool: ...


@dc.dataclass(slots=True)
class EventViewProxy(EventView):
    source: EventView | None

    def __init__(self) -> None:
        self.source = None
        self._resolved = Event()

    def resolve(self, source: EventView) -> None:
        self.source = source.source if isinstance(source, EventViewProxy) else source
        self._resolved.set()

    async def wait(self) -> None:
        if self.source is None:
            await self._resolved.wait()
        assert self.source is not None
        await self.source.wait()

    def is_set(self) -> bool:
        return self.source.is_set() if self.source else False


async def _cancel_when(trigger: EventView | Callable[[], Awaitable[Any]], scope: CancelScope) -> None:
    await (trigger() if callable(trigger) else trigger.wait())
    scope.cancel()


def cancellable_from(*events: EventView):
    def _decorator(func: Callable[P, Awaitable[Any]]) -> Callable[P, Awaitable[None]]:
        @wraps(func)
        async def _wrapper(*args, **kwargs):
            # await wait_any(lambda: func(*args, **kwargs), *[e.wait for e in events])
            async with create_task_group() as exec_tg:
                exec_scope = exec_tg.cancel_scope
                for e in events:
                    exec_tg.start_soon(_cancel_when, e, exec_scope)
                await func(*args, **kwargs)
                exec_scope.cancel()

        return _wrapper if events else func

    return _decorator


async def wait_all(events: Iterable[EventView]) -> None:
    assert events, "At least one event must be provided"

    async with create_task_group() as tg:
        for event in events:
            start_task_soon(tg, event.wait)


async def wait_any(*targets: EventView | Callable[[], Awaitable[Any]]) -> None:
    if not targets:
        return

    async with create_task_group() as tg:
        for t in targets:
            tg.start_soon(_cancel_when, t, tg.cancel_scope)
