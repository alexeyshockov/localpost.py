from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import math
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
    asynccontextmanager,
    nullcontext,
)
from typing import Any, Generic, ParamSpec, TypeAlias, cast, final

from anyio import ClosedResourceError, EndOfStream, create_task_group, to_thread
from anyio.abc import ObjectReceiveStream
from typing_extensions import TypeVar

from localpost._utils import ensure_async_callable, ensure_sync_callable, is_async_callable

T = TypeVar("T", default=Any)  # Default to Any to allow for more flexible typing
T2 = TypeVar("T2", default=Any)
P = ParamSpec("P")
R = TypeVar("R", Awaitable[None], None)
R2 = TypeVar("R2", Awaitable[None], None)

AsyncHandler: TypeAlias = Callable[[T], Awaitable[None]]
AsyncHandlerManager: TypeAlias = AbstractAsyncContextManager[
    Callable[[T], Awaitable[None]]  # Handler[T]
]
SyncHandler: TypeAlias = Callable[[T], None]
SyncHandlerManager: TypeAlias = AbstractAsyncContextManager[
    Callable[[T], None]  # SyncHandler[T]
]

AnyHandler: TypeAlias = Callable[[T], Awaitable[None] | None]
# AnyHandlerSource: TypeAlias = Callable[[], AsyncGenerator[Handler[T]] | Generator[Handler[T]]]
# AnyHandlerManager: TypeAlias = Union[
#     AbstractAsyncContextManager[Callable[[T], Awaitable[None]]],  # HandlerManager[T]
#     AbstractAsyncContextManager[Callable[[T], None]],  # SyncHandlerManager[T]
# ]
AnyHandlerManager: TypeAlias = AbstractAsyncContextManager[
    Callable[[T], Awaitable[None] | None]  # Any (sync or async) handler
]

HandlerMiddleware: TypeAlias = Callable[
    ["FlowHandler[T]"],  # Next handler, as FlowHandler[T]
    AsyncGenerator[Callable[[T2], Awaitable[None] | None]],  # FlowHandler[T2] or a handler func
]
_HandlerMiddleware: TypeAlias = Callable[
    ["FlowHandler[T]"],  # Next handler, as FlowHandler[T]
    AbstractAsyncContextManager[Callable[[T2], Awaitable[None] | None]],  # FlowHandler[T2] or a handler func
]

HandlerDecorator: TypeAlias = Callable[
    ["FlowHandlerManager[T]"],  # Next (wrapped) handler
    "FlowHandlerManager[T2]",  # Resulting handler
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


def handler(func: AnyHandler[T]) -> FlowHandlerManager[T]:
    """
    Wrap a function, so it can be used as a flow handler.
    """
    assert callable(func)
    return FlowHandlerManager(lambda: nullcontext(FlowHandler.ensure(func)))


def handler_manager(
    func: Callable[[], AsyncGenerator[AnyHandler[T]]],
) -> FlowHandlerManager[T]:
    """
    Wrap a generator, so it can be used as a flow handler manager.
    """
    assert inspect.isasyncgenfunction(func)
    return FlowHandlerManager(asynccontextmanager(func))


def _ensure_handler_manager_factory(
    source: Callable[P, AbstractAsyncContextManager[AnyHandler[T]]],
) -> Callable[P, AbstractAsyncContextManager[FlowHandler[T]]]:
    @asynccontextmanager
    async def _handler_manager(*args, **kwargs):
        async with source(*args, **kwargs) as h:
            yield FlowHandler.ensure(h)

    return _handler_manager


def ensure_async_handler(source: AnyHandler[T]) -> AsyncHandler[T]:
    if isinstance(source, FlowHandler):
        return source.async_h
    return ensure_async_callable(source)


def ensure_async_handler_manager(source: AnyHandlerManager[T]) -> AsyncHandlerManager[T]:
    @asynccontextmanager
    async def _async_handler_manager():
        async with source as h:
            yield h.async_h if isinstance(h, FlowHandler) else ensure_async_callable(h)

    return _async_handler_manager()  # type: ignore[return-value]


def ensure_sync_handler(source: AnyHandler[T]) -> SyncHandler[T]:
    if isinstance(source, FlowHandler):
        return source.sync_h
    return ensure_sync_callable(source)


def ensure_sync_handler_manager(source: AnyHandlerManager[T]) -> SyncHandlerManager[T]:
    @asynccontextmanager
    async def _sync_handler_manager():
        async with source as h:
            yield h.sync_h if isinstance(h, FlowHandler) else ensure_sync_callable(h)

    return _sync_handler_manager()  # type: ignore[return-value]


@dc.dataclass(frozen=True, slots=True)
class _HandlerMiddlewareDecorator(Generic[T, T2]):
    middleware: _HandlerMiddleware[T, T2]

    def __call__(self, next_hm: FlowHandlerManager[T]) -> FlowHandlerManager[T2]:
        return next_hm << self


def handler_middleware(m: HandlerMiddleware[T, T2], /) -> HandlerDecorator[T, T2]:
    assert inspect.isasyncgenfunction(m)
    return _HandlerMiddlewareDecorator(_handler_middleware(m))


def _handler_middleware(m: HandlerMiddleware[T, T2]) -> _HandlerMiddleware[T, T2]:
    return _ensure_handler_manager_factory(asynccontextmanager(m))


@final
@dc.dataclass(eq=False, kw_only=True)
class FlowHandler(Generic[T]):
    is_async: bool
    async_h: Callable[[T], Awaitable[None]]
    sync_h: Callable[[T], None]

    def __call__(self, item: T, /) -> Awaitable[None] | None:
        return self.async_h(item) if self.is_async else self.sync_h(item)  # type: ignore[func-returns-value]

    def __post_init__(self):
        self.__call__ = self.async_h if self.is_async else self.sync_h  # type: ignore[assignment]

    @classmethod
    def ensure(cls, h: AnyHandler[T]) -> FlowHandler[T]:
        if isinstance(h, FlowHandler):
            return h
        if is_async_callable(h):
            return cls.create_async(async_h=cast(AsyncHandler[T], h))
        else:
            return cls.create_sync(sync_h=cast(SyncHandler[T], h))

    @classmethod
    def create_async(
        cls, *, async_h: AsyncHandler[T] | None = None, sync_h: SyncHandler[T] | None = None
    ) -> FlowHandler[T]:
        if async_h is None and sync_h is None:
            raise ValueError("At least one of async_h or sync_h must be provided")
        return cls(
            is_async=True,
            async_h=async_h if async_h else ensure_async_handler(sync_h),  # type: ignore
            sync_h=sync_h if sync_h else ensure_sync_handler(async_h),  # type: ignore
        )

    @classmethod
    def create_sync(
        cls, *, async_h: AsyncHandler[T] | None = None, sync_h: SyncHandler[T] | None = None
    ) -> FlowHandler[T]:
        if async_h is None and sync_h is None:
            raise ValueError("At least one of async_h or sync_h must be provided")
        return cls(
            is_async=False,
            async_h=async_h if async_h else ensure_async_handler(sync_h),  # type: ignore
            sync_h=sync_h if sync_h else ensure_sync_handler(async_h),  # type: ignore
        )

    def create(
        self,
        *,
        async_h: Callable[[T2], Awaitable[None]] | None = None,
        sync_h: Callable[[T2], None] | None = None,
    ) -> FlowHandler[T2]:
        if self.is_async:
            return FlowHandler.create_async(async_h=async_h, sync_h=sync_h)
        else:
            return FlowHandler.create_sync(async_h=async_h, sync_h=sync_h)


@final
# class _HandlerManagerFactory(
class FlowHandlerManager(  # AnyHandlerManager[T]
    Generic[T],
    AbstractAsyncContextManager[FlowHandler[T]],
):
    _source: Callable[..., AbstractAsyncContextManager[Callable[[T], Awaitable[None] | None]]]
    _middlewares: tuple[_HandlerMiddleware, ...]

    _resolved_hm: AbstractAsyncContextManager[FlowHandler[T]] | None

    def __init__(
        self, factory: Callable[..., AbstractAsyncContextManager[Callable[[T], Awaitable[None] | None]]]
    ) -> None:
        self._source = factory  # Maybe accept static arguments later
        self._resolved_hm = None
        self._middlewares = ()

    def __call__(self, *args, **kwargs) -> AbstractAsyncContextManager[FlowHandler[T]]:
        if not self._middlewares:
            return _ensure_handler_manager_factory(self._source)(*args, **kwargs)

        @asynccontextmanager
        async def _composite_handler_manager():
            hm = self._source(*args, **kwargs)
            async with AsyncExitStack() as es:
                for middleware in self._middlewares:
                    h = await es.enter_async_context(hm)
                    hm = middleware(FlowHandler.ensure(h))
                h = await es.enter_async_context(hm)
                yield FlowHandler.ensure(h)

        return _composite_handler_manager()

    def _use(self, m: _HandlerMiddleware[T, T2]) -> FlowHandlerManager[T2]:
        f = FlowHandlerManager(self._source)
        f._middlewares = self._middlewares + (m,)
        return cast(FlowHandlerManager[T2], f)

    def use(self, m: HandlerMiddleware[T, T2], /) -> FlowHandlerManager[T2]:
        assert inspect.isasyncgenfunction(m)
        return self._use(_handler_middleware(m))

    # handler << handler_decorator
    def __lshift__(self, t: HandlerDecorator[T, T2]) -> FlowHandlerManager[T2]:
        if isinstance(t, _HandlerMiddlewareDecorator):
            return self._use(t.middleware)
        return t(self)

    # handler | service_template
    def __xor__(self, service_template: Callable[[AnyHandlerManager[T]], T2]) -> T2:
        return service_template(self)

    async def __aenter__(self):
        assert not self._resolved_hm  # Protect against re-entering
        self._resolved_hm = self()
        return await self._resolved_hm.__aenter__()

    async def __aexit__(self, exc_type, exc_value, traceback):
        assert self._resolved_hm
        try:
            return await self._resolved_hm.__aexit__(exc_type, exc_value, traceback)
        finally:
            self._resolved_hm = None


@asynccontextmanager
async def stream_consumer(
    source: ObjectReceiveStream[T],
    h: AsyncHandler[T],
    concurrency: int = 1,
    process_leftovers: bool = True,
):
    async def consume(handle_soon: bool):
        while True:
            try:
                item = await source.receive()

                if handle_soon:  # Infinite concurrency, just spawn a new task for each item
                    tg.start_soon(h, item)
                else:
                    await h(item)
            except EndOfStream:
                logger.debug("Source stream has been completed, no more items to consume")
                break
            except ClosedResourceError:
                logger.debug("Receiver has been closed (according to consumer's process_leftovers setting)")
                break

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
