import dataclasses as dc
import queue
import sys
import time
from collections.abc import Callable, Collection
from queue import Queue, SimpleQueue
from typing import Generic, Protocol, TypeVar

from anyio import ClosedResourceError, from_thread

if sys.version_info >= (3, 13):
    from queue import ShutDown
else:

    class ShutDown(Exception):
        pass


T = TypeVar("T", covariant=True)
TC = TypeVar("TC", bound=Collection[object])

CHECK_INTERVAL = 0.1  # How often to check for cancellation or shutdown, seconds


class ReceiveStream(Protocol[T]):
    def receive(self) -> T: ...

    def close(self): ...


@dc.dataclass(eq=False, slots=True)
class Receiver(Generic[T]):
    queue: Queue[T] | SimpleQueue[T]
    closed: bool = False

    def receive(self) -> T:
        def checkpoint():
            from_thread.check_cancelled()
            if self.closed:
                raise ClosedResourceError()

        while True:
            checkpoint()
            try:
                return self.queue.get(timeout=CHECK_INTERVAL)
            except queue.Empty:
                continue

    def close(self):
        self.closed = True


@dc.dataclass(eq=False, slots=True)
class BatchReceiver(Generic[T, TC]):
    queue: Queue[T] | SimpleQueue[T]
    batch_size: int
    batch_window: float
    items_f: Callable[[list[T]], TC]
    closed: bool = False

    def receive(self) -> TC:
        def checkpoint():
            from_thread.check_cancelled()
            if self.closed:
                raise ClosedResourceError()

        def read_batch() -> list[T]:
            batch: list[T] = []
            batch_started_at = time.monotonic()
            batch_age = 0.0
            while (len(batch) < self.batch_size) and (batch_age < self.batch_window):
                checkpoint()
                batch_age = time.monotonic() - batch_started_at
                try:
                    message = self.queue.get(timeout=CHECK_INTERVAL)
                    batch.append(message)
                except queue.Empty:
                    continue
            return batch

        empty_batch: list[T] = []
        while True:
            items = empty_batch
            try:
                items = read_batch()
                if not items:
                    continue
                return self.items_f(items)
            except ShutDown:
                if items:
                    return self.items_f(items)  # Return the last batch first
                raise

    def close(self):
        self.closed = True
