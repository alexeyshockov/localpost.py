"""Thread-management primitives for ``localpost.threadtools``.

Three flavors, all sync context managers exposing a ``submit`` method:

* :class:`WorkerExecutor` — channel-backed pool of plain ``threading.Thread``
  workers. Lazy spawn (driven by ``put_nowait``/``WouldBlock``), idle-timeout
  self-exit. Standalone — does not require AnyIO at runtime.
* :class:`AnyIOWorkerExecutor` — same channel/lazy-spawn shape, but workers
  run inside ``anyio.to_thread.run_sync(..., abandon_on_cancel=False)`` via
  a :class:`anyio.from_thread.BlockingPortal`. That populates AnyIO's thread
  locals so user code can call :func:`anyio.from_thread.check_cancelled`.
* :class:`AnyIOExecutor` — every :meth:`submit` schedules a fresh AnyIO task
  on the portal that goes through ``to_thread.run_sync``. No worker reuse.

All three propagate the caller's :class:`contextvars.Context` to the task,
matching the semantics of :func:`asyncio.to_thread` / Trio / AnyIO spawn.
"""

from __future__ import annotations

import contextvars
import dataclasses as dc
import functools
import threading
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import ExitStack
from types import TracebackType
from typing import Any, Protocol, Self, cast, final, runtime_checkable

from anyio import (
    CapacityLimiter,
    ClosedResourceError,
    EndOfStream,
    WouldBlock,
    to_thread,
)
from anyio.from_thread import BlockingPortal, start_blocking_portal

from ._channel import Channel, ReceiveChannel, SendChannel

DEFAULT_IDLE_TIMEOUT: float = 60.0
"""Seconds an idle worker waits for new work before self-exiting."""


@runtime_checkable
class Executor(Protocol):
    """Minimal executor contract — a sync context manager with ``submit``."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def submit[**P, R](
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[R]: ...


@dc.dataclass(slots=True)
class _Envelope:
    future: Future[Any]
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    context: contextvars.Context

    def run(self) -> None:
        if not self.future.set_running_or_notify_cancel():
            return  # Future was cancelled while queued
        try:
            result = self.context.run(self.fn, *self.args, **self.kwargs)
        except BaseException as exc:  # noqa: BLE001
            self.future.set_exception(exc)
        else:
            self.future.set_result(result)


def _make_envelope(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> _Envelope:
    return _Envelope(
        future=Future(),
        fn=fn,
        args=args,
        kwargs=kwargs,
        context=contextvars.copy_context(),
    )


# --------------------------------------------------------------------------
# WorkerExecutor — channel + plain threads
# --------------------------------------------------------------------------


@final
class WorkerExecutor:
    """Channel-backed worker pool of plain ``threading.Thread``s.

    ``submit`` tries ``put_nowait`` first; on ``WouldBlock`` it spawns a new
    worker (up to ``max_concurrency``) and falls through to a blocking
    ``put`` (which is what backpressures the caller when at the cap).

    Workers self-exit after ``idle_timeout`` seconds without work; on exit
    they remove their cloned receiver from ``open_receivers`` under the
    executor's lock so submission and worker bookkeeping stay consistent.
    """

    def __init__(
        self,
        *,
        max_concurrency: int | None = None,
        backlog: int | None = 0,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        thread_name_prefix: str = "lp-worker",
    ) -> None:
        if max_concurrency is not None and max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0 or None")
        if idle_timeout <= 0:
            raise ValueError("idle_timeout must be > 0")
        self._max_concurrency = max_concurrency
        self._idle_timeout = idle_timeout
        self._thread_name_prefix = thread_name_prefix
        self._tx: SendChannel[_Envelope]
        # ``_rx_template`` is kept open for the lifetime of the executor so
        # ``open_receive_channels`` never drops to zero between worker spawns
        # — that would make the next ``put_nowait`` raise ClosedResourceError.
        # Workers always run on a clone; when they retire they close the clone,
        # never the template.
        self._rx_template: ReceiveChannel[_Envelope]
        self._tx, self._rx_template = Channel.create(capacity=backlog)
        self._lock = threading.Lock()
        self._open_receivers: list[ReceiveChannel[_Envelope]] = []
        self._workers: list[threading.Thread] = []
        self._opened = False
        self._closed = False
        self._next_worker_id = 0

    @property
    def open_receivers(self) -> list[ReceiveChannel[_Envelope]]:
        return self._open_receivers

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
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # Closing the sender lets in-flight workers drain whatever is buffered
        # and then observe EndOfStream on their next ``get``.
        self._tx.close()
        for t in list(self._workers):
            t.join()
        # Drop the template once every worker is gone.
        self._rx_template.close()

    def submit[**P, R](
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[R]:
        if not self._opened or self._closed:
            raise RuntimeError("WorkerExecutor is not open")
        env = _make_envelope(fn, args, kwargs)
        try:
            self._tx.put_nowait(env)
        except WouldBlock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("WorkerExecutor is closed") from None
                if self._max_concurrency is None or len(self._open_receivers) < self._max_concurrency:
                    rx = self._rx_template.clone()
                    self._open_receivers.append(rx)
                    self._spawn_worker(rx)
            self._tx.put(env)  # blocks once we're at the cap
        return env.future

    def _spawn_worker(self, rx: ReceiveChannel[_Envelope]) -> None:
        wid = self._next_worker_id
        self._next_worker_id += 1
        t = threading.Thread(
            target=self._run_worker,
            args=(rx,),
            name=f"{self._thread_name_prefix}-{wid}",
            daemon=True,
        )
        self._workers.append(t)
        t.start()

    def _retire(self, rx: ReceiveChannel[_Envelope]) -> None:
        try:
            rx.close()
        except ClosedResourceError:
            pass
        with self._lock:
            if rx in self._open_receivers:
                self._open_receivers.remove(rx)

    def _run_worker(self, rx: ReceiveChannel[_Envelope]) -> None:
        try:
            while True:
                try:
                    env = rx.get(timeout=self._idle_timeout)
                except TimeoutError:
                    # Idle. Close the timeout-vs-submit race under the lock:
                    # if a submitter slipped a task into the channel between
                    # ``get`` returning and us deciding to die, take it.
                    with self._lock:
                        try:
                            env = rx.get_nowait()
                        except (WouldBlock, EndOfStream, ClosedResourceError):
                            if rx in self._open_receivers:
                                self._open_receivers.remove(rx)
                            try:
                                rx.close()
                            except ClosedResourceError:
                                pass
                            return
                except (EndOfStream, ClosedResourceError):
                    return
                env.run()
        finally:
            try:
                rx.close()
            except ClosedResourceError:
                pass


# --------------------------------------------------------------------------
# AnyIOWorkerExecutor — channel + AnyIO-backed worker threads
# --------------------------------------------------------------------------


@final
class AnyIOWorkerExecutor:
    """Like :class:`WorkerExecutor`, but workers run via
    ``anyio.to_thread.run_sync`` so user code can call
    :func:`anyio.from_thread.check_cancelled`.

    Construction is sync; if no ``portal`` is passed the executor opens its
    own via :func:`anyio.from_thread.start_blocking_portal` and tears it
    down at exit. Pass an existing portal when integrating into an outer
    AnyIO event loop.
    """

    def __init__(
        self,
        *,
        max_concurrency: int | None = None,
        backlog: int | None = 0,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        portal: BlockingPortal | None = None,
        backend: str = "asyncio",
    ) -> None:
        if max_concurrency is not None and max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0 or None")
        if idle_timeout <= 0:
            raise ValueError("idle_timeout must be > 0")
        self._max_concurrency = max_concurrency
        self._idle_timeout = idle_timeout
        self._tx: SendChannel[_Envelope]
        self._rx_template: ReceiveChannel[_Envelope]
        self._tx, self._rx_template = Channel.create(capacity=backlog)
        self._lock = threading.Lock()
        # See ``WorkerExecutor`` — the template stays open for the executor's
        # lifetime so the channel never sees ``open_receive_channels == 0``.
        self._open_receivers: list[ReceiveChannel[_Envelope]] = []
        # ``Future``s returned by ``portal.start_task_soon`` — one per host task.
        self._host_tasks: list[Future[None]] = []
        self._external_portal = portal
        self._portal: BlockingPortal | None = None
        self._backend = backend
        self._stack: ExitStack | None = None
        self._opened = False
        self._closed = False

    @property
    def portal(self) -> BlockingPortal:
        if self._portal is None:
            raise RuntimeError("AnyIOWorkerExecutor is not open")
        return self._portal

    @property
    def open_receivers(self) -> list[ReceiveChannel[_Envelope]]:
        return self._open_receivers

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("AnyIOWorkerExecutor cannot be reused")
        self._stack = ExitStack().__enter__()
        if self._external_portal is not None:
            self._portal = self._external_portal
        else:
            self._portal = self._stack.enter_context(start_blocking_portal(backend=self._backend))
        self._opened = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._tx.close()
        # Wait for every worker host task to finish (its ``to_thread.run_sync``
        # returns once the inner loop exits cleanly).
        for f in list(self._host_tasks):
            try:
                f.result()
            except BaseException:  # noqa: BLE001, S110
                pass
        self._rx_template.close()
        if self._stack is not None:
            self._stack.__exit__(exc_type, exc, tb)
            self._stack = None
        self._portal = None

    def submit[**P, R](
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[R]:
        if not self._opened or self._closed:
            raise RuntimeError("AnyIOWorkerExecutor is not open")
        env = _make_envelope(fn, args, kwargs)
        try:
            self._tx.put_nowait(env)
        except WouldBlock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("AnyIOWorkerExecutor is closed") from None
                if self._max_concurrency is None or len(self._open_receivers) < self._max_concurrency:
                    rx = self._rx_template.clone()
                    self._open_receivers.append(rx)
                    self._spawn_worker(rx)
            self._tx.put(env)
        return env.future

    def _spawn_worker(self, rx: ReceiveChannel[_Envelope]) -> None:
        portal = self._portal
        assert portal is not None

        async def host_task() -> None:
            await to_thread.run_sync(self._run_worker, rx, abandon_on_cancel=False)

        f = portal.start_task_soon(host_task)
        self._host_tasks.append(f)

    def _run_worker(self, rx: ReceiveChannel[_Envelope]) -> None:
        try:
            while True:
                try:
                    env = rx.get(timeout=self._idle_timeout)
                except TimeoutError:
                    with self._lock:
                        try:
                            env = rx.get_nowait()
                        except (WouldBlock, EndOfStream, ClosedResourceError):
                            if rx in self._open_receivers:
                                self._open_receivers.remove(rx)
                            try:
                                rx.close()
                            except ClosedResourceError:
                                pass
                            return
                except (EndOfStream, ClosedResourceError):
                    return
                env.run()
        finally:
            try:
                rx.close()
            except ClosedResourceError:
                pass


# --------------------------------------------------------------------------
# AnyIOExecutor — fresh AnyIO task per submit
# --------------------------------------------------------------------------


@final
class AnyIOExecutor:
    """One AnyIO task per :meth:`submit`, executed via
    ``anyio.to_thread.run_sync``.

    Useful when you want every task to be its own AnyIO scope rather than
    reusing a long-lived worker. ``max_concurrency`` is enforced by an
    :class:`anyio.CapacityLimiter`.
    """

    def __init__(
        self,
        *,
        max_concurrency: int | None = None,
        portal: BlockingPortal | None = None,
        backend: str = "asyncio",
    ) -> None:
        if max_concurrency is not None and max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0 or None")
        self._max_concurrency = max_concurrency
        self._external_portal = portal
        self._portal: BlockingPortal | None = None
        self._backend = backend
        self._stack: ExitStack | None = None
        self._limiter: CapacityLimiter | None = None
        self._opened = False
        self._closed = False
        self._inflight: list[Future[Any]] = []
        self._inflight_lock = threading.Lock()

    @property
    def portal(self) -> BlockingPortal:
        if self._portal is None:
            raise RuntimeError("AnyIOExecutor is not open")
        return self._portal

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("AnyIOExecutor cannot be reused")
        self._stack = ExitStack().__enter__()
        if self._external_portal is not None:
            self._portal = self._external_portal
        else:
            self._portal = self._stack.enter_context(start_blocking_portal(backend=self._backend))
        if self._max_concurrency is not None:
            self._limiter = self._portal.call(CapacityLimiter, self._max_concurrency)
        self._opened = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        # Drain in-flight submissions before closing the portal — otherwise the
        # portal's task group will cancel them on shutdown.
        for f in list(self._inflight):
            try:
                f.result()
            except BaseException:  # noqa: BLE001, S110
                pass
        if self._stack is not None:
            self._stack.__exit__(exc_type, exc, tb)
            self._stack = None
        self._portal = None
        self._limiter = None

    def submit[**P, R](
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Future[R]:
        if not self._opened or self._closed:
            raise RuntimeError("AnyIOExecutor is not open")
        portal = self._portal
        assert portal is not None
        ctx = contextvars.copy_context()
        bound = functools.partial(ctx.run, fn, *args, **kwargs)
        limiter = self._limiter

        async def task() -> R:
            if limiter is not None:
                return cast("R", await to_thread.run_sync(bound, limiter=limiter, abandon_on_cancel=False))
            return cast("R", await to_thread.run_sync(bound, abandon_on_cancel=False))

        fut = portal.start_task_soon(task)
        with self._inflight_lock:
            self._inflight.append(fut)
        fut.add_done_callback(self._on_done)
        return fut

    def _on_done(self, fut: Future[Any]) -> None:
        with self._inflight_lock:
            try:
                self._inflight.remove(fut)
            except ValueError:
                pass
