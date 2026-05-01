from __future__ import annotations

import dataclasses
import socket
from collections.abc import Awaitable
from wsgiref.types import WSGIApplication

from anyio import Event, create_task_group, from_thread, to_thread

from localpost import hosting
from localpost.hosting import ServiceLifetime
from localpost.http.config import ServerConfig
from localpost.http.server import RequestHandler, start_http_server
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


@hosting.service
def http_server(
    config: ServerConfig,
    handler: RequestHandler,
    /,
    *,
    selectors: int = 1,
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
        selectors: Number of selector threads. Default 1. With ``> 1``,
            each selector binds its own listening socket on the same
            address via ``SO_REUSEPORT``; the kernel distributes incoming
            connections. ``config.port == 0`` is resolved to an ephemeral
            port once and shared by all selectors. Measured impact on
            standard CPython / macOS is flat (GIL holds during parser
            callbacks + dispatch + channel handoff; macOS
            ``SO_REUSEPORT`` doesn't load-balance like Linux). Useful on
            Linux (kernel-level distribution) and on free-threaded
            builds; see ``benchmarks/http/PERF_FINDINGS.md`` Phase 7.
    """
    if selectors < 1:
        raise ValueError(f"selectors must be >= 1 (got {selectors})")

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
    # ``start_http_server_base``). The kernel hashes incoming SYNs across
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


def wsgi_server(
    config: ServerConfig,
    app: WSGIApplication,
    /,
    *,
    selectors: int = 1,
):
    """Same as :func:`http_server`, but for a WSGI application.

    A WSGI app blocks during request handling (response body iteration is
    synchronous). Wrap with :func:`thread_pool_handler` if you want to
    serve more than one request at a time::

        async with thread_pool_handler(wrap_wsgi(my_app), max_concurrency=8) as h:
            async with http_server(config, h):
                ...
    """
    return http_server(config, wrap_wsgi(app), selectors=selectors)
