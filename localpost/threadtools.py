from __future__ import annotations

import dataclasses as dc
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from typing import Any, Protocol, Self, final, override

from anyio import (
    ClosedResourceError,
    EndOfStream,
    WouldBlock,
    from_thread,
)

__all__ = [
    "Channel",
    "ReceiveChannel",
    "SendChannel",
    "ThreadTaskGroup",
]

CHECK_TIMEOUT: float = 1.0
"""Timeout (seconds) for cancellation checks (e.g. in the server loop)."""

# Alias, so it's possible to override if needed
check_cancelled = from_thread.check_cancelled

current_time = time.monotonic


###
# Synchronization primitives
###


@final
class CancellableLock:
    """Same interface as threading.Lock, but acquire() is cancellation aware.

    The class deliberately mirrors a few CPython-private attributes from
    ``threading.RLock`` (``_release_save``, ``_acquire_restore``, ``_is_owned``)
    so that ``threading.Condition(lock=CancellableLock(...))`` accepts it
    transparently — the stdlib's ``Condition`` does its own duck-typing on
    these names. This is intentional, not a leaky abstraction.
    """

    __slots__ = ("__exit__", "_acquire_restore", "_is_owned", "_release_save", "locked", "release", "source")

    def __init__(self, lock: threading.Lock | threading.RLock | None = None) -> None:
        lock = lock or threading.Lock()
        self.source = lock
        self.release = lock.release
        self.__exit__ = lock.__exit__
        if hasattr(self.source, "locked"):
            self.locked = lock.locked  # type: ignore
        if hasattr(lock, "_release_save"):
            self._release_save = lock._release_save  # type: ignore
        if hasattr(lock, "_acquire_restore"):
            self._acquire_restore = lock._acquire_restore  # type: ignore
        if hasattr(lock, "_is_owned"):
            self._is_owned = lock._is_owned  # type: ignore

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        if not blocking:
            return self.source.acquire(blocking=False)
        if timeout is None or timeout < 0:
            # No timeout — loop until acquired, checking for cancellation
            while not self.source.acquire(timeout=CHECK_TIMEOUT):
                check_cancelled()
            return True
        # Finite timeout — respect the deadline
        deadline = current_time() + timeout
        while (remaining := deadline - current_time()) > 0:
            if self.source.acquire(timeout=min(CHECK_TIMEOUT, remaining)):
                return True
            check_cancelled()
        return False

    __enter__ = acquire


def cancellable_condition(lock: CancellableLock | None = None) -> threading.Condition:
    # ``Condition`` accepts any duck-typed lock with the right private attrs;
    # ``CancellableLock`` mirrors them (see its docstring).
    cond = threading.Condition(lock or CancellableLock(threading.RLock()))  # type: ignore
    orig_wait = cond.wait

    def cancellable_wait(timeout: float | None = None) -> bool:
        if timeout is None or timeout < 0:
            while True:
                check_cancelled()
                if orig_wait(CHECK_TIMEOUT):
                    return True

        end_time = current_time() + timeout
        while True:
            now = current_time()
            if now >= end_time:
                return False
            check_cancelled()
            remaining = min(CHECK_TIMEOUT, max(0.0, end_time - now))
            if orig_wait(remaining):
                return True

    cond.wait = cancellable_wait  # type: ignore
    return cond


def cancellable_semaphore(value: int = 1) -> threading.BoundedSemaphore:
    # ``Semaphore`` / ``BoundedSemaphore`` use a private ``_cond`` for blocking
    # waits; swap it for our cancellable variant so the semaphore's blocking
    # ``acquire`` becomes cancellation-aware.
    source = threading.BoundedSemaphore(value)
    source._cond = cancellable_condition()  # type: ignore
    return source


###
# Channels
###


@final
class Channel[T]:
    @staticmethod
    def create(capacity: int | None = None) -> tuple[SendChannel[T], ReceiveChannel[T]]:
        """Create a channel sender/receiver pair.

        Args:
            capacity: Buffer size. None means unbounded, 0 means rendezvous
                (put blocks until a receiver consumes the item), N>0 means bounded.
        """
        with ChannelState(capacity) as state:
            state.open_send_channels += 1
            tx = SendChannel(state)
            state.open_receive_channels += 1
            rx = ReceiveChannel(state)
            return tx, rx


class BaseReceiveChannel[T](Protocol):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def __iter__(self) -> Iterator[T]:
        while True:
            try:
                yield self.get()
            except EndOfStream:
                break

    def clone(self) -> ReceiveChannel[T]: ...

    # Raises:
    #   EndOfStream - if the sender has been closed cleanly, and no more objects are coming. This is not an error
    #       condition.
    #   ClosedResourceError - if you previously closed this ReceiveChannel object.
    #   BrokenResourceError - if something has gone wrong, and the channel is broken.
    def get(self) -> T: ...

    def close(self): ...


class BaseSendChannel[T](Protocol):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def clone(self) -> SendChannel[T]: ...

    # Raises:
    #   BrokenResourceError - if something has gone wrong, and the channel is broken. For example, you may get this if
    #       the receiver has already been closed.
    #   ClosedResourceError - if you previously closed this SendChannel object, or if another task closes it while
    #       put() is running.
    def put(self, item: T, /) -> None: ...

    def close(self) -> None: ...


@final
@dc.dataclass(slots=True)
class ChannelState[T]:
    buffer: deque[T]
    capacity: int | None
    open_send_channels: int
    open_receive_channels: int
    waiting_receivers: int
    pending_handoffs: int
    items_consumed: int
    _lock: CancellableLock
    not_empty: threading.Condition
    not_full: threading.Condition

    def __init__(self, capacity: int | None = None):
        if capacity is not None and capacity < 0:
            raise ValueError("capacity must be >= 0 or None")
        self.buffer = deque()
        self.capacity = capacity
        """
        None: unbounded
        0: rendezvous — put blocks until its own item is consumed by a receiver; put_nowait succeeds
            only if an unclaimed waiting receiver is available. With N waiting receivers, up to N
            puts can be in flight concurrently.
        Positive int: bounded (put blocks until len(buffer) < capacity)
        """
        self.open_send_channels = 0
        self.open_receive_channels = 0
        self.waiting_receivers = 0
        # Rendezvous bookkeeping (capacity=0 only). ``pending_handoffs`` is a budget counter for
        # buffered items already paired with a waiting receiver; ``items_consumed`` is monotonic
        # and lets a blocking ``put`` detect consumption of its own item even when the buffer
        # holds other in-flight items.
        self.pending_handoffs = 0
        self.items_consumed = 0
        self._lock = CancellableLock(threading.RLock())
        self.not_empty = cancellable_condition(self._lock)
        self.not_full = cancellable_condition(self._lock)

    @property
    def can_put(self) -> bool:
        if self.open_receive_channels == 0:  # TODO Make this state permanent
            raise ClosedResourceError("no more receivers")
        if self.capacity == 0:
            return self.waiting_receivers > self.pending_handoffs or len(self.buffer) == 0
        return self.capacity is None or len(self.buffer) < self.capacity

    @property
    def can_put_nowait(self) -> bool:
        if self.open_receive_channels == 0:  # TODO Make this state permanent
            raise ClosedResourceError("no more receivers")
        if self.capacity == 0:
            return self.waiting_receivers > self.pending_handoffs
        return self.capacity is None or len(self.buffer) < self.capacity

    def __enter__(self) -> Self:
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.open_send_channels == 0 or self.open_receive_channels == 0:
            self.not_empty.notify_all()
            self.not_full.notify_all()
        self._lock.release()


@final
# TODO dataclass + repr
class SendChannel[T](BaseSendChannel[T]):
    def __init__(self, state: ChannelState[T]) -> None:
        self._state = state
        self._closed = False

    @override
    def clone(self):
        with self._state as state:
            if self._closed:
                raise ClosedResourceError("send channel is already closed")
            state.open_send_channels += 1
        return SendChannel(state)

    def put_nowait(self, item: T, /) -> None:
        with self._state as state:
            if self._closed:
                raise ClosedResourceError("send channel has been closed")
            if not state.can_put_nowait:
                raise WouldBlock
            state.buffer.append(item)
            if state.capacity == 0:
                # ``can_put_nowait`` already gated on ``waiting_receivers > pending_handoffs``,
                # so this is always a real claim.
                state.pending_handoffs += 1
            state.not_empty.notify()

    @override
    def put(self, item: T, /) -> None:
        state = self._state
        my_target = 0
        # Phase 1: wait for space in the buffer.
        # Body of ``put_nowait`` is inlined here to avoid re-entering the lock
        # and to skip the ``WouldBlock`` exception on the contended path.
        while True:
            check_cancelled()
            with state:
                if self._closed:
                    raise ClosedResourceError("send channel has been closed")
                if state.can_put:
                    state.buffer.append(item)
                    if state.capacity == 0:
                        if state.waiting_receivers > state.pending_handoffs:
                            state.pending_handoffs += 1
                        # Snapshot a target for Phase 2: consumption of *our* item bumps
                        # ``items_consumed`` to (at least) this value, regardless of any other
                        # in-flight items the buffer holds.
                        my_target = state.items_consumed + len(state.buffer)
                    state.not_empty.notify()
                    break
                state.not_full.wait()

        # Phase 2 (rendezvous only): wait until *our* item is consumed
        if state.capacity == 0:
            while True:
                with state:
                    if self._closed:
                        raise ClosedResourceError("send channel has been closed")
                    if state.items_consumed >= my_target:
                        return  # Consumed
                    if state.open_receive_channels == 0:
                        return  # No receivers left
                    state.not_full.wait()
                check_cancelled()

    def close(self) -> None:
        with self._state as state:
            if not self._closed:
                self._closed = True
                state.open_send_channels -= 1
                state.not_full.notify()  # Wake up threads waiting in put() immediately


@final
# TODO dataclass + repr
class ReceiveChannel[T](BaseReceiveChannel[T]):
    def __init__(self, state: ChannelState[T]) -> None:
        self._state = state
        self._closed = False

    @override
    def clone(self):
        with self._state as state:
            if self._closed:
                raise ClosedResourceError("receive channel is already closed")
            state.open_receive_channels += 1
        return ReceiveChannel(state)

    @override
    def get(self) -> T:
        state = self._state
        check_cancelled()
        while not self._closed:
            with state:
                if state.buffer:
                    item = state.buffer.popleft()
                    if state.capacity == 0:
                        state.items_consumed += 1
                        if state.pending_handoffs > 0:
                            state.pending_handoffs -= 1
                    state.not_full.notify()
                    return item
                if state.open_send_channels == 0:
                    raise EndOfStream("no more senders")
                state.waiting_receivers += 1
                try:
                    state.not_empty.wait()
                finally:
                    state.waiting_receivers -= 1
        raise ClosedResourceError("receive channel has been closed")

    @override
    def close(self) -> None:
        with self._state as state:
            if not self._closed:
                self._closed = True
                state.open_receive_channels -= 1
                state.not_empty.notify()  # Wake up threads waiting in get() immediately


###
# ThreadTaskGroup
###

_IDLE_TIMEOUT: float = 60.0
"""Seconds an idle worker waits for new work before self-exiting."""


@dc.dataclass(slots=True)
class _Task:
    future: Future[Any]
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    group: ThreadTaskGroup

    def run(self) -> None:
        try:
            try:
                result = self.fn(*self.args, **self.kwargs)
            except BaseException as exc:  # noqa: BLE001 — Trio-style: capture everything
                self.future.set_exception(exc)
                self.group._record_error(exc)
            else:
                self.future.set_result(result)
        finally:
            self.group._task_done()


@final
class _Worker:
    """Idle-tracked worker thread with a per-worker inbox.

    Lifecycle: ``alive`` until the thread self-exits after ``_IDLE_TIMEOUT``
    seconds with no work, after which the worker becomes a tombstone in the
    global ``_idle`` deque. The dispatcher detects tombstones via
    :meth:`submit` returning ``False`` and pops the next worker / spawns a
    fresh one.
    """

    __slots__ = ("_alive", "_cv", "_inbox")

    def __init__(self) -> None:
        self._inbox: deque[_Task] = deque()
        self._cv = threading.Condition(threading.Lock())
        # Set under ``_cv`` before lock release in ``_run``; that ordering is
        # what makes ``submit`` race-free vs ``Thread.is_alive()``, which can
        # still report ``True`` after ``_run`` has released the lock but
        # before CPython's bootstrap marks the thread stopped.
        self._alive = True
        threading.Thread(target=self._run, daemon=True, name="localpost-worker").start()

    def submit(self, task: _Task) -> bool:
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
                    if not self._cv.wait(timeout=_IDLE_TIMEOUT):
                        # Timed out. One more inbox check under the lock to
                        # close the timeout-vs-notify race.
                        if not self._inbox:
                            self._alive = False
                            return
                        break
                task = self._inbox.popleft()
            task.run()
            _idle.append(self)


_idle: deque[_Worker] = deque()
"""Global LIFO stack of idle workers, shared across all ``ThreadTaskGroup``s."""


@final
class ThreadTaskGroup:
    """Trio-style task group running sync callables on a shared thread pool.

    Tasks submitted via :meth:`start_soon` run on a process-wide pool of
    worker threads. Workers are spawned on demand, reused across all
    ``ThreadTaskGroup`` instances, and self-exit after 60 s of idleness.
    There is no concurrency cap — ``start_soon`` always succeeds (modulo
    OS thread limits).

    Lifetime: sync context manager. On exit, blocks until every task
    started inside the ``with`` block has finished, then re-raises any
    task exceptions wrapped in an :class:`ExceptionGroup` (Trio
    ``strict_exception_groups=True`` semantics — a body exception and
    task exceptions are merged into one group).

    Example::

        with ThreadTaskGroup() as tg:
            fut = tg.start_soon(do_work, arg)
            # ...
        # On exit: drains in-flight tasks; raises ExceptionGroup if any failed.

    ``start_soon`` is callable from any thread, including from inside a
    task running on the same group (recursive spawn). It returns a
    :class:`concurrent.futures.Future` that captures the task's result
    or exception. Reading the future is optional — a task exception is
    surfaced via the ``ExceptionGroup`` raised at ``__exit__`` even if
    the future is discarded.
    """

    __slots__ = ("_closed", "_cv", "_errors", "_lock", "_name", "_pending")

    def __init__(self, *, name: str | None = None) -> None:
        self._name = name
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
            raise RuntimeError("ThreadTaskGroup cannot be reused")
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
            label = f"ThreadTaskGroup {self._name!r} failed" if self._name else "ThreadTaskGroup failed"
            # ``BaseExceptionGroup(...)`` returns ``ExceptionGroup`` when every
            # member is an ``Exception`` subclass, ``BaseExceptionGroup`` otherwise
            # — matches Trio semantics for ``KeyboardInterrupt`` / ``SystemExit``.
            raise BaseExceptionGroup(label, all_errors)

    def start_soon[**P, R](
        self, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> Future[R]:
        """Submit ``fn(*args, **kwargs)`` to a worker thread. Returns a future."""
        with self._lock:
            if self._closed:
                raise RuntimeError("ThreadTaskGroup is closed")
            self._pending += 1
        fut: Future[R] = Future()
        task = _Task(fut, fn, args, kwargs, self)
        while True:
            try:
                w = _idle.pop()
            except IndexError:
                # No idle worker — spawn a fresh one. ``submit`` on a fresh
                # worker is guaranteed to succeed (``_alive=True``).
                _Worker().submit(task)
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
