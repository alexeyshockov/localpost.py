from __future__ import annotations

import inspect
import logging
import math
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Iterable
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
    nullcontext,
)
from functools import partial, wraps
from typing import Any, ParamSpec, TypeAlias, TypeVar, Union, cast

from anyio import ClosedResourceError, EndOfStream, create_task_group, from_thread, to_thread
from anyio.abc import ObjectReceiveStream
from typing_extensions import Self

from localpost._utils import ensure_async_callable, is_async_callable

T = TypeVar("T")
T2 = TypeVar("T2")
P = ParamSpec("P")
R = TypeVar("R", Awaitable[None], None)
R2 = TypeVar("R2", Awaitable[None], None)

Handler: TypeAlias = Callable[[T], Awaitable[None]]
HandlerManager: TypeAlias = AbstractAsyncContextManager[
    Callable[[T], Awaitable[None]]  # Handler[T]
]
SyncHandler: TypeAlias = Callable[[T], None]
SyncHandlerManager: TypeAlias = AbstractAsyncContextManager[
    Callable[[T], None]  # SyncHandler[T]
]

AnyHandler: TypeAlias = Union[
    Callable[[T], Awaitable[None]],
    Callable[[T], None],
]
AnyHandlerSource: TypeAlias = Callable[[], AsyncGenerator[Handler[T]] | Generator[Handler[T]]]
AnyHandlerManager: TypeAlias = Union[
    AbstractAsyncContextManager[Callable[[T], Awaitable[None]]],  # HandlerManager[T]
    AbstractAsyncContextManager[Callable[[T], None]],  # SyncHandlerManager[T]
]

HandlerWrapper: TypeAlias = Callable[[T], AbstractContextManager[Any]]
HandlerMiddleware: TypeAlias = Union[
    Callable[
        [Callable[[T], R]],  # next handler (sync or async)
        AbstractAsyncContextManager[Callable[[T2], R2]],  # Handler manager
    ],
    Callable[[Callable[[T], R]], AsyncGenerator[Callable[[T2], R2], None]],
]
_HandlerMiddleware: TypeAlias = Callable[
    [Callable[[T], R]],
    AbstractAsyncContextManager[Callable[[T2], R2]],
]
HandlerDecorator: TypeAlias = Callable[
    [AbstractAsyncContextManager[Callable[[T], R]]],  # HandlerManager[T] or SyncHandlerManager[T]
    AbstractAsyncContextManager[Callable[[T2], R2]],
]

logger = logging.getLogger("localpost.flow")


class _ThreadContext(AbstractAsyncContextManager):
    def __init__(self, cm: AbstractContextManager):
        self._source = cm

    async def __aenter__(self):
        return await to_thread.run_sync(self._source.__enter__)  # type: ignore

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await to_thread.run_sync(self._source.__exit__, exc_type, exc_value, traceback)


# Too complex case, maybe for the future
# def handler_manager_factory(
#     func: Union[
#         Callable[P, HandlerManager[T]],
#         Callable[P, AbstractContextManager[Handler[T]]],
#         Callable[P, AsyncGenerator[Handler[T]]],
#         Callable[P, Generator[Handler[T]]],
#     ],
# ) -> Callable[P, HandlerManager[T]]:
#     return cast(Callable[P, HandlerManager[T]], _HandlerManagerFactory.ensure(func))


def handler(func: AnyHandler[T] | AnyHandlerSource[T]) -> HandlerManager[T]:
    """
    Wrap a function, so it can be used as a flow handler.
    """
    if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
        return _HandlerManagerFactory.ensure(func)
    func = cast(AnyHandler[T], func)
    if is_async_callable(func):
        return _HandlerManagerFactory(lambda: nullcontext(func))
    return _HandlerManagerFactory(lambda: nullcontext(partial(to_thread.run_sync, func)))  # type: ignore[arg-type]


def ensure_async_handler(source: AnyHandlerManager[T]) -> HandlerManager[T]:
    @asynccontextmanager
    async def _async_handler_manager():
        async with source as h:
            yield ensure_async_callable(h)

    return _HandlerManagerFactory(cast(Callable[[], HandlerManager[T]], _async_handler_manager))


def sync_handler(func: AnyHandler[T] | AnyHandlerSource[T]) -> SyncHandlerManager[T]:
    """
    Wrap a function, so it can be used as a sync flow handler.
    """
    if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
        return _HandlerManagerFactory.ensure(func)
    func = cast(AnyHandler[T], func)
    if is_async_callable(func):
        return _HandlerManagerFactory(lambda: nullcontext(partial(from_thread.run, func)))  # type: ignore[arg-type]
    return _HandlerManagerFactory(lambda: nullcontext(func))


def ensure_sync_handler(source: AnyHandlerManager[T]) -> SyncHandlerManager[T]:
    @asynccontextmanager
    async def _sync_handler_manager():
        async with source as h:
            if is_async_callable(h):
                yield partial(from_thread.run, h)
            else:
                yield h

    return _HandlerManagerFactory(cast(Callable[[], SyncHandlerManager[T]], _sync_handler_manager))


@asynccontextmanager
async def _composite_handler_manager(
    hm: AbstractAsyncContextManager[Callable[[Any], Any]], middlewares: Iterable[_HandlerMiddleware]
):
    async with AsyncExitStack() as es:
        for middleware in middlewares:
            h = await es.enter_async_context(hm)
            hm = middleware(h)
        h = await es.enter_async_context(hm)
        yield h


class _HandlerManagerFactory(
    # Handler manager by itself (assuming the factory has no parameters)
    AbstractAsyncContextManager[Callable[[Any], Any]],
):
    _factory: Callable[..., AbstractAsyncContextManager[Callable[[Any], Any]]]
    _middlewares: tuple[_HandlerMiddleware, ...]

    _handler: AbstractAsyncContextManager[Callable[[Any], Any]] | None

    @classmethod
    def ensure(cls, func: Callable[..., Any]) -> Self:
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

    def __init__(self, factory):
        if isinstance(factory, AbstractAsyncContextManager):
            self._factory = lambda *_, **__: factory
        else:
            self._factory = factory
        self._handler = None
        self._middlewares = ()

    def __call__(self, *args, **kwargs) -> AbstractAsyncContextManager[Callable[[Any], Any]]:
        hm = self._factory(*args, **kwargs)
        if not self._middlewares:
            return hm
        return _composite_handler_manager(hm, self._middlewares)

    def use(self, m: HandlerMiddleware) -> _HandlerManagerFactory:
        m = cast(_HandlerMiddleware, asynccontextmanager(m) if inspect.isasyncgenfunction(m) else m)
        f = _HandlerManagerFactory(self._factory)
        f._middlewares = self._middlewares + (m,)
        return f

    async def __aenter__(self):
        assert not self._handler
        self._handler = self()
        return await self._handler.__aenter__()

    async def __aexit__(self, exc_type, exc_value, traceback):
        assert self._handler
        try:
            return await self._handler.__aexit__(exc_type, exc_value, traceback)
        finally:
            self._handler = None


def handler_middleware(m: HandlerMiddleware[T, R, T2, R2], /) -> HandlerDecorator[T, R, T2, R2]:
    def decorator(next_h):
        return (next_h if isinstance(next_h, _HandlerManagerFactory) else _HandlerManagerFactory(next_h)).use(m)

    return decorator


def handler_wrapper(
    w: Callable[[T], AbstractContextManager[Any]] | Callable[[T], Generator[Any, None, Any]], /
) -> HandlerDecorator[T, R, T, R]:
    if inspect.isgeneratorfunction(w):
        w = contextmanager(w)
    w = cast(Callable[[T], AbstractContextManager[Any]], w)

    @handler_middleware
    async def middleware(next_h: Callable[[T], R]) -> AsyncGenerator[Callable[[T], Any], None]:
        if is_async_callable(next_h):

            async def _handle_async(item: T):
                with w(item):
                    await next_h(item)

            yield _handle_async
        else:

            def _handle_sync(item: T):
                with w(item):
                    next_h(item)

            yield _handle_sync

    return middleware


@asynccontextmanager
async def stream_consumer(
    source: ObjectReceiveStream[T],
    h: Handler[T],
    concurrency: int = 1,
    process_leftovers: bool = True,
):
    async def consume(handle_soon: bool):
        while True:
            try:
                item = await source.receive()
            except EndOfStream:
                logger.debug("Source stream has been completed, no more items to consume")
                break
            except ClosedResourceError:
                logger.debug("Receiver has been closed (according to consumer's process_leftovers setting)")
                break

            if handle_soon:  # Infinite concurrency, just spawn a new task for each item
                tg.start_soon(h, item)
            else:
                await h(item)

    async with source, create_task_group() as tg:
        if math.isinf(concurrency):
            tg.start_soon(consume, True)
        else:
            for _ in range(concurrency):
                tg.start_soon(consume, False)

        yield

        if process_leftovers:
            # Process all the remaining items (until the source stream is completed)
            pass
        else:
            # Immediately stop consuming (close the receiver) and ignore the remaining items
            await source.aclose()
