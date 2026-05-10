"""Host as RSGI application — Mode B for Granian deployments.

When you have hosted services beyond just the HTTP app — schedulers,
gRPC servers, custom background workers — and you want Granian as your
process supervisor, :class:`HostRSGIApp` runs the *whole* hosting stack
inside each Granian worker process. The HTTP request handler is
dispatched per-request via Granian's RSGI interface; the rest of the
services run in the background, sharing memory with the handler the
way a normal :func:`localpost.hosting.run_app` deployment does.

Granian's lifecycle hooks drive ours:

- ``__rsgi_init__(loop)`` — enter :func:`localpost.hosting.serve`'s
  context manager. Services start in dependency order; the call
  returns once everything reaches ``started``.
- ``__rsgi__(scope, proto)`` — dispatch the request via the same RSGI
  bridge :func:`localpost.http.to_rsgi` uses.
- ``__rsgi_del__(loop)`` — exit the context manager. Hosting fires
  shutdown for every service and waits for ``stopped`` before
  returning to Granian.

Note that **per-worker side effects** are the user's concern. Granian
spawns N workers; ``services=[heartbeat.service()]`` runs in **each**
worker. Cron-style jobs that should fire once need either
``--workers 1`` or external coordination (DB lock, leader election).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, final

from localpost._utils import wait_all
from localpost.hosting._host import ServiceF, ServiceLifetime, serve
from localpost.http._async_base import AsyncRequestHandler
from localpost.http.rsgi import _dispatch

if TYPE_CHECKING:
    from localpost.openapi.aio.app import HttpAsyncApp

__all__ = ["HostRSGIApp"]


@final
class HostRSGIApp:
    """RSGI application that runs a full hosting lifecycle per Granian worker.

    Args:
        services: Background hosted services to run alongside the HTTP
            handler (scheduled tasks, gRPC servers, anything that returns
            a :data:`localpost.hosting.ServiceF`).
        rsgi_handler: Either an :data:`AsyncRequestHandler` (low-level)
            or an :class:`HttpAsyncApp` (the framework will compile its
            route table internally).
        max_body_size: Cap on the request body. See :func:`to_rsgi`.

    Example::

        from localpost.hosting.rsgi import HostRSGIApp
        from localpost.openapi import HttpAsyncApp
        from localpost.scheduler import every, scheduled_task


        app = HttpAsyncApp()


        @app.get("/")
        async def root() -> str:
            return "ok"


        @scheduled_task(every(seconds=5))
        async def heartbeat() -> None: ...


        rsgi_app = HostRSGIApp(
            services=[heartbeat.service()],
            rsgi_handler=app,
        )

        # granian --interface rsgi --workers 4 myapp:rsgi_app
    """

    __slots__ = (
        "_handler",
        "_lifecycle_task",
        "_max_body_size",
        "_ready",
        "_services",
        "_shutdown_signal",
    )

    def __init__(
        self,
        *,
        services: Sequence[ServiceF],
        rsgi_handler: AsyncRequestHandler | HttpAsyncApp,
        max_body_size: int = 1 << 20,
    ) -> None:
        self._services = tuple(services)
        self._handler = _resolve_handler(rsgi_handler)
        self._max_body_size = max_body_size
        self._ready: asyncio.Event | None = None
        self._shutdown_signal: asyncio.Event | None = None
        self._lifecycle_task: asyncio.Task[None] | None = None

    def __rsgi_init__(self, loop: Any) -> None:
        """Schedule lifecycle startup on Granian's loop.

        Granian calls this **synchronously** (no ``await``) per worker
        process when the worker is set up, so we can't wait for services
        to be ``started`` here. Instead we spawn a long-running
        :meth:`_lifecycle` task that enters :func:`localpost.hosting.serve`
        and holds the lifetime open until :meth:`__rsgi_del__` signals
        shutdown. The first :meth:`__rsgi__` call waits on
        :attr:`_ready` before dispatching, so requests never see a
        half-started host.

        The lifecycle is owned by a single task because anyio's cancel
        scopes (used inside :func:`serve`) must be entered and exited
        by the same task — splitting ``__aenter__`` / ``__aexit__``
        across two would raise ``"different task than it was entered
        in"``.

        We do **not** apply the ``shutdown_on_signal`` middleware:
        Granian owns signal handling; ``__rsgi_del__`` is how shutdown
        reaches us.
        """
        if not self._services:
            return
        self._ready = asyncio.Event()
        self._shutdown_signal = asyncio.Event()
        self._lifecycle_task = loop.create_task(self._lifecycle())

    async def _lifecycle(self) -> None:
        assert self._ready is not None
        assert self._shutdown_signal is not None
        try:
            root = self._services[0] if len(self._services) == 1 else _compose_started(self._services)
            async with serve(root):
                self._ready.set()
                await self._shutdown_signal.wait()
        finally:
            # Always release the gate — if startup failed before we
            # set it, requests pile up; release so they fail fast.
            self._ready.set()

    async def __rsgi__(self, scope: Any, proto: Any) -> None:
        if self._ready is not None and not self._ready.is_set():
            await self._ready.wait()
        await _dispatch(self._handler, self._max_body_size, scope, proto)

    def __rsgi_del__(self, loop: Any) -> None:
        """Signal the lifecycle task to shut down.

        Granian calls this **synchronously** on its loop thread, so
        ``self._shutdown_signal.set()`` runs without scheduling
        cross-thread. The lifecycle task exits its
        ``async with serve(...)`` block — which fires service shutdown
        and waits for ``stopped`` — then this :class:`HostRSGIApp` is
        done.
        """
        signal = self._shutdown_signal
        if signal is not None:
            signal.set()


def _resolve_handler(handler: AsyncRequestHandler | HttpAsyncApp) -> AsyncRequestHandler:
    """Accept either a raw :data:`AsyncRequestHandler` or an
    :class:`HttpAsyncApp` (compile its route handler on demand)."""
    # Lazy import — hosting shouldn't pull openapi at import time.
    from localpost.openapi.aio.app import HttpAsyncApp as _App

    if isinstance(handler, _App):
        return handler._build_async_handler()
    return handler


def _compose_started(services: Sequence[ServiceF]) -> ServiceF:
    """Multi-service composition that fires ``set_started`` on the parent
    once every child reports started.

    Same shape as :func:`_run_many` but suitable for
    :func:`localpost.hosting.serve` consumers — ``serve`` waits for
    ``started`` (or ``stopped``), and bare ``_run_many`` never fires
    its own ``started`` event since it just spawns and waits. Without
    this composition, ``serve(_run_many(...))`` deadlocks. ``run_app``
    avoids the issue because it uses ``run`` (no ``started`` wait), but
    ``HostRSGIApp`` runs the lifetime through ``serve`` so ``started``
    must propagate.
    """

    async def _run(lt: ServiceLifetime) -> None:
        children = [lt.start(svc) for svc in services]

        async def propagate_shutdown() -> None:
            await lt.shutting_down.wait()
            for c in children:
                c.shutdown()

        if children:
            lt.tg.start_soon(propagate_shutdown)
            await wait_all(c.started for c in children)
            lt.set_started()
        await wait_all(c.stopped for c in children)

    return _run
