"""``run_async`` — bridge from a sync (worker) thread back into the loop.

Mirrors :func:`anyio.to_thread.run_sync` in reverse. The dispatch goes to the
current service's :class:`anyio.from_thread.BlockingPortal`, so the loop is
the one that hosts the running service — no need to thread the portal through
manually.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable


def run_async[**P, R](
    func: Callable[P, Awaitable[R]],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Run an async ``func`` on the current service's loop and return its result.

    Counterpart to :func:`anyio.to_thread.run_sync`: call from a worker thread
    to dispatch back into the event loop. Resolves the portal via
    :func:`localpost.hosting.current_service`, so the caller's
    :class:`contextvars.Context` must carry the hosting context (true for any
    thread spawned through AnyIO / the threadtools executors).

    Must be called from a non-loop thread; the underlying
    :meth:`anyio.from_thread.BlockingPortal.call` raises ``RuntimeError`` if
    invoked from the loop thread.
    """
    # Local import: ``localpost.hosting`` pulls in ``localpost.http``, which in
    # turn imports ``localpost.threadtools`` — a top-level import here would
    # close the cycle.
    from localpost.hosting import current_service  # noqa: PLC0415

    portal = current_service().portal
    return portal.call(functools.partial(func, *args, **kwargs))
