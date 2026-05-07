from __future__ import annotations

import dataclasses as dc
import threading
from collections import deque
from collections.abc import Callable, Iterator
from typing import Protocol, Self, final, override

from anyio import (
    ClosedResourceError,
    EndOfStream,
    WouldBlock,
)

from ._base import _noop_check
from ._sync import CancellableLock, cancellable_condition


@final
class Channel[T]:
    @staticmethod
    def create(
        capacity: int | None = None,
        *,
        check_cancelled: Callable[[], None] = _noop_check,
    ) -> tuple[SendChannel[T], ReceiveChannel[T]]:
        """Create a channel sender/receiver pair.

        Args:
            capacity: Buffer size. None means unbounded, 0 means rendezvous
                (put blocks until a receiver consumes the item), N>0 means bounded.
            check_cancelled: Cancellation probe invoked from blocking lock /
                condition waits. Defaults to a no-op; pass
                ``anyio.from_thread.check_cancelled`` to make the channel
                cancellation-aware from inside an anyio worker thread.
        """
        with ChannelState(capacity, check_cancelled=check_cancelled) as state:
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

    def __init__(self, capacity: int | None = None, *, check_cancelled: Callable[[], None] = _noop_check):
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
        self._lock = CancellableLock(threading.RLock(), check_cancelled=check_cancelled)
        self.not_empty = cancellable_condition(self._lock, check_cancelled=check_cancelled)
        self.not_full = cancellable_condition(self._lock, check_cancelled=check_cancelled)

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
        # Cancellation is observed inside ``state.__enter__`` (cancellable lock
        # acquire) and ``state.not_full.wait`` (cancellable condition).
        while True:
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
        # Cancellation is observed inside ``state.__enter__`` (cancellable lock
        # acquire) and ``state.not_empty.wait`` (cancellable condition).
        state = self._state
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
