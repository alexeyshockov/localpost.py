"""Process-wide(-ish) thread pool tying our worker dispatch to AnyIO's
``to_thread`` machinery.

The pool owns three things:

* a :class:`anyio.from_thread.BlockingPortal` so sync code (worker threads,
  test harnesses) can call back into the event loop;
* an ``anyio.TaskGroup`` of *host tasks* — one per live worker. Each host
  task awaits ``anyio.to_thread.run_sync(worker._run, abandon_on_cancel=False,
  limiter=...)``, which is what populates ``threadlocals.current_token``
  (asyncio) / ``PARENT_TASK_DATA`` (Trio) on the worker thread. That's
  what makes :func:`anyio.from_thread.check_cancelled` work inside user
  tasks running on our pool;
* a per-pool LIFO ``idle`` deque of parked workers, plus a set of all
  live workers used to broadcast ``shutdown`` on teardown.

The pool is exposed via :func:`thread_pool` (an async context manager)
and is typically opened inside :func:`localpost.hosting.run_app` so it's
ambient throughout the host's lifetime. :class:`TaskGroup` reads the
ambient pool from a contextvar.
"""

from __future__ import annotations

import functools
import math
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, final

from anyio import CapacityLimiter, create_task_group, to_thread
from anyio.from_thread import BlockingPortal

from ._task_group import Worker, _current_pool

if TYPE_CHECKING:
    from anyio.abc import TaskGroup as AioTaskGroup


@final
class ThreadPool:
    """A pool of worker threads, hosted by AnyIO's ``to_thread`` machinery.

    Construct via :func:`thread_pool` rather than directly.
    """

    __slots__ = ("_host_tg", "_idle", "_limiter", "_portal", "_workers")

    def __init__(
        self,
        *,
        portal: BlockingPortal,
        host_tg: AioTaskGroup,
        limiter: CapacityLimiter,
    ) -> None:
        self._portal = portal
        self._host_tg = host_tg
        self._limiter = limiter
        self._idle: deque[Worker] = deque()
        # Tracks every worker spawned by this pool so teardown can wake
        # parked ones. ``deque`` is documented thread-safe for
        # ``append`` / ``remove``; we use a list-of-strong-refs and rely
        # on ``Worker.shutdown`` being idempotent.
        self._workers: list[Worker] = []

    @property
    def portal(self) -> BlockingPortal:
        """The pool's blocking portal — used by tasks to call back into
        the host event loop."""
        return self._portal

    @property
    def idle(self) -> deque[Worker]:
        """Per-pool LIFO deque of idle workers. Public for inspection
        only; mutating it from outside is undefined."""
        return self._idle

    async def warmup(self, n: int, /) -> None:
        """Pre-spawn ``n`` workers and park them in the idle deque.

        Useful at startup to amortise OS thread-creation cost so the
        first burst of work doesn't pay it.

        Raises:
            ValueError: ``n`` is negative.
        """
        if n < 0:
            raise ValueError("count must be >= 0")
        for _ in range(n):
            w = self._spawn_worker()
            self._idle.append(w)

    def _spawn_worker(self) -> Worker:
        """Create a worker and dispatch its ``_run`` to the host task
        group. Must be called from the event-loop thread."""
        w = Worker(self._idle)
        self._workers.append(w)
        # ``functools.partial`` is needed because ``start_soon`` doesn't
        # forward kwargs.
        self._host_tg.start_soon(
            functools.partial(
                to_thread.run_sync,
                w._run,
                abandon_on_cancel=False,
                limiter=self._limiter,
            )
        )
        return w

    def _spawn_worker_blocking(self) -> Worker:
        """Sync entry point for ``TaskGroup.create_task``. Hops to the
        event loop via the portal to do the actual spawn."""
        return self._portal.call(self._spawn_worker)

    def _shutdown_all_workers(self) -> None:
        """Signal every live worker to exit at its next opportunity."""
        for w in self._workers:
            w.shutdown()


@asynccontextmanager
async def thread_pool(
    *, limiter: CapacityLimiter | None = None
) -> AsyncGenerator[ThreadPool]:
    """Open a :class:`ThreadPool`, set it as the ambient pool for
    :class:`TaskGroup` callers, and tear it down on exit.

    The pool runs on the calling event loop: the
    :class:`~anyio.from_thread.BlockingPortal` and the host task group
    that owns worker host tasks are both opened inside this context.

    Args:
        limiter: Caps how many worker threads can run concurrently from
            this pool's POV. Defaults to ``CapacityLimiter(math.inf)``
            (no cap). Note that AnyIO's own
            :func:`anyio.to_thread.run_sync` machinery underlies each
            worker, but the limiter here is dedicated to this pool.

    Tasks running on workers spawned by this pool can call
    :func:`anyio.from_thread.check_cancelled` to observe cancellation of
    the surrounding scope. On teardown, idle workers are signalled to
    exit; in-flight tasks that don't poll are not interrupted (we wait
    for them to return).
    """
    if limiter is None:
        limiter = CapacityLimiter(math.inf)
    async with BlockingPortal() as portal, create_task_group() as host_tg:
        pool = ThreadPool(portal=portal, host_tg=host_tg, limiter=limiter)
        token = _current_pool.set(pool)
        try:
            yield pool
        finally:
            _current_pool.reset(token)
            # Wake idle workers parked in ``_cv.wait`` so their ``_run``
            # returns promptly (otherwise they'd sit on the IDLE_TIMEOUT
            # for up to 60 s before the host task group could drain).
            pool._shutdown_all_workers()
            # Cancel host_tg so any task currently calling
            # ``check_cancelled`` from inside a worker raises. With
            # ``abandon_on_cancel=False`` the host tasks still wait for
            # ``_run`` to return — workers exit either because the
            # in-flight task completed, raised on cancel, or the
            # ``_shutdown`` flag was observed.
            host_tg.cancel_scope.cancel()
