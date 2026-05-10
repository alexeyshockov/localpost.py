"""Thread-management primitives for ``localpost.threadtools``.

Three flavors of executor, each with the same ``submit`` contract but
different lifecycle and cancellation properties:

* :class:`WorkerExecutor` — sync context manager. Channel-backed pool of
  plain ``threading.Thread`` workers. Lazy spawn (driven by
  ``put_nowait`` / ``WouldBlock``); workers live until the executor
  closes. Standalone — no AnyIO loop required.
* :class:`AsyncWorkerExecutor` — async context manager. Same
  channel / lazy-spawn shape, but workers run inside
  ``anyio.to_thread.run_sync(..., abandon_on_cancel=False)`` so user code
  can call :func:`anyio.from_thread.check_cancelled`. The cancel scope of
  each worker spans its whole lifetime (many tasks reuse the same worker),
  so cancellation granularity is *per-worker*, not per-task.
* :class:`AsyncExecutor` — async context manager. Every :meth:`submit`
  schedules a fresh AnyIO task on the executor's internal task group.
  Cancel granularity is *per-task*: ``Future.cancel()`` propagates to the
  underlying task's :func:`anyio.from_thread.check_cancelled`.

All three propagate :class:`contextvars.Context` to the task, matching
:func:`asyncio.to_thread` / Trio / AnyIO spawn semantics.

The async variants take a caller-owned :class:`localpost.Portal` and run
their internal :class:`anyio.abc.TaskGroup` on its loop. They expose
:meth:`stop` (safe from any thread) for cooperative cancellation.

There is no idle-timeout self-exit for workers in either pool variant —
once spawned, a worker lives until the executor closes. See
``docs/adr/0005-no-idle-timeout-for-worker-pools.md`` for the rationale.
"""

from __future__ import annotations

import contextvars
import functools
import math
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol, Self, cast, final, runtime_checkable

from anyio import (
    CapacityLimiter,
    ClosedResourceError,
    WouldBlock,
    create_task_group,
    to_thread,
)

from localpost._portal import Portal

from ._channel import Channel, ReceiveChannel, SendChannel


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
# WorkerExecutor — channel + plain threads
# --------------------------------------------------------------------------


@final
class WorkerExecutor:
    """Channel-backed worker pool of plain ``threading.Thread``s.

    ``submit`` tries ``put_nowait`` first; on ``WouldBlock`` it spawns a new
    worker (up to ``max_concurrency``) and falls through to a blocking
    ``put`` (which is what backpressures the caller when at the cap).

    Workers all pull from a single shared receiver. Once spawned, a worker
    lives until the executor closes — no idle-timeout self-exit. See
    ``docs/adr/0005-no-idle-timeout-for-worker-pools.md``.
    """

    def __init__(
        self,
        *,
        max_concurrency: float = math.inf,
        backlog: int | None = 0,
        thread_name_prefix: str = "lp-worker",
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0")
        self._max_concurrency = max_concurrency
        self._thread_name_prefix = thread_name_prefix
        self._tx: SendChannel[Task]
        self._rx: ReceiveChannel[Task]
        self._tx, self._rx = Channel.create(capacity=backlog)
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
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
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # Closing tx makes idle workers see EndOfStream; busy workers finish
        # their current task and then see EndOfStream on the next get.
        self._tx.close()
        for t in list(self._workers):
            t.join()
        self._rx.close()

    def submit[**P, R](
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[R]:
        if not self._opened or self._closed:
            raise RuntimeError("WorkerExecutor is not open")
        task = Task(fn, args, kwargs)
        try:
            self._tx.put_nowait(task)
        except WouldBlock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("WorkerExecutor is closed") from None
                if len(self._workers) < self._max_concurrency:
                    self._spawn_worker()
            self._tx.put(task)  # blocks once we're at the cap
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
        for task in self._rx:
            task.run()


# --------------------------------------------------------------------------
# AsyncWorkerExecutor — channel + AnyIO-backed worker threads
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
    shutdown — it cancels the internal task group and closes the channel so
    idle workers wake immediately and active tasks' next ``check_cancelled``
    raises.
    """

    def __init__(
        self,
        *,
        portal: Portal,
        max_concurrency: float = math.inf,
        backlog: int | None = 0,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0")
        self._portal = portal
        self._max_concurrency = max_concurrency
        self._tx: SendChannel[Task]
        self._rx: ReceiveChannel[Task]
        self._tx, self._rx = Channel.create(capacity=backlog)
        self._lock = threading.Lock()
        self._worker_count = 0
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
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # Close the channel first — idle workers wake via EndOfStream.
        self._tx.close()
        # Exit the task group: on clean body it waits for host tasks to finish;
        # on exception AnyIO cancels children automatically.
        await self._tg.__aexit__(exc_type, exc, tb)
        self._rx.close()

    def stop(self) -> None:
        """Cooperatively shut down: close the channel and cancel the internal
        task group. Safe from any thread. Idempotent.

        Idle workers wake via the channel close; active tasks see their next
        :func:`anyio.from_thread.check_cancelled` raise. Tasks that don't poll
        run to natural completion.
        """
        if not self._opened or self._closed:
            return
        # The channel uses thread locks; closing is safe from any thread.
        try:
            self._tx.close()
        except ClosedResourceError:
            pass
        self._portal.run_sync(self._tg.cancel_scope.cancel)

    def submit[**P, R](
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[R]:
        if not self._opened or self._closed:
            raise RuntimeError("AsyncWorkerExecutor is not open")
        task = Task(fn, args, kwargs)
        try:
            self._tx.put_nowait(task)
        except WouldBlock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("AsyncWorkerExecutor is closed") from None
                if self._worker_count < self._max_concurrency:
                    self._spawn_worker()
            self._tx.put(task)
        return task.future

    def _spawn_worker(self) -> None:
        async def host_task() -> None:
            await to_thread.run_sync(self._run_worker, abandon_on_cancel=False)

        # Schedule the host task into our internal task group from any thread.
        self._portal.run_sync(self._tg.start_soon, host_task)
        self._worker_count += 1

    def _run_worker(self) -> None:
        for task in self._rx:
            task.run()


# --------------------------------------------------------------------------
# AsyncExecutor — fresh AnyIO task per submit
# --------------------------------------------------------------------------


@final
class AsyncExecutor:
    """Async context manager. One AnyIO task per :meth:`submit`, executed via
    ``anyio.to_thread.run_sync`` and gated by an
    :class:`anyio.CapacityLimiter` (``math.inf`` = no cap).

    Cancel granularity is *per-task*: calling ``Future.cancel()`` on the
    returned :class:`~concurrent.futures.Future` cancels just that submit's
    AnyIO task; the cancel propagates through ``to_thread.run_sync(...,
    abandon_on_cancel=False)`` into the worker thread's threadlocals so the
    task's next :func:`anyio.from_thread.check_cancelled` raises.

    :meth:`stop` cancels the internal task group, signalling every in-flight
    submit at once.
    """

    def __init__(
        self,
        *,
        portal: Portal,
        max_concurrency: float = math.inf,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0")
        self._portal = portal
        # Constructed eagerly: callers always instantiate inside ``async with`` on the
        # portal's loop (the only loop ``tg.start_soon`` can target via ``portal``), so
        # ``get_async_backend()`` is satisfied. Loop-affinity is set by ``__aenter__``.
        self._tg = create_task_group()
        self._limiter = limiter = CapacityLimiter(max_concurrency)
        self._to_thread = functools.partial(to_thread.run_sync, limiter=limiter)
        self._opened = False
        self._closed = False

    @property
    def portal(self) -> Portal:
        return self._portal

    async def __aenter__(self) -> Self:
        if self._closed:
            raise RuntimeError("AsyncExecutor cannot be reused")
        await self._tg.__aenter__()
        self._opened = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        # Exit the internal task group: clean body waits for in-flight submits;
        # exception body cancels them. No manual tracking needed.
        await self._tg.__aexit__(exc_type, exc, tb)

    def stop(self) -> None:
        """Cancel every in-flight submit by cancelling the internal task group.
        Safe from any thread. Idempotent.
        """
        if not self._opened or self._closed:
            return
        self._portal.run_sync(self._tg.cancel_scope.cancel)

    def submit[**P, R](
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[R]:
        if not self._opened or self._closed:
            raise RuntimeError("AsyncExecutor is not open")
        task = Task(fn, args, kwargs)
        self._portal.run_sync(self._tg.start_soon, self._to_thread, task.run)
        return task.future
