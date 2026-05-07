from __future__ import annotations

import contextvars
import dataclasses as dc
import inspect
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Self, final, overload

from .._utils import is_async_callable

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal

IDLE_TIMEOUT: float = 60.0
"""Seconds an idle worker waits for new work before self-exiting."""


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
                    # coroutine to the event loop via the portal. The worker
                    # thread blocks on ``portal.call`` until the coroutine
                    # completes — same lifetime model as the sync path.
                    portal = self.group._aio_portal
                    if portal is None:
                        result.close()
                        raise RuntimeError(  # noqa: TRY301
                            "TaskGroup received a coroutine but was not given an aio_portal"
                        )
                    coro = result
                    result = portal.call(lambda: coro)
            except BaseException as exc:  # noqa: BLE001 — Trio-style: capture everything
                self.future.set_exception(exc)
                self.group._record_error(exc)
            else:
                self.future.set_result(result)
        finally:
            self.group._task_done()


@final
class Worker:
    """Idle-tracked worker thread with a per-worker inbox.

    Lifecycle: ``alive`` until the thread self-exits after ``IDLE_TIMEOUT``
    seconds with no work, after which the worker becomes a tombstone in the
    global ``idle`` deque. The dispatcher detects tombstones via
    :meth:`submit` returning ``False`` and pops the next worker / spawns a
    fresh one.
    """

    __slots__ = ("_alive", "_cv", "_inbox")

    def __init__(self) -> None:
        self._inbox: deque[Task] = deque()
        self._cv = threading.Condition(threading.Lock())
        # Set under ``_cv`` before lock release in ``_run``; that ordering is
        # what makes ``submit`` race-free vs ``Thread.is_alive()``, which can
        # still report ``True`` after ``_run`` has released the lock but
        # before CPython's bootstrap marks the thread stopped.
        self._alive = True
        threading.Thread(target=self._run, daemon=True, name="localpost-worker").start()

    def submit(self, task: Task) -> bool:
        """Hand off a task. Returns ``False`` if the worker has self-exited."""
        with self._cv:
            if not self._alive:
                return False
            self._inbox.append(task)
            self._cv.notify()
            return True

    def _run(self) -> None:
        while True:
            with self._cv:
                # Re-check inbox after each wait — covers both spurious wakeups
                # and the lost-notify race where a submit lands between the
                # outer ``inbox.popleft`` and the wait re-acquiring the lock.
                while not self._inbox:
                    if not self._cv.wait(timeout=IDLE_TIMEOUT):
                        # Timed out. One more inbox check under the lock to
                        # close the timeout-vs-notify race.
                        if not self._inbox:
                            self._alive = False
                            return
                        break
                task = self._inbox.popleft()
            task.run()
            idle.append(self)


idle: deque[Worker] = deque()
"""Global LIFO stack of idle workers, shared across all ``TaskGroup``s."""


def warmup(count: int, /) -> None:
    """Pre-spawn ``count`` worker threads and park them in the global idle pool.

    Useful at process startup to amortise thread-creation cost so the first
    bursts of work don't pay it. Pre-warmed workers are indistinguishable
    from organically spawned ones — they self-exit on the same idle timeout
    if unused.

    One-shot: ``warmup(8)`` always spawns 8 fresh workers, regardless of
    how many idle workers already exist. Calling it again from a long-idle
    process re-warms.

    Raises:
        ValueError: ``count`` is negative.
    """
    if count < 0:
        raise ValueError("count must be >= 0")
    for _ in range(count):
        idle.append(Worker())


@final
class TaskGroup:
    """Trio-style task group running sync callables on a shared thread pool.

    Tasks submitted via :meth:`start_soon` run on a process-wide pool of
    worker threads. Workers are spawned on demand, reused across all
    ``TaskGroup`` instances, and self-exit after 60 s of idleness. There
    is no concurrency cap — ``start_soon`` always succeeds (modulo OS
    thread limits).

    Lifetime: sync context manager. On exit, blocks until every task
    started inside the ``with`` block has finished, then re-raises any
    task exceptions wrapped in an :class:`ExceptionGroup` (Trio
    ``strict_exception_groups=True`` semantics — a body exception and
    task exceptions are merged into one group).

    Example::

        with TaskGroup() as tg:
            tg.start_soon(do_work, arg)        # fire-and-forget
            fut = tg.create_task(other_work)   # observe via Future
        # On exit: drains in-flight tasks; raises ExceptionGroup if any failed.

    Both :meth:`start_soon` and :meth:`create_task` are callable from any
    thread, including from inside a task running on the same group
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

    If ``aio_portal`` is provided, both spawn methods also accept async
    callables: the coroutine is dispatched to the portal's event loop
    via :meth:`anyio.from_thread.BlockingPortal.call`, run from inside
    the worker thread (the worker blocks until the coroutine returns).
    Submitting an async callable without a portal raises ``RuntimeError``.
    """

    __slots__ = ("_aio_portal", "_closed", "_cv", "_errors", "_lock", "_name", "_pending")

    def __init__(
        self,
        *,
        name: str | None = None,
        aio_portal: BlockingPortal | None = None,
    ) -> None:
        self._name = name
        self._aio_portal = aio_portal
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
        all_errors: list[BaseException] = []
        if exc is not None:
            all_errors.append(exc)
        all_errors.extend(self._errors)
        if all_errors:
            label = f"TaskGroup {self._name!r} failed" if self._name else "TaskGroup failed"
            # ``BaseExceptionGroup(...)`` returns ``ExceptionGroup`` when every
            # member is an ``Exception`` subclass, ``BaseExceptionGroup`` otherwise
            # — matches Trio semantics for ``KeyboardInterrupt`` / ``SystemExit``.
            raise BaseExceptionGroup(label, all_errors)

    @overload
    def start_soon[**P](
        self, fn: Callable[P, Awaitable[Any]], /, *args: P.args, **kwargs: P.kwargs
    ) -> None: ...
    @overload
    def start_soon[**P](
        self, fn: Callable[P, Any], /, *args: P.args, **kwargs: P.kwargs
    ) -> None: ...
    def start_soon(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
        """Submit ``fn(*args, **kwargs)`` to a worker thread. Fire-and-forget.

        Errors still surface via the ``ExceptionGroup`` raised at
        ``__exit__``. Use :meth:`create_task` if you need to observe the
        task's result or exception via a :class:`concurrent.futures.Future`.
        """
        self.create_task(fn, *args, **kwargs)

    @overload
    def create_task[**P, R](
        self, fn: Callable[P, Awaitable[R]], /, *args: P.args, **kwargs: P.kwargs
    ) -> Future[R]: ...
    @overload
    def create_task[**P, R](
        self, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> Future[R]: ...
    def create_task(
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Future[Any]:
        """Submit ``fn(*args, **kwargs)`` to a worker thread. Returns a future.

        ``fn`` may be sync or async. Async callables require ``aio_portal``;
        the worker invokes ``portal.call`` to await the coroutine on the
        event loop.

        The returned future is observation-only. ``Future.cancel()`` only
        succeeds while the task is still queued; it cannot interrupt a
        running task. Reading ``.exception()`` does not suppress the
        ``ExceptionGroup`` raised at ``__exit__``.
        """
        if is_async_callable(fn) and self._aio_portal is None:
            raise RuntimeError("TaskGroup got an async callable but the group has no aio_portal")
        with self._lock:
            if self._closed:
                raise RuntimeError("TaskGroup is closed")
            self._pending += 1
        fut: Future[Any] = Future()
        # Snapshot the caller's context now (matches Trio / AnyIO / asyncio
        # ``to_thread`` semantics); each spawn captures independently.
        task = Task(fut, fn, args, kwargs, self, contextvars.copy_context())
        while True:
            try:
                w = idle.pop()
            except IndexError:
                # No idle worker — spawn a fresh one. ``submit`` on a fresh
                # worker is guaranteed to succeed (``_alive=True``).
                Worker().submit(task)
                return fut
            if w.submit(task):
                return fut
            # Tombstone (self-died on idle timeout); pop the next one.

    def _task_done(self) -> None:
        with self._cv:
            self._pending -= 1
            if self._pending == 0:
                self._cv.notify_all()

    def _record_error(self, exc: BaseException) -> None:
        # No lock needed: ``list.append`` is atomic in CPython (GIL or
        # free-threaded per-list mutex). Memory visibility to ``__exit__``
        # is established by ``_task_done``'s acquire of ``_cv``, which always
        # runs after ``_record_error`` in the same task.
        self._errors.append(exc)


