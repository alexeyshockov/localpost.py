from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
    nullcontext,
)
from functools import partial, wraps
from typing import Any, Generic, ParamSpec, TypeAlias, TypeVar, Union, cast, overload

from anyio import from_thread, to_thread
from typing_extensions import Self

from localpost._utils import is_async_callable

# T = TypeVar("T")
# T2 = TypeVar("T2")
T = TypeVar("T", covariant=True)
T2 = TypeVar("T2", covariant=True)
P = ParamSpec("P")

Handler: TypeAlias = Callable[[T], Awaitable[object]]
SyncHandler: TypeAlias = Callable[[T], object]
HandlerManager: TypeAlias = AbstractAsyncContextManager[
    Callable[[T], Awaitable[object]]  # Handler[T]
]
HandlerMiddleware: TypeAlias = Callable[
    [Callable[[T], Awaitable[object]]],  # Handler[T] (next)
    AbstractAsyncContextManager[Callable[[T2], Awaitable[object]]],  # HandlerManager[T2]
]
HandlerDecorator: TypeAlias = Callable[[Callable[P, HandlerManager[T]]], Callable[P, HandlerManager[T2]]]

__all__ = [
    "Handler",
    "HandlerDecorator",
    "HandlerManager",
    "HandlerMiddleware",
    "SyncHandler",
    "handler",
    "handler_manager",
    "handler_manager_factory",
    "make_handler_decorator",
]

logger = logging.getLogger(__name__)


class _ThreadContext(AbstractAsyncContextManager):
    def __init__(self, cm: AbstractContextManager):
        self._source = cm

    async def __aenter__(self):
        return await to_thread.run_sync(self._source.__enter__)  # type: ignore

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await to_thread.run_sync(self._source.__exit__, exc_type, exc_value, traceback)


# Rarely used, only when use have a custom flow that resolves a handler manager with some parameters.
def handler_manager_factory(
    func: Union[
        Callable[P, HandlerManager[T]],
        Callable[P, AbstractContextManager[Handler[T]]],
        Callable[P, AsyncGenerator[Handler[T]]],
        Callable[P, Generator[Handler[T]]],
    ],
) -> Callable[P, HandlerManager[T]]:
    return cast(Callable[P, HandlerManager[T]], _HandlerManagerFactory.ensure(func))


@overload
def handler_manager(
    func: Union[
        Callable[[], AsyncGenerator[Handler[T]]],
        Callable[[], Generator[Handler[T]]],
    ],
) -> AbstractAsyncContextManager[Handler[T]]: ...


@overload
def handler_manager(
    func: Union[
        Callable[[], AsyncGenerator[SyncHandler[T]]],
        Callable[[], Generator[SyncHandler[T]]],
    ],
) -> AbstractAsyncContextManager[SyncHandler[T]]: ...


def handler_manager(func: Callable[[], AsyncGenerator[Any] | Generator[Any]]) -> AbstractAsyncContextManager[Any]:
    if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
        return _HandlerManagerFactory.ensure(func)
    raise ValueError("Invalid handler type")


def handler(func: Handler[T] | SyncHandler[T]) -> HandlerManager[T]:
    if is_async_callable(func):
        return _HandlerManagerFactory(lambda: nullcontext(func))  # type: ignore
    return _HandlerManagerFactory(lambda: nullcontext(partial(to_thread.run_sync, func)))  # type: ignore


def sync_handler(
    func: Handler[T] | SyncHandler[T],
) -> AbstractAsyncContextManager[
    Callable[[T], object]  # SyncHandler[T]
]:
    if is_async_callable(func):
        return _HandlerManagerFactory(lambda: nullcontext(partial(from_thread.run, func)))
    return _HandlerManagerFactory(lambda: nullcontext(func))


@asynccontextmanager
async def _composite_handler_manager(hm: HandlerManager[T], middlewares: Sequence[HandlerMiddleware]):
    async with AsyncExitStack() as es:
        for middleware in middlewares:
            message_handler = await es.enter_async_context(hm)
            hm = middleware(message_handler)
        message_handler = await es.enter_async_context(hm)
        yield message_handler


class _HandlerManagerFactory(
    Generic[T],
    AbstractAsyncContextManager[Handler[T]],  # HandlerManager[T] by itself (assuming the factory has no parameters)
):
    @classmethod
    def ensure(
        cls,
        func: Union[
            Callable[P, AsyncGenerator[Handler[T]]],
            Callable[P, Generator[Handler[T]]],
            Callable[P, AbstractContextManager[Handler[T]]],
            Callable[P, HandlerManager[T]],
        ],
    ) -> Self:
        @wraps(func)
        def wrapper(*args, **kwargs):
            if inspect.isgeneratorfunction(func):
                # Otherwise timeouts won't work...
                return _ThreadContext(contextmanager(func)(*args, **kwargs))
            elif inspect.isasyncgenfunction(func):
                return asynccontextmanager(func)(*args, **kwargs)
            else:
                cm = func(*args, **kwargs)
                if isinstance(cm, AbstractAsyncContextManager):
                    return cm
                elif isinstance(cm, AbstractContextManager):
                    return _ThreadContext(cm)
                else:
                    raise ValueError(f"Invalid handler type: {type(cm)}")

        return cls(wrapper)

    def __init__(self, factory: Callable[..., AbstractAsyncContextManager[Handler[T] | SyncHandler[T]]]):
        self._factory = factory
        self._middlewares: tuple[HandlerMiddleware, ...] = ()
        self._handler = None

    def __call__(self, *args, **kwargs) -> HandlerManager[T]:
        hm = self._factory(*args, **kwargs)
        return _composite_handler_manager(hm, self._middlewares)

    def use(self, middleware: HandlerMiddleware) -> _HandlerManagerFactory:
        n = _HandlerManagerFactory(self._factory)
        n._middlewares = self._middlewares + (middleware,)
        return n

    async def __aenter__(self):
        assert not self._handler
        self._handler = self()
        return await self._handler.__aenter__()

    async def __aexit__(self, exc_type, exc_value, traceback):
        assert self._handler
        return await self._handler.__aexit__(exc_type, exc_value, traceback)


def make_handler_decorator(m: HandlerMiddleware[T, T2], /) -> HandlerDecorator[P, T, T2]:
    def _decorator(next_h: Callable[P, HandlerManager[T]]) -> Callable[P, HandlerManager[T2]]:
        # async def handler(*args, **kwargs):
        #     async with next_h(*args, **kwargs) as next_mh, middleware(next_mh) as mh:
        #         yield mh
        # return handler
        return (next_h if isinstance(next_h, _HandlerManagerFactory) else _HandlerManagerFactory(next_h)).use(m)

    return _decorator
