from __future__ import annotations

import dataclasses
import logging
import socket
from collections.abc import Awaitable
from contextlib import closing
from wsgiref.types import WSGIApplication

import anyio
from anyio import Event, create_task_group, from_thread, to_thread

from localpost import hosting
from localpost._utils import AnyEventView
from localpost.hosting import ServiceLifetime
from localpost.http._base import (
    BaseServer,
    RequestHandler,
    RoundRobinAcceptor,
    Selector,
    start_http_server,
)
from localpost.http.config import LOGGER_NAME, ServerConfig
from localpost.http.wsgi import wrap_wsgi

__all__ = ["http_server", "wsgi_server"]


def _resolve_ephemeral_port(host: str) -> int:
    """Pin an ephemeral port up-front so all selectors agree on it.

    With ``selectors > 1`` and ``config.port == 0`` each selector would
    bind to a *different* ephemeral, defeating the shared-port design.
    Bind once here, return the assigned port, then close — selectors
    rebind via ``SO_REUSEPORT``. Tiny race window vs other processes
    grabbing the port; same window the existing test fixture accepts.
    """
    with socket.create_server((host, 0)) as probe:
        return probe.getsockname()[1]


async def _periodic_cleanup(period: float, shutdown: AnyEventView, *selectors: Selector) -> None:
    """Drive stale-conn cleanup on every selector at a fixed cadence.

    One coroutine per selector group is enough — the per-selector op queue
    coalesces duplicates, and the sweep itself is cheap when nothing is
    expired. Returns cleanly when ``shutdown`` is set so the surrounding
    task group can finish; the wait is checkpointed every ``period`` seconds
    via ``anyio.move_on_after``.
    """
    while not shutdown.is_set():
        with anyio.move_on_after(period):
            await shutdown.wait()
        if shutdown.is_set():
            return
        for sel in selectors:
            sel.post_cleanup()


def _pick_conn_factory(backend: str):
    """Match :func:`start_http_server`'s backend selection — used by the
    acceptor topology, where we wire :class:`RoundRobinAcceptor` directly
    instead of going through ``start_http_server``."""
    if backend == "h11":
        from localpost.http.server_h11 import HTTPConn  # noqa: PLC0415

        return HTTPConn
    if backend == "httptools":
        try:
            from localpost.http.server_httptools import HTTPConn  # noqa: PLC0415
        except ImportError as e:
            raise ImportError(
                "httptools backend requires the [http-fast] extra (pip install localpost[http-fast])"
            ) from e
        return HTTPConn
    raise ValueError(f"unknown backend {backend!r} (expected 'h11' or 'httptools')")


@hosting.service
def http_server(
    config: ServerConfig,
    handler: RequestHandler,
    /,
    *,
    selectors: int = 1,
    acceptor: bool = False,
):
    """Run an HTTP server inside a hosted service.

    Backend (``h11`` / ``httptools``) is selected via
    :attr:`ServerConfig.backend`. ``handler`` is invoked on the
    **selector thread** for every accepted request — synchronous handlers
    (e.g. a :class:`Router` answering 404 / 405 inline) complete without
    leaving that thread. Handlers that need a worker pool should be
    wrapped with :func:`localpost.http.thread_pool_handler` *before*
    being passed in.

    The service has no request-cancellation machinery of its own.
    Per-request cancellation lives in :func:`localpost.http.check_cancelled`
    and is wired up by :func:`thread_pool_handler` (the only place that
    needs it — selector-thread handlers run to completion synchronously
    and have no thread to signal).

    Args:
        config: Listening / timeouts / buffer-size / backend config.
        handler: Sync request handler. Shared across selector threads when
            ``selectors > 1`` — make sure any state it captures is
            thread-safe (``thread_pool_handler`` already is).
        selectors: Number of selector threads. Default 1.

            With ``acceptor=False`` (default), each selector binds its own
            listening socket on the same address via ``SO_REUSEPORT``;
            the kernel distributes incoming connections. ``config.port == 0``
            is resolved to an ephemeral port once and shared by all
            selectors. Measured impact on standard CPython / macOS is
            flat (GIL holds during parser callbacks + dispatch + channel
            handoff; macOS ``SO_REUSEPORT`` doesn't load-balance like
            Linux). Useful on Linux (kernel-level distribution) and on
            free-threaded builds; see ``benchmarks/http/PERF_FINDINGS.md``
            Phase 7.

            With ``acceptor=True``, ``selectors`` is the number of
            **worker** selectors fed by a single acceptor thread. See
            ``acceptor`` below.
        acceptor: When ``True``, run a dedicated acceptor thread that
            accepts every connection on the listen socket and round-robins
            it to one of ``selectors`` worker selectors via the cross-thread
            op queue. Useful when ``SO_REUSEPORT`` doesn't distribute
            evenly (macOS, free-threaded builds). The acceptor thread does
            no parsing; worker selectors do all I/O for the conns assigned
            to them.
    """
    if selectors < 1:
        raise ValueError(f"selectors must be >= 1 (got {selectors})")

    if acceptor:
        return _run_acceptor(config, handler, workers=selectors)

    if selectors == 1:

        def run_single(lt: ServiceLifetime) -> Awaitable[None]:
            def run_server() -> None:
                with start_http_server(config, handler) as server:
                    lt.set_started()
                    while not lt.shutting_down.is_set():
                        from_thread.check_cancelled()
                        server.run()

            return to_thread.run_sync(run_server)

        return run_single

    # Multi-selector: N independent ``BaseServer`` threads bound to the
    # same address via ``SO_REUSEPORT`` (already enabled in
    # ``_start_http_server``). The kernel hashes incoming SYNs across
    # the listening sockets; established conns stay with one selector for
    # their lifetime. The ``handler`` instance is shared — when wrapped
    # with ``thread_pool_handler`` the underlying channel naturally accepts
    # multiple producers.
    actual_config = (
        dataclasses.replace(config, port=_resolve_ephemeral_port(config.host)) if config.port == 0 else config
    )

    async def run_multi(lt: ServiceLifetime) -> None:
        started: list[Event] = [Event() for _ in range(selectors)]

        def run_one(idx: int) -> None:
            with start_http_server(actual_config, handler) as server:
                from_thread.run_sync(started[idx].set)
                while not lt.shutting_down.is_set():
                    from_thread.check_cancelled()
                    server.run()

        async def wait_started() -> None:
            for ev in started:
                await ev.wait()
            lt.set_started()

        async with create_task_group() as tg:
            tg.start_soon(wait_started)
            for i in range(selectors):
                tg.start_soon(to_thread.run_sync, run_one, i)

    return run_multi


def _run_acceptor(config: ServerConfig, handler: RequestHandler, *, workers: int):
    """Acceptor topology: 1 acceptor thread + N worker-selector threads.

    The acceptor's :class:`BaseServer` runs a :class:`RoundRobinAcceptor`
    as its :data:`ConnHandler`; each accepted client socket is wrapped in
    a :class:`BaseHTTPConn` bound to the next worker :class:`Selector` and
    delivered via :meth:`Selector.post_track` (cross-thread op queue +
    wakeup pipe).
    """
    conn_factory = _pick_conn_factory(config.backend)

    async def run(lt: ServiceLifetime) -> None:
        logger = logging.getLogger(LOGGER_NAME)
        # Bind the listen socket once on the calling (anyio) thread so we
        # can compute ``port`` deterministically before any worker spins
        # up. This mirrors what ``_start_http_server`` does internally.
        listen_sock = socket.create_server(
            (config.host, config.port),
            backlog=config.backlog,
            reuse_port=True,
        )
        port = listen_sock.getsockname()[1]
        logger.info("Serving on %s:%d (acceptor + %d workers)", config.host, port, workers)

        # Build worker selectors first so the acceptor's ConnHandler can
        # capture them. Each worker has its own Selector with its own
        # wakeup pipe / op queue. ``shutting_down`` is per-selector — we
        # toggle them on shutdown.
        worker_selectors = tuple(Selector(config, port=port, logger=logger) for _ in range(workers))
        acceptor_selector = Selector(config, port=port, logger=logger)
        round_robin = RoundRobinAcceptor(
            workers=worker_selectors,
            handler=handler,
            conn_factory=conn_factory,
        )
        acceptor_server = BaseServer(config, round_robin, logger, listen_sock, acceptor_selector)

        worker_started: list[Event] = [Event() for _ in range(workers)]
        acceptor_started = Event()

        def run_worker(idx: int) -> None:
            sel = worker_selectors[idx]
            try:
                from_thread.run_sync(worker_started[idx].set)
                while not lt.shutting_down.is_set():
                    from_thread.check_cancelled()
                    sel.run()
            finally:
                sel.shutdown()
                sel.close()

        def run_acceptor() -> None:
            try:
                from_thread.run_sync(acceptor_started.set)
                while not lt.shutting_down.is_set():
                    from_thread.check_cancelled()
                    acceptor_server.run()
            finally:
                acceptor_server.shutdown()
                acceptor_selector.close()

        async def wait_started() -> None:
            await acceptor_started.wait()
            for ev in worker_started:
                await ev.wait()
            lt.set_started()

        try:
            async with create_task_group() as tg:
                tg.start_soon(wait_started)
                tg.start_soon(
                    _periodic_cleanup,
                    config.select_timeout,
                    lt.shutting_down,
                    acceptor_selector,
                    *worker_selectors,
                )
                tg.start_soon(to_thread.run_sync, run_acceptor)
                for i in range(workers):
                    tg.start_soon(to_thread.run_sync, run_worker, i)
        finally:
            with closing(listen_sock):
                pass

    return run


def wsgi_server(
    config: ServerConfig,
    app: WSGIApplication,
    /,
    *,
    selectors: int = 1,
    acceptor: bool = False,
):
    """Same as :func:`http_server`, but for a WSGI application.

    A WSGI app blocks during request handling (response body iteration is
    synchronous). Wrap with :func:`thread_pool_handler` if you want to
    serve more than one request at a time::

        async with thread_pool_handler(wrap_wsgi(my_app)) as h:
            async with http_server(config, h):
                ...
    """
    return http_server(config, wrap_wsgi(app), selectors=selectors, acceptor=acceptor)
