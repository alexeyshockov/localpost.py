import queue
import threading
from contextlib import suppress

import anyio
from anyio import from_thread

CHECK_TIMEOUT: float = 1.0
"""Timeout (seconds) for cancellation checks (e.g. in the server loop)."""


def check_cancelled() -> None:
    with suppress(anyio.NoEventLoopError):
        from_thread.check_cancelled()


def acquire_sem(sem: threading.Semaphore) -> None:
    while not sem.acquire(timeout=CHECK_TIMEOUT):
        check_cancelled()
    return


def pull_queue[T](q: queue.Queue[T] | queue.SimpleQueue[T]) -> T:
    while True:
        check_cancelled()
        try:
            return q.get(timeout=CHECK_TIMEOUT)
        except queue.Empty:
            continue
