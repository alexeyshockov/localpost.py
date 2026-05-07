from __future__ import annotations

import threading
from collections.abc import Callable
from typing import final

from ._base import CHECK_TIMEOUT, _noop_check, current_time


@final
class CancellableLock:
    """Same interface as threading.Lock, but acquire() is cancellation aware.

    The class deliberately mirrors a few CPython-private attributes from
    ``threading.RLock`` (``_release_save``, ``_acquire_restore``, ``_is_owned``)
    so that ``threading.Condition(lock=CancellableLock(...))`` accepts it
    transparently — the stdlib's ``Condition`` does its own duck-typing on
    these names. This is intentional, not a leaky abstraction.
    """

    __slots__ = (
        "__exit__",
        "_acquire_restore",
        "_check_cancelled",
        "_is_owned",
        "_release_save",
        "locked",
        "release",
        "source",
    )

    def __init__(
        self,
        lock: threading.Lock | threading.RLock | None = None,
        *,
        check_cancelled: Callable[[], None] = _noop_check,
    ) -> None:
        lock = lock or threading.Lock()
        self.source = lock
        self.release = lock.release
        self.__exit__ = lock.__exit__
        self._check_cancelled = check_cancelled
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
                self._check_cancelled()
            return True
        # Finite timeout — respect the deadline
        deadline = current_time() + timeout
        while (remaining := deadline - current_time()) > 0:
            if self.source.acquire(timeout=min(CHECK_TIMEOUT, remaining)):
                return True
            self._check_cancelled()
        return False

    __enter__ = acquire


def cancellable_condition(
    lock: CancellableLock | None = None,
    *,
    check_cancelled: Callable[[], None] = _noop_check,
) -> threading.Condition:
    # ``Condition`` accepts any duck-typed lock with the right private attrs;
    # ``CancellableLock`` mirrors them (see its docstring).
    # Note: ``check_cancelled`` controls the condition's ``wait`` loop only.
    # The lock keeps whatever probe it was constructed with — pass a lock built
    # with the same callable if you want a single coherent policy.
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


def cancellable_semaphore(
    value: int = 1, *, check_cancelled: Callable[[], None] = _noop_check
) -> threading.BoundedSemaphore:
    # ``Semaphore`` / ``BoundedSemaphore`` use a private ``_cond`` for blocking
    # waits; swap it for our cancellable variant so the semaphore's blocking
    # ``acquire`` becomes cancellation-aware.
    source = threading.BoundedSemaphore(value)
    source._cond = cancellable_condition(check_cancelled=check_cancelled)  # type: ignore
    return source
