"""A thread-friendly task group that owns a logical set of submissions to an
:class:`Executor`.

The group enforces structured concurrency: every task started inside a
``with TaskGroup(executor) as tg:`` block must complete before the block
exits. Failures are collected and surfaced as a :class:`BaseExceptionGroup`.

There is no cancel signal — running tasks are not interrupted on the first
failure. Use an executor that propagates AnyIO cancellation
(:class:`AnyIOWorkerExecutor`, :class:`AnyIOExecutor`) and have your tasks
poll :func:`anyio.from_thread.check_cancelled` if cooperative cancel is
needed in your application.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ALL_COMPLETED, Future, wait
from types import TracebackType
from typing import Any, Self, final, overload

from ._executor import Executor


@final
class TaskGroup:
    """Trio-style task group backed by a user-supplied :class:`Executor`.

    Tasks are submitted via :meth:`start_soon` (fire-and-forget) or
    :meth:`create_task` (returns a :class:`~concurrent.futures.Future`).
    The group is a sync context manager; on exit it waits for every
    in-flight task — including ones spawned recursively from inside other
    tasks — to settle, then re-raises any failures wrapped in a
    :class:`BaseExceptionGroup` (Trio ``strict_exception_groups=True``
    semantics: a body exception and task exceptions are merged into one
    group).

    ``contextvars`` propagation, thread-of-execution, and concurrency
    limits are the executor's concern. The task group is a thin
    bookkeeping layer.
    """

    def __init__(self, executor: Executor, *, name: str | None = None) -> None:
        self._executor = executor
        self._name = name
        self._lock = threading.Lock()
        # ``_tasks`` holds every Future the group has produced. Successful tasks
        # are removed by ``_on_done``; failed / cancelled tasks remain so we can
        # collect their exceptions at exit.
        self._tasks: list[Future[Any]] = []
        self._opened = False
        self._closed = False

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("TaskGroup cannot be reused")
        self._opened = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Drain. Tasks may spawn more tasks; loop until no pending remain.
        while True:
            with self._lock:
                pending = [t for t in self._tasks if not t.done()]
            if not pending:
                break
            wait(pending, return_when=ALL_COMPLETED)

        with self._lock:
            self._closed = True
            failed = list(self._tasks)

        all_errors: list[BaseException] = []
        for f in failed:
            if f.cancelled():
                continue
            e = f.exception()
            if e is not None:
                all_errors.append(e)
        # Dedup body exception by identity, prepended to preserve task-completion
        # order for everything else.
        if exc is not None and all(e is not exc for e in all_errors):
            all_errors.insert(0, exc)
        if all_errors:
            label = f"TaskGroup {self._name!r} failed" if self._name else "TaskGroup failed"
            raise BaseExceptionGroup(label, all_errors)

    @overload
    def start_soon[**P](self, fn: Callable[P, Awaitable[Any]], /, *args: P.args, **kwargs: P.kwargs) -> None: ...
    @overload
    def start_soon[**P](self, fn: Callable[P, Any], /, *args: P.args, **kwargs: P.kwargs) -> None: ...
    def start_soon(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
        """Submit ``fn(*args, **kwargs)`` to the executor. Fire-and-forget.

        Errors still surface via the :class:`BaseExceptionGroup` raised at
        ``__exit__``. Use :meth:`create_task` if you need to observe the
        task's outcome via a :class:`~concurrent.futures.Future`.
        """
        self.create_task(fn, *args, **kwargs)

    @overload
    def create_task[**P, R](self, fn: Callable[P, Awaitable[R]], /, *args: P.args, **kwargs: P.kwargs) -> Future[R]: ...
    @overload
    def create_task[**P, R](self, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> Future[R]: ...
    def create_task(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        """Submit ``fn(*args, **kwargs)``. Returns the executor's future.

        Reading ``Future.exception()`` does *not* suppress the
        :class:`BaseExceptionGroup` raised at ``__exit__`` — body code that
        re-raises a task exception will see it deduped (by identity) inside
        the group.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("TaskGroup is closed")
            if not self._opened:
                raise RuntimeError("TaskGroup must be entered before submitting tasks")
        fut = self._executor.submit(fn, *args, **kwargs)
        with self._lock:
            self._tasks.append(fut)
        fut.add_done_callback(self._on_done)
        return fut

    def _on_done(self, f: Future[Any]) -> None:
        if f.cancelled():
            return
        if f.exception() is not None:
            return  # keep in ``_tasks`` for collection at __exit__
        with self._lock:
            try:
                self._tasks.remove(f)
            except ValueError:
                pass
