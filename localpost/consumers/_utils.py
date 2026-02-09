from collections.abc import Callable, Awaitable

from anyio import CapacityLimiter, to_thread, from_thread

from localpost._utils import is_async_callable

type SyncHandler[T] = Callable[[T], None]
type AsyncHandler[T] = Callable[[T], Awaitable[None]]
type AnyHandler[T] = SyncHandler[T] | AsyncHandler[T]


def ensure_async_handler(handler, limiter: CapacityLimiter = None) -> AsyncHandler:
    if not is_async_callable(handler):
        return lambda x: to_thread.run_sync(handler, x, limiter=limiter)
    return handler


def ensure_sync_handler(handler) -> SyncHandler:
    if is_async_callable(handler):
        return lambda x: from_thread.run(handler, x)
    return handler
