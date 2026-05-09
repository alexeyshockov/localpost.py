from __future__ import annotations

import dataclasses as dc
import threading
import time
from collections import deque
from collections.abc import Iterator
from typing import Protocol, Self, final, override

from anyio import (
    ClosedResourceError,
    EndOfStream,
    WouldBlock,
)


@final
class Channel[T]:
    @staticmethod
    def create(capacity: int | None = None) -> tuple[SendChannel[T], ReceiveChannel[T]]:
        """Create a channel sender/receiver pair.

        Args:
            capacity: Buffer size. ``None`` means unbounded; ``0`` means rendezvous
                (put blocks until a receiver consumes the item); ``N > 0`` means
                bounded (put blocks while ``len(buffer) >= N``).
        """
        with ChannelState[T](capacity) as state:
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
    #   EndOfStream - all senders closed cleanly and the buffer is drained.
    #   TimeoutError - ``timeout`` elapsed without an item.
    #   ClosedResourceError - this receive channel was already closed.
    def get(self, timeout: float | None = None) -> T: ...

    def get_nowait(self) -> T: ...

    def close(self) -> None: ...


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
    #   TimeoutError - ``timeout`` elapsed without buffer space (or, in rendezvous mode, without
    #       the item being consumed).
    #   ClosedResourceError - this send channel was already closed, or no receivers remain.
    def put(self, item: T, /, timeout: float | None = None) -> None: ...

    def put_nowait(self, item: T, /) -> None: ...

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
    _lock: threading.RLock
    not_empty: threading.Condition
    not_full: threading.Condition

    def __init__(self, capacity: int | None = None):
        if capacity is not None and capacity < 0:
            raise ValueError("capacity must be >= 0 or None")
        self.buffer = deque()
        self.capacity = capacity
        # capacity semantics:
        #   None: unbounded
        #   0: rendezvous — put blocks until its own item is consumed by a receiver; put_nowait
        #      succeeds only if an unclaimed waiting receiver is available. With N waiting
        #      receivers, up to N puts can be in flight concurrently.
        #   Positive int: bounded (put blocks until len(buffer) < capacity)
        self.open_send_channels = 0
        self.open_receive_channels = 0
        self.waiting_receivers = 0
        # Rendezvous bookkeeping (capacity=0 only). ``pending_handoffs`` is a budget counter
        # for buffered items already paired with a waiting receiver; ``items_consumed`` is
        # monotonic and lets a blocking ``put`` detect consumption of its own item even when
        # the buffer holds other in-flight items.
        self.pending_handoffs = 0
        self.items_consumed = 0
        self._lock = threading.RLock()
        self.not_empty = threading.Condition(self._lock)
        self.not_full = threading.Condition(self._lock)

    @property
    def can_put(self) -> bool:
        if self.open_receive_channels == 0:
            raise ClosedResourceError("no more receivers")
        if self.capacity == 0:
            return self.waiting_receivers > self.pending_handoffs or len(self.buffer) == 0
        return self.capacity is None or len(self.buffer) < self.capacity

    @property
    def can_put_nowait(self) -> bool:
        if self.open_receive_channels == 0:
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


def _deadline(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    if timeout < 0:
        raise ValueError("timeout must be >= 0")
    return time.monotonic() + timeout


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - time.monotonic()


@final
class SendChannel[T](BaseSendChannel[T]):
    def __init__(self, state: ChannelState[T]) -> None:
        self._state = state
        self._closed = False

    def __repr__(self) -> str:
        return f"<SendChannel closed={self._closed}>"

    @override
    def clone(self) -> SendChannel[T]:
        with self._state as state:
            if self._closed:
                raise ClosedResourceError("send channel is already closed")
            state.open_send_channels += 1
        return SendChannel(state)

    @override
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
    def put(self, item: T, /, timeout: float | None = None) -> None:
        state = self._state
        deadline = _deadline(timeout)
        my_target = 0
        # Phase 1: wait for buffer space.
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
                        # ``items_consumed`` to (at least) this value, regardless of any
                        # other in-flight items the buffer holds.
                        my_target = state.items_consumed + len(state.buffer)
                    state.not_empty.notify()
                    break
                wait_for = _remaining(deadline)
                if wait_for is not None and wait_for <= 0:
                    raise TimeoutError
                state.not_full.wait(timeout=wait_for)

        # Phase 2 (rendezvous only): wait until *our* item is consumed.
        # On timeout the item stays in the buffer; some later receiver may still consume it.
        if state.capacity == 0:
            while True:
                with state:
                    if self._closed:
                        raise ClosedResourceError("send channel has been closed")
                    if state.items_consumed >= my_target:
                        return  # consumed
                    if state.open_receive_channels == 0:
                        return  # no receivers left
                    wait_for = _remaining(deadline)
                    if wait_for is not None and wait_for <= 0:
                        raise TimeoutError
                    state.not_full.wait(timeout=wait_for)

    @override
    def close(self) -> None:
        with self._state as state:
            if not self._closed:
                self._closed = True
                state.open_send_channels -= 1
                # Broadcast: any sender blocked in put()'s Phase 2 needs to re-check; receivers
                # blocked in get() need to see open_send_channels==0 and raise EndOfStream.
                state.not_empty.notify_all()
                state.not_full.notify_all()


@final
class ReceiveChannel[T](BaseReceiveChannel[T]):
    def __init__(self, state: ChannelState[T]) -> None:
        self._state = state
        self._closed = False

    def __repr__(self) -> str:
        return f"<ReceiveChannel closed={self._closed}>"

    @override
    def clone(self) -> ReceiveChannel[T]:
        with self._state as state:
            if self._closed:
                raise ClosedResourceError("receive channel is already closed")
            state.open_receive_channels += 1
        return ReceiveChannel(state)

    def _take(self, state: ChannelState[T]) -> T:
        item = state.buffer.popleft()
        if state.capacity == 0:
            state.items_consumed += 1
            if state.pending_handoffs > 0:
                state.pending_handoffs -= 1
        state.not_full.notify()
        return item

    @override
    def get_nowait(self) -> T:
        with self._state as state:
            if self._closed:
                raise ClosedResourceError("receive channel has been closed")
            if state.buffer:
                return self._take(state)
            if state.open_send_channels == 0:
                raise EndOfStream("no more senders")
            raise WouldBlock

    @override
    def get(self, timeout: float | None = None) -> T:
        state = self._state
        deadline = _deadline(timeout)
        while True:
            with state:
                if self._closed:
                    raise ClosedResourceError("receive channel has been closed")
                if state.buffer:
                    return self._take(state)
                if state.open_send_channels == 0:
                    raise EndOfStream("no more senders")
                wait_for = _remaining(deadline)
                if wait_for is not None and wait_for <= 0:
                    raise TimeoutError
                state.waiting_receivers += 1
                try:
                    state.not_empty.wait(timeout=wait_for)
                finally:
                    state.waiting_receivers -= 1

    @override
    def close(self) -> None:
        with self._state as state:
            if not self._closed:
                self._closed = True
                state.open_receive_channels -= 1
                # Broadcast: any thread blocked in get() on this rx needs to see _closed and
                # raise; senders blocked in put() need to see open_receive_channels==0.
                state.not_empty.notify_all()
                state.not_full.notify_all()
