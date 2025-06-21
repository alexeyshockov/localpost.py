from __future__ import annotations

import dataclasses as dc
import functools
import inspect
import math
import random
import signal
import sys
import typing
from collections.abc import Awaitable, Callable, Coroutine, Generator, Iterable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from datetime import timedelta
from functools import wraps
from typing import (
    Any,
    Final,
    Generic,
    ParamSpec,
    Protocol,
    TypeAlias,
    TypedDict,
    Union,
    cast,
    final,
    overload,
)

import anyio
from anyio import CancelScope, WouldBlock, create_task_group, from_thread, to_thread
from anyio.abc import TaskGroup, TaskStatus
from anyio.lowlevel import checkpoint
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream, MemoryObjectStreamState
from typing_extensions import NotRequired, Self, TypeGuard, TypeVar

if sys.version_info >= (3, 11):
    from builtins import ExceptionGroup  # noqa
else:
    from exceptiongroup import ExceptionGroup

T = TypeVar("T", default=Any)
P = ParamSpec("P")
R = TypeVar("R")

# Sentinel object, to indicate that a value is not set (see https://python-patterns.guide/python/sentinel-object)
NOT_SET: Final = object()

TD_ZERO: Final = timedelta(0)

HANDLED_SIGNALS = (
    signal.SIGINT,  # Unix signal 2. Sent by Ctrl+C.
    signal.SIGTERM,  # Unix signal 15. Sent by `kill <pid>`.
)
if sys.platform == "win32":
    HANDLED_SIGNALS += (signal.SIGBREAK,)  # Windows signal 21. Sent by Ctrl+Break.


class _IgnoredTaskStatus(TaskStatus[Any]):
    def started(self, value: Any = None) -> None:
        pass


NO_OP_TS: Final = _IgnoredTaskStatus()


# PyCharm has a bug when calling a TypeVarTuple-parameterized function with 0 arguments,
# see https://youtrack.jetbrains.com/issue/PY-63820
def start_task_soon(tg: TaskGroup, func: Callable[[], Awaitable[Any]], name: object = None) -> None:
    tg.start_soon(func, name=name)  # type: ignore


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


@final
# Actually immutable, but frozen=True has noticeable performance impact
@dc.dataclass(slots=True, eq=True, unsafe_hash=True)
class Result(Generic[T]):
    value: T  # | NOT_SET
    error: BaseException  # | None

    @property
    def is_failure(self) -> bool:
        return self.error is not None

    @classmethod
    def ok(cls, value: T) -> Result[T]:
        return cls(value, None)  # type: ignore

    @classmethod
    def failure(cls, error: BaseException) -> Result[T]:
        return cls(cast(T, NOT_SET), error)


def get_callable_return_type(func: Callable[..., Any], /) -> type[Any]:
    try:
        desc = typing.get_type_hints(func, globalns=getattr(func, "__globals__", None))
    except (TypeError, NameError):
        try:
            desc = typing.get_type_hints(func.__call__, globalns=getattr(func.__call__, "__globals__", None))
        except (TypeError, NameError, AttributeError):
            return type(None)

    if ret_type := desc.get("return", None):
        return typing.get_origin(ret_type) or ret_type
    return type(None)


@overload
def is_async_callable(obj: Callable[P, Any], /) -> TypeGuard[Callable[P, Awaitable[Any]]]: ...


@overload
def is_async_callable(obj: Callable[P, Any], ret_t: type[R], /) -> TypeGuard[Callable[P, Awaitable[R]]]: ...


# See also: https://docs.python.org/3/library/inspect.html#inspect.markcoroutinefunction
def is_async_callable(obj: Callable[..., Any] | object, _=None, /) -> TypeGuard[Callable[..., Awaitable[Any]]]:
    while isinstance(obj, functools.partial):
        obj = obj.func
    if not callable(obj):
        return False
    return (
        inspect.iscoroutinefunction(obj)
        or inspect.iscoroutinefunction(obj.__call__)  # type: ignore
        or issubclass(get_callable_return_type(obj), Awaitable)
    )


def ensure_async_callable(
    func: Callable[P, Awaitable[T]] | Callable[P, T] | Callable[P, Awaitable[T] | T], /
) -> Callable[P, Awaitable[T]]:
    if is_async_callable(func):
        return func
    return functools.partial(to_thread.run_sync, func)  # type: ignore[return-value]


def ensure_sync_callable(
    func: Callable[P, Awaitable[T]] | Callable[P, T] | Callable[P, Awaitable[T] | T], /
) -> Callable[P, T]:
    if is_async_callable(func):
        return functools.partial(from_thread.run, func)
    return func  # type: ignore[return-value]


def def_full_name(func: Any, /) -> str:
    try:  # Function case
        module = func.__module__
        name = func.__qualname__
    except AttributeError:  # Object case
        object_type = type(func)
        module = object_type.__module__
        name = object_type.__qualname__
    return f"{module}.{name}" if module and module not in ("builtins", "__main__") else name


def ensure_td(value: timedelta | str, /) -> timedelta:
    if isinstance(value, timedelta):
        return value
    if isinstance(value, str):
        try:
            import pytimeparse2

            use_dateutil = pytimeparse2.HAS_RELITIVE_TIMEDELTA
            try:
                # Make sure to get timedelta, not relativedelta from dateutil
                pytimeparse2.HAS_RELITIVE_TIMEDELTA = False
                result = pytimeparse2.parse(value, as_timedelta=True)
                if result is None:
                    raise ValueError(f"Invalid time period: {value!r}")
                return result  # type: ignore[return-value]
            finally:
                pytimeparse2.HAS_RELITIVE_TIMEDELTA = use_dateutil
        except ImportError:
            raise ValueError("pytimeparse2 package is required to parse a time period string") from None
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


@final
@dc.dataclass(eq=False)
class MemorySendStream(Generic[T], MemoryObjectSendStream[T]):
    def send_or_drop(self, item: T) -> None:
        try:
            self.send_nowait(item)
        except WouldBlock:
            pass

    async def send_or_drop_async(self, item: T) -> None:
        await checkpoint()
        try:
            self.send_nowait(item)
        except WouldBlock:
            pass


class MemoryStream(Generic[T]):
    @staticmethod
    def create(max_buffer_size: float = 0) -> tuple[MemorySendStream[T], MemoryObjectReceiveStream[T]]:
        if max_buffer_size != math.inf and not isinstance(max_buffer_size, int):
            raise ValueError("max_buffer_size must be either an integer or math.inf")
        if max_buffer_size < 0:
            raise ValueError("max_buffer_size cannot be negative")

        state = MemoryObjectStreamState[T](max_buffer_size)
        return MemorySendStream(state), MemoryObjectReceiveStream(state)


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


class AnyEventView(Protocol):
    """Read-only view on an async event."""

    def is_set(self) -> bool: ...

    def wait(self) -> Awaitable[None]: ...


class EventView(AnyEventView, Protocol):
    """More convenient read-only view on an async event."""

    def __bool__(self) -> bool:
        return self.is_set()

    def __await__(self) -> Generator[Any, Any, None]:
        return self.wait().__await__()


@dc.dataclass(frozen=True, slots=True)
class Event(EventView):
    _source: anyio.Event

    def __init__(self) -> None:
        object.__setattr__(self, "_source", anyio.Event())

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(is_set={self._source.is_set()})"

    def set(self) -> None:
        self._source.set()

    def is_set(self) -> bool:
        return self._source.is_set()

    def wait(self) -> Awaitable[None]:
        return self._source.wait()


@dc.dataclass(slots=True)
class EventViewProxy(EventView):
    source: AnyEventView | None

    def __init__(self) -> None:
        self.source = None
        self._resolved = anyio.Event()

    def resolve(self, source: AnyEventView) -> None:
        self.source = source.source if isinstance(source, EventViewProxy) else source
        self._resolved.set()

    def is_set(self) -> bool:
        return False if self.source is None else self.source.is_set()

    async def wait(self) -> None:
        if self.source is None:
            await self._resolved.wait()
        assert self.source is not None
        await self.source.wait()


async def _cancel_when(trigger: AnyEventView | Callable[[], Awaitable[Any]], scope: CancelScope) -> None:
    await (trigger() if callable(trigger) else trigger.wait())
    scope.cancel()


def cancellable_from(*events: AnyEventView):
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
    async with create_task_group() as tg:
        for event in events:
            start_task_soon(tg, event.wait)


async def wait_any(*targets: EventView | Callable[[], Awaitable[Any]]) -> None:
    async with create_task_group() as tg:
        for t in targets:
            tg.start_soon(_cancel_when, t, tg.cancel_scope)
