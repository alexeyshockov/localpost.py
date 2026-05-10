"""Thread-management primitives for ``localpost.threadtools``.

Two flavors of executor, each with the same ``submit`` contract but
different lifecycle and cancellation properties:

* :class:`WorkerExecutor` — sync context manager. Spawn-on-demand pool of
  plain ``threading.Thread`` workers backed by a ``deque`` and a
  :class:`threading.Condition`. No event loop required.
* :class:`AsyncWorkerExecutor` — async context manager. Same shape, but
  workers run inside ``anyio.to_thread.run_sync(..., abandon_on_cancel=False)``
  so user code can call :func:`anyio.from_thread.check_cancelled`. The
  cancel scope of each worker spans its whole lifetime (many tasks reuse
  the same worker), so cancellation granularity is *per-worker*, not
  per-task.

Both propagate :class:`contextvars.Context` to the task, matching
:func:`asyncio.to_thread` / Trio / AnyIO spawn semantics.

There is no cap on the number of workers and no backlog — concurrency is
the caller's concern (a Cloud Run-like upstream gate, a consumer-level
``Semaphore``, etc.). Workers are spawned lazily as submissions arrive
with no idle worker available, and live until the executor closes; see
``docs/adr/0005-no-idle-timeout-for-worker-pools.md`` for the rationale.

The async variant takes a caller-owned :class:`localpost.Portal` and runs
its internal :class:`anyio.abc.TaskGroup` on its loop. It exposes
:meth:`stop` (safe from any thread) for cooperative cancellation.
"""

from __future__ import annotations

import contextvars
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol, Self, final, runtime_checkable

from anyio import create_task_group, to_thread

from localpost._portal import Portal


@runtime_checkable
class Executor(Protocol):
    """Minimal executor contract: just :meth:`submit`.

    Lifecycle (sync ``with`` vs ``async with``) is implementation-specific —
    callers depend only on the submit shape.
    """

    def submit[**P, R](
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[R]: ...


@dataclass(eq=False, slots=True)
class Task:
    """A single submission carried through the executor pipeline.

    The caller's :class:`contextvars.Context` is snapshotted at construction;
    the task runs inside that copy so mutations don't leak back.
    """

    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    context: contextvars.Context = field(default_factory=contextvars.copy_context)
    future: Future[Any] = field(default_factory=Future)

    def run(self) -> None:
        if not self.future.set_running_or_notify_cancel():
            return  # Future was cancelled while queued
        try:
            result = self.context.run(self.fn, *self.args, **self.kwargs)
        except BaseException as exc:  # noqa: BLE001
            self.future.set_exception(exc)
        else:
            self.future.set_result(result)


# --------------------------------------------------------------------------
# WorkerExecutor — deque + Condition, plain threads
# --------------------------------------------------------------------------


@final
class WorkerExecutor:
    """Spawn-on-demand pool of plain ``threading.Thread`` workers.

    ``submit`` enqueues onto a shared :class:`collections.deque` under a
    :class:`threading.Condition`; if no worker is idle, a new one is
    spawned. There is no cap and no backlog — concurrency is the caller's
    concern. Once spawned, a worker lives until the executor closes. See
    ``docs/adr/0005-no-idle-timeout-for-worker-pools.md``.
    """

    def __init__(self, *, thread_name_prefix: str = "lp-worker") -> None:
        self._thread_name_prefix = thread_name_prefix
        self._cond = threading.Condition()
        self._queue: deque[Task] = deque()
        self._workers: list[threading.Thread] = []
        self._idle = 0
        self._opened = False
        self._closed = False

    @property
    def workers(self) -> list[threading.Thread]:
        return self._workers

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("WorkerExecutor cannot be reused")
        self._opened = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        for t in list(self._workers):
            t.join()

    def submit[**P, R](
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[R]:
        task = Task(fn, args, kwargs)
        with self._cond:
            if not self._opened or self._closed:
                raise RuntimeError("WorkerExecutor is not open")
            self._queue.append(task)
            if self._idle == 0:
                self._spawn_worker()
            self._cond.notify()
        return task.future

    def _spawn_worker(self) -> None:
        wid = len(self._workers)
        t = threading.Thread(
            target=self._run_worker,
            name=f"{self._thread_name_prefix}-{wid}",
            daemon=True,
        )
        self._workers.append(t)
        t.start()

    def _run_worker(self) -> None:
        while True:
            with self._cond:
                self._idle += 1
                while not self._queue and not self._closed:
                    self._cond.wait()
                self._idle -= 1
                if not self._queue:
                    return  # closed and drained
                task = self._queue.popleft()
            task.run()


# --------------------------------------------------------------------------
# AsyncWorkerExecutor — deque + Condition, AnyIO-backed worker threads
# --------------------------------------------------------------------------


@final
class AsyncWorkerExecutor:
    """Like :class:`WorkerExecutor`, but workers run via
    ``anyio.to_thread.run_sync`` so user code can call
    :func:`anyio.from_thread.check_cancelled`.

    Async context manager. The internal :class:`anyio.abc.TaskGroup` runs on
    the loop the ``portal`` is attached to and hosts every worker's
    ``host_task``. ``portal`` is required and caller-owned.

    Cancel granularity is per-worker (cancel scope spans the worker's whole
    lifetime, which serves many tasks). Use :meth:`stop` for fast cooperative
    shutdown — it cancels the internal task group and notifies idle workers
    so they wake immediately and active tasks' next ``check_cancelled`` raises.
    """

    def __init__(self, *, portal: Portal) -> None:
        self._portal = portal
        self._cond = threading.Condition()
        self._queue: deque[Task] = deque()
        self._worker_count = 0
        self._idle = 0
        # Constructed eagerly: callers always instantiate inside ``async with`` on the
        # portal's loop (the only loop ``tg.start_soon`` can target via ``portal``), so
        # ``get_async_backend()`` is satisfied. Loop-affinity is set by ``__aenter__``.
        self._tg = create_task_group()
        self._opened = False
        self._closed = False

    @property
    def portal(self) -> Portal:
        return self._portal

    @property
    def worker_count(self) -> int:
        return self._worker_count

    async def __aenter__(self) -> Self:
        if self._closed:
            raise RuntimeError("AsyncWorkerExecutor cannot be reused")
        await self._tg.__aenter__()
        self._opened = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        with self._cond:
            if not self._closed:
                self._closed = True
                self._cond.notify_all()
        # Exit the task group: on clean body it waits for host tasks to finish;
        # on exception AnyIO cancels children automatically.
        await self._tg.__aexit__(exc_type, exc, tb)

    def stop(self) -> None:
        """Cooperatively shut down: wake idle workers and cancel the internal
        task group. Safe from any thread. Idempotent.

        Idle workers wake from the condition; active tasks see their next
        :func:`anyio.from_thread.check_cancelled` raise. Tasks that don't poll
        run to natural completion.
        """
        if not self._opened:
            return
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        self._portal.run_sync(self._tg.cancel_scope.cancel)

    def submit[**P, R](
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[R]:
        task = Task(fn, args, kwargs)
        with self._cond:
            if not self._opened or self._closed:
                raise RuntimeError("AsyncWorkerExecutor is not open")
            self._queue.append(task)
            if self._idle == 0:
                self._spawn_worker()
            self._cond.notify()
        return task.future

    def _spawn_worker(self) -> None:
        async def host_task() -> None:
            await to_thread.run_sync(self._run_worker, abandon_on_cancel=False)

        # Schedule the host task into our internal task group from any thread.
        self._portal.run_sync(self._tg.start_soon, host_task)
        self._worker_count += 1

    def _run_worker(self) -> None:
        while True:
            with self._cond:
                self._idle += 1
                while not self._queue and not self._closed:
                    self._cond.wait()
                self._idle -= 1
                if not self._queue:
                    return  # closed and drained
                task = self._queue.popleft()
            task.run()
