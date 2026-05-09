from __future__ import annotations

import contextvars
import dataclasses as dc
import functools
import inspect
import math
import threading
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from concurrent.futures import Future
from contextlib import AbstractContextManager, asynccontextmanager
from typing import Any, Self, final, overload

from anyio import CapacityLimiter, to_thread
from anyio.abc import TaskGroup as AioTaskGroup
from anyio.from_thread import BlockingPortal
from coverage.debug import pp

from localpost._utils import set_cvar

IDLE_TIMEOUT: float = 60.0
"""Seconds an idle worker waits for new work before self-exiting."""

_current_pool: contextvars.ContextVar[WorkerPool] = contextvars.ContextVar("localpost.threadtools.current_pool")
"""Ambient :class:`ThreadPool` for the current async context. Set by
:func:`thread_pool`; read by :class:`TaskGroup` on construction."""


@dc.dataclass(slots=True)
class Task:
    future: Future[Any]
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    group: TaskGroup
    context: contextvars.Context
    """Snapshot of the caller's ContextVars at ``start_soon`` time. The user
    callable runs inside this context; mutations are confined to the task."""

    def run(self) -> None:
        try:
            try:
                result = self.context.run(self.fn, *self.args, **self.kwargs)
                if inspect.iscoroutine(result):
                    # The user submitted an async callable; dispatch the
                    # coroutine to the event loop via the pool's portal. The
                    # worker thread blocks on ``portal.call`` until the
                    # coroutine completes — same lifetime model as the sync
                    # path.
                    coro = result
                    result = self.group._pool.portal.call(lambda: coro)
            except BaseException as exc:  # noqa: BLE001 — Trio-style: capture everything
                self.future.set_exception(exc)
                self.group._record_error(exc)
            else:
                self.future.set_result(result)
        finally:
            self.group._task_done()


@final
class Worker:
    """Idle-tracked worker with a per-worker inbox.

    Lifecycle: ``alive`` until the worker self-exits — either after
    ``IDLE_TIMEOUT`` seconds with no work, or when the owning pool calls
    :meth:`shutdown`. After self-exit the worker becomes a tombstone in
    the pool's ``idle`` deque. The dispatcher detects tombstones via
    :meth:`submit` returning ``False`` and pops the next worker / spawns
    a fresh one.

    The worker thread itself is provided by the pool via
    ``anyio.to_thread.run_sync(worker._run, abandon_on_cancel=False)`` —
    AnyIO sets up the thread-local state needed for
    :func:`anyio.from_thread.check_cancelled` to work inside user tasks.
    """

    def __init__(self, pool: WorkerPool) -> None:
        self._inbox: deque[Task] = deque()
        self._cv = threading.Condition(threading.Lock())
        # Set under ``_cv`` before lock release in ``_run``; that ordering is
        # what makes ``submit`` race-free vs the host ``to_thread.run_sync``
        # call returning, which can otherwise still report the worker alive
        # after ``_run`` has released the lock.
        self._alive = True
        self._pool = pool

    def wakeup(self) -> None:
        with self._cv:
            self._cv.notify()

    def submit(self, task: Task) -> bool:
        """Hand off a task. Returns ``False`` if the worker has self-exited."""
        with self._cv:
            if not self._alive:
                return False
            self._inbox.append(task)
            self._cv.notify()
            return True

    def _run(self) -> None:
        while not self._pool._closed:
            with self._cv:
                while not self._inbox:
                    # TODO Can we use wait_for here?
                    if not self._cv.wait(timeout=IDLE_TIMEOUT):
                        # Idle timeout or pool closed.
                        # One more inbox check under the lock to close the timeout-vs-notify race.
                        if not self._inbox:
                            self._alive = False
                            return
                        break
                task = self._inbox.popleft()
            task.run()
            self._pool._idle.append(self)


@final
class TaskGroup:
    """Trio-style task group running sync callables on a shared thread pool.

    Tasks submitted via :meth:`start_soon` run on workers borrowed from
    the ambient :class:`localpost.threadtools.ThreadPool`. Construction
    requires an active ``thread_pool()`` context — typically provided by
    :func:`localpost.hosting.run_app`. Without one, ``__init__`` raises
    :class:`RuntimeError`.

    Workers are spawned on demand, reused across all ``TaskGroup``
    instances under the same pool, and self-exit after
    :data:`IDLE_TIMEOUT` seconds of idleness. There is no concurrency
    cap from this class (the pool may impose one via its
    :class:`anyio.CapacityLimiter`).

    Lifetime: sync context manager. On exit, blocks until every task
    started inside the ``with`` block has finished, then re-raises any
    task exceptions wrapped in an :class:`ExceptionGroup` (Trio
    ``strict_exception_groups=True`` semantics — a body exception and
    task exceptions are merged into one group).

    Example::

        async with thread_pool():
            with TaskGroup() as tg:
                tg.start_soon(do_work, arg)  # fire-and-forget
                fut = tg.create_task(other_work)  # observe via Future
            # On exit: drains in-flight tasks; raises ExceptionGroup if any failed.

    Both :meth:`start_soon` and :meth:`create_task` are callable from
    any thread, including from inside a task running on the same group
    (recursive spawn). ``start_soon`` is fire-and-forget; ``create_task``
    returns a :class:`concurrent.futures.Future` that captures the task's
    result or exception. Either way, task exceptions are surfaced via the
    ``ExceptionGroup`` raised at ``__exit__`` — for ``create_task``,
    reading the future's exception does not suppress that group raise.

    ``contextvars`` are propagated: each spawn snapshots the caller's
    context with :func:`contextvars.copy_context`, and the task runs
    inside that snapshot. Mutations the task makes to ContextVars stay
    confined to its copy — same semantics as :func:`asyncio.to_thread`
    and Trio / AnyIO task spawn.

    Async callables are accepted by both spawn methods: the coroutine is
    dispatched to the pool's portal via
    :meth:`anyio.from_thread.BlockingPortal.call`, run from inside the
    worker thread (the worker blocks until the coroutine returns).

    Cancellation: because workers are spawned via
    ``anyio.to_thread.run_sync`` under the pool, user code can call
    :func:`anyio.from_thread.check_cancelled` to observe cancellation of
    the pool's host scope. Tasks that don't poll won't be interrupted.
    """

    def __init__(self, pool: WorkerPool, *, name: str | None = None) -> None:
        self._name = name  # TODO Remove
        self._pool = pool
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending = 0
        # ``deque.append`` is documented thread-safe (vs ``list.append`` which
        # is only atomic by CPython implementation accident). Workers append
        # without holding any group-level lock; ``__exit__`` reads after
        # drain, so the ``_cv`` release/acquire in ``_task_done`` provides
        # the visibility barrier.
        self._errors: deque[BaseException] = deque()
        self._closed = False

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("TaskGroup cannot be reused")
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        # Drain. Nested ``start_soon`` from in-flight tasks is allowed — the
        # while loop re-checks after each notify.
        with self._cv:
            while self._pending > 0:
                self._cv.wait()
            self._closed = True
        # Seed from ``_errors`` first so recorded tasks keep their original
        # order. Prepend the body exception only if it isn't already in
        # there — a task exception observed via ``Future.result()`` and let
        # to propagate is the same instance as the one in ``_errors``;
        # surfacing it once preserves both dedup and record order.
        all_errors: list[BaseException] = list(self._errors)
        if exc is not None and all(e is not exc for e in all_errors):
            all_errors.insert(0, exc)
        if all_errors:
            label = f"TaskGroup {self._name!r} failed" if self._name else "TaskGroup failed"
            # ``BaseExceptionGroup(...)`` returns ``ExceptionGroup`` when every
            # member is an ``Exception`` subclass, ``BaseExceptionGroup`` otherwise
            # — matches Trio semantics for ``KeyboardInterrupt`` / ``SystemExit``.
            raise BaseExceptionGroup(label, all_errors)

    @overload
    def start_soon[**P](self, fn: Callable[P, Awaitable[Any]], /, *args: P.args, **kwargs: P.kwargs) -> None: ...
    @overload
    def start_soon[**P](self, fn: Callable[P, Any], /, *args: P.args, **kwargs: P.kwargs) -> None: ...
    def start_soon(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
        """Submit ``fn(*args, **kwargs)`` to a worker thread. Fire-and-forget.

        Errors still surface via the ``ExceptionGroup`` raised at
        ``__exit__``. Use :meth:`create_task` if you need to observe the
        task's result or exception via a :class:`concurrent.futures.Future`.
        """
        self.create_task(fn, *args, **kwargs)

    @overload
    def create_task[**P, R](self, fn: Callable[P, Awaitable[R]], /, *args: P.args, **kwargs: P.kwargs) -> Future[R]: ...
    @overload
    def create_task[**P, R](self, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> Future[R]: ...
    def create_task(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        """Submit ``fn(*args, **kwargs)`` to a worker thread. Returns a future.

        ``fn`` may be sync or async. Async callables are dispatched to
        the pool's portal; the worker invokes ``portal.call`` to await
        the coroutine on the event loop.

        The returned future is observation-only. ``Future.cancel()`` only
        succeeds while the task is still queued; it cannot interrupt a
        running task. Reading ``.exception()`` does not suppress the
        ``ExceptionGroup`` raised at ``__exit__``.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("TaskGroup is closed")  # TODO AnyIO ClosedResourceError ?
            self._pending += 1
        fut: Future[Any] = Future()
        # Snapshot the caller's context (matches Trio / AnyIO / asyncio semantics)
        task = Task(fut, fn, args, kwargs, self, contextvars.copy_context())
        while True:
            if self._pool.get_idle_worker().submit(task):
                return fut
            # Tombstone (worker self-died on idle timeout), pop the next one

    def _task_done(self) -> None:
        with self._cv:
            self._pending -= 1
            if self._pending == 0:
                self._cv.notify_all()

    def _record_error(self, exc: BaseException) -> None:
        # No lock needed: ``deque.append`` is documented thread-safe (vs
        # ``list.append`` which is only atomic by CPython implementation
        # accident). Memory visibility to ``__exit__`` is established by
        # ``_task_done``'s acquire of ``_cv``, which always runs after
        # ``_record_error`` in the same task.
        self._errors.append(exc)


def task_group(*, name: str | None = None) -> AbstractContextManager[TaskGroup]:
    if pool := _current_pool.get(None):
        return TaskGroup(pool, name=name)
    raise RuntimeError(
        "No active thread_pool() context. Wrap your code in "
        "`async with thread_pool():` or run inside "
        "`localpost.hosting.run_app()`."
    )


class AnyIOWorkerExecutor:
    def submit[R](fn: Callable[..., R], /, *args, **kwargs) -> Future[R]:
        pass


class AnyIOExecutor:
    def submit[R](fn: Callable[..., R], /, *args, **kwargs) -> Future[R]:
        pass


class WorkerExecutor:
    def submit[R](fn: Callable[..., R], /, *args, **kwargs) -> Future[R]:
        pass


@final
class WorkerPool:
    def __init__(self, portal: BlockingPortal, host_tg: AioTaskGroup) -> None:
        self._portal = portal
        self._host_tg = host_tg
        self._idle: deque[Worker] = deque()
        self._limiter = CapacityLimiter(math.inf)
        self._closed = False

    def get_idle_worker(self) -> Worker:
        try:
            return self._idle.pop()
        except IndexError:
            return self._spawn_worker_blocking()

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
        """Create a worker (must be called from the event-loop thread)."""
        w = Worker(self)
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
        if self._closed:
            raise RuntimeError("pool is closed")
        return self._portal.call(self._spawn_worker)

    def _shutdown_all_workers(self) -> None:
        self._closed = True
        while self._idle:
            self._idle.pop().wakeup()


@asynccontextmanager
async def thread_pool(portal: BlockingPortal, host_tg: AioTaskGroup) -> AsyncGenerator[WorkerPool]:
    with set_cvar(_current_pool, WorkerPool(portal, host_tg)) as pool:
        try:
            yield pool
        finally:
            # At that point all the workers should be idle. Wake them up so they can exit promptly, otherwise they'd sit
            # on the IDLE_TIMEOUT before the host task group could drain).
            pool._shutdown_all_workers()
