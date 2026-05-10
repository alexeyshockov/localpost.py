from collections.abc import Awaitable, Callable, Generator
from contextlib import AbstractAsyncContextManager, contextmanager

from localpost._portal import Portal
from localpost.hosting import current_service

from ._channel import Channel, ReceiveChannel, SendChannel
from ._executor import (
    DEFAULT_IDLE_TIMEOUT,
    AsyncExecutor,
    AsyncWorkerExecutor,
    Executor,
    WorkerExecutor,
)
from ._task_group import TaskGroup

__all__ = [
    "DEFAULT_IDLE_TIMEOUT",
    "AsyncExecutor",
    "AsyncWorkerExecutor",
    "Channel",
    "Executor",
    "Portal",
    "ReceiveChannel",
    "SendChannel",
    "TaskGroup",
    "WorkerExecutor",
    "as_sync_cm",
    "run_async",
]


def run_async[**P, R](
    func: Callable[P, Awaitable[R]],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Run an async ``func`` on the current service's loop and return its result.

    Counterpart to :func:`anyio.to_thread.run_sync`: call from a worker thread
    to dispatch back into the event loop. Resolves the portal via
    :func:`localpost.hosting.current_service`, so the caller's
    :class:`contextvars.Context` must carry the hosting context (true for any
    thread spawned through AnyIO / the threadtools executors).

    Must be called from a non-loop thread; raises :class:`RuntimeError`
    otherwise (would deadlock the loop).
    """
    return current_service().portal.run_async(func, *args, **kwargs)


@contextmanager
def as_sync_cm[T](
    cm: AbstractAsyncContextManager[T],
    /,
    *,
    portal: Portal | None = None,
) -> Generator[T]:
    """Sync view over an async context manager.

    Counterpart to :func:`run_async` for context managers: lets a worker
    thread enter an async CM via a portal, transparently dispatching
    ``__aenter__`` / ``__aexit__`` onto the loop. Resolves the portal via
    :func:`localpost.hosting.current_service` when omitted, so the caller's
    :class:`contextvars.Context` must carry the hosting context (true for any
    thread spawned through AnyIO / the threadtools executors).

    Must be entered from a non-loop thread; raises :class:`RuntimeError`
    otherwise (would deadlock the loop).
    """
    p = portal if portal is not None else current_service().portal
    if p.same_thread:
        raise RuntimeError("Deadlock: synchronous wait on the portal's loop thread")
    with p.raw.wrap_async_context_manager(cm) as result:
        yield result
