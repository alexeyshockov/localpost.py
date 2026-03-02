from __future__ import annotations

import dataclasses as dc
import os
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol, Self, final, override

from anyio import (
    CapacityLimiter,
    ClosedResourceError,
    EndOfStream,
    WouldBlock,
    create_task_group,
    from_thread,
    to_thread,
)

__all__ = [
    "Channel",
    "SendChannel",
    "ReceiveChannel",
    "create_executor",
    "gather",
]

from localpost._utils import NOT_SET, RandomDelay, is_async_callable

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
    """Same interface as threading.Lock, but acquire() is cancellation aware."""

    __slots__ = ("__exit__", "_acquire_restore", "_is_owned", "_release_save", "locked", "release", "source")

    # noinspection PyProtectedMember
    def __init__(self, lock: threading.Lock | threading.RLock | None = None) -> None:
        lock = lock or threading.Lock()
        self.source = lock
        self.release = lock.release
        self.__exit__ = lock.__exit__
        if hasattr(self.source, "locked"):
            self.locked = lock.locked  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        if hasattr(lock, "_release_save"):
            self._release_save = lock._release_save  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        if hasattr(lock, "_acquire_restore"):
            self._acquire_restore = lock._acquire_restore  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        if hasattr(lock, "_is_owned"):
            self._is_owned = lock._is_owned  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]

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
    cond = threading.Condition(lock or CancellableLock(threading.RLock()))  # pyright: ignore[reportArgumentType]
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
    source = threading.BoundedSemaphore(value)
    source._cond = cancellable_condition()  # pyright: ignore[reportAttributeAccessIssue]
    return source


###
# Task Group related
###


@dc.dataclass(eq=False, slots=True)
class FutureResult:
    value: Any = NOT_SET


def gather(*workload: Callable[[], Any] | tuple[Any, ...]) -> Sequence[Any]:
    async def run_all():
        results: list[FutureResult] = []
        async with create_task_group() as tg:
            for item in workload:
                if callable(item):
                    func, args = item, ()
                else:
                    func, *args = item
                res = FutureResult()
                results.append(res)
                tg.start_soon(run_item, res, func, *args)
        return [res.value for res in results]

    async def run_item(res: FutureResult, func: Callable[..., Any], *args: Any) -> None:
        res.value = await (func(*args) if is_async_callable(func) else to_thread.run_sync(func, *args))

    return from_thread.run(run_all)


###
# Thread Pool
###


@dc.dataclass(frozen=True, eq=False, slots=True)
class ExecutorThread[T]:
    func: Callable[[T], None]
    inbox: ReceiveChannel[T]
    stop_at: float | None

    def arun(self, wl: threading.Semaphore, tl: CapacityLimiter) -> Awaitable[None]:
        return to_thread.run_sync(self.run, wl, limiter=tl)

    def run(self, limiter: threading.Semaphore) -> None:
        with self.inbox as work_items:
            for item in work_items:
                try:
                    self.func(item)  # It's AnyIO-managed thread in the end, so exceptions will be propagated
                    if (stop_at := self.stop_at) and (current_time() >= stop_at):
                        return
                finally:
                    limiter.release()


@final
@dc.dataclass(frozen=True, eq=False, slots=True)
class ThreadPoolExecutor[T]:
    _sender: SendChannel[T]
    _limiter: threading.Semaphore
    _start_worker: Callable[[], None]

    def acquire_nowait(self) -> Callable[[T], None] | None:
        """
        Acquire a slot in the executor, get a submit callable for the payload.

        The slot is released when the payload is processed.
        """
        return self._handle if self._limiter.acquire(blocking=False) else None

    def _handle(self, payload: T) -> None:
        try:
            self._sender.put_nowait(payload)
        except WouldBlock:  # No idle workers, start a new one
            self._start_worker()
            self._sender.put(payload)


@asynccontextmanager
async def create_executor[T](
    func: Callable[[T], None],
    max_workers: int = min(32, (os.process_cpu_count() or 1) + 4),
    worker_max_age: int = 60 * 5,  # Seconds after which a thread is stopped
):
    max_age_jitter = RandomDelay((0.0, worker_max_age * 0.2))
    threads_limiter = CapacityLimiter(max_workers)
    work_limiter = cancellable_semaphore(max_workers)
    sender, receiver = Channel[T].create()

    def start_thread() -> None:
        stop_at = current_time() + worker_max_age + max_age_jitter().total_seconds()
        thread = ExecutorThread(func, receiver.clone(), stop_at)
        from_thread.run_sync(exec_tg.start_soon, thread.arun, work_limiter, threads_limiter)

    async with create_task_group() as exec_tg, sender:
        permanent_thread = ExecutorThread(func, receiver, None)
        exec_tg.start_soon(permanent_thread.arun, work_limiter, threads_limiter)
        yield ThreadPoolExecutor[T](sender, work_limiter, start_thread)


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
    #   EndOfStream – if the sender has been closed cleanly, and no more objects are coming. This is not an error
    #       condition.
    #   ClosedResourceError – if you previously closed this ReceiveChannel object.
    #   BrokenResourceError – if something has gone wrong, and the channel is broken.
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
    #   BrokenResourceError – if something has gone wrong, and the channel is broken. For example, you may get this if
    #       the receiver has already been closed.
    #   ClosedResourceError – if you previously closed this SendChannel object, or if another task closes it while
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
    _lock: CancellableLock
    not_empty: threading.Condition
    not_full: threading.Condition

    def __init__(self, capacity: int | None = None):
        self.buffer = deque()
        self.capacity = capacity
        """
        None: unbounded
        0: rendezvous (at most 1 item in-flight, put() blocks until a receiver consumes the item)
        Positive int: bounded (put blocks until len(buffer) < capacity)
        """
        self.open_send_channels = 0
        self.open_receive_channels = 0
        self._lock = CancellableLock(threading.RLock())
        self.not_empty = cancellable_condition(self._lock)
        self.not_full = cancellable_condition(self._lock)

    @property
    def can_put(self) -> bool:
        if self.open_receive_channels == 0:  # TODO Make this state permanent
            raise ClosedResourceError("no more receivers")
        return self.capacity is None or len(self.buffer) < max(self.capacity, 1)

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
            if not state.can_put:
                raise WouldBlock
            state.buffer.append(item)
            state.not_empty.notify()

    @override
    def put(self, item: T, /) -> None:
        # Phase 1: wait for space in the buffer
        while True:
            check_cancelled()
            with self._state as state:
                try:
                    self.put_nowait(item)
                    break
                except WouldBlock:
                    state.not_full.wait()

        # Phase 2 (rendezvous only): wait until the item is consumed
        if self._state.capacity == 0:
            while True:
                with self._state as state:
                    if self._closed:
                        raise ClosedResourceError("send channel has been closed")
                    if len(state.buffer) == 0:
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
        check_cancelled()
        while not self._closed:
            with self._state as state:
                if len(state.buffer) > 0:
                    item = state.buffer.popleft()
                    state.not_full.notify()
                    return item
                if state.open_send_channels == 0:
                    raise EndOfStream("no more senders")
                state.not_empty.wait()
        raise ClosedResourceError("receive channel has been closed")

    @override
    def close(self) -> None:
        with self._state as state:
            if not self._closed:
                self._closed = True
                state.open_receive_channels -= 1
                state.not_empty.notify()  # Wake up threads waiting in get() immediately
