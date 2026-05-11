"""Thread-aware view over :class:`anyio.from_thread.BlockingPortal`.

Holds the loop thread's ident, captured at construction (the call site of
``Portal(...)`` *must* be on the loop thread). Lets callers schedule work on
the portal's loop without having to know whether they're already on it —
``run_sync`` does the right dispatch in either direction, and ``run_async``
adds a clean deadlock guard for blocking awaits.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Awaitable, Callable
from typing import final

from anyio.from_thread import BlockingPortal


@final
class Portal:
    """Wraps :class:`BlockingPortal` with loop-thread awareness.

    Construct on the loop thread (``Portal(portal)`` snapshots the current
    thread's ident as the loop thread).
    """

    def __init__(self, portal: BlockingPortal) -> None:
        self._p = portal
        self._loop_tid = threading.get_ident()

    @property
    def raw(self) -> BlockingPortal:
        """The underlying :class:`BlockingPortal`. Escape hatch for AnyIO
        ecosystem methods (``wrap_async_context_manager``, etc.).
        """
        return self._p

    @property
    def same_thread(self) -> bool:
        """``True`` iff the current thread is the portal's loop thread."""
        return threading.get_ident() == self._loop_tid

    def run_sync[**P, R](
        self,
        fn: Callable[P, R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Run a sync ``fn`` on the portal's loop and return its result.

        On the loop thread the call is direct; off-loop it routes through
        :meth:`BlockingPortal.call`. Either way the call completes
        synchronously from the caller's POV.
        """
        if self.same_thread:
            return fn(*args, **kwargs)
        return self._p.call(functools.partial(fn, **kwargs), *args)

    def run_async[**P, R](
        self,
        fn: Callable[P, Awaitable[R]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Run an async ``fn`` on the portal's loop and block this thread
        until it returns.

        Off-loop only: from the loop thread we'd be blocking the loop while
        waiting for itself to make progress, so :class:`RuntimeError` is
        raised instead of deadlocking.
        """
        if self.same_thread:
            raise RuntimeError("Deadlock: synchronous wait on the portal's loop thread")
        return self._p.start_task_soon(functools.partial(fn, **kwargs), *args).result()
