from collections.abc import Awaitable, Callable

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
