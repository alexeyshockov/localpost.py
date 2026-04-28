from __future__ import annotations

from collections.abc import Awaitable
from wsgiref.types import WSGIApplication

from anyio import from_thread, to_thread

from localpost import hosting
from localpost.hosting import ServiceLifetime
from localpost.http.config import ServerConfig
from localpost.http.server import RequestHandler, start_http_server
from localpost.http.wsgi import wrap_wsgi

__all__ = ["http_server", "wsgi_server"]


@hosting.service
def http_server(config: ServerConfig, handler: RequestHandler, /):
    """Run an HTTP server inside a hosted service.

    ``handler`` is invoked on the **selector thread** for every accepted
    request — synchronous handlers (e.g. a :class:`Router` answering 404 /
    405 inline) complete without leaving that thread. Handlers that need
    a worker pool should be wrapped with
    :func:`localpost.http.thread_pool_handler` *before* being passed in.

    The service has no request-cancellation machinery of its own.
    Per-request cancellation lives in :func:`localpost.http.check_cancelled`
    and is wired up by :func:`thread_pool_handler` (the only place that
    needs it — selector-thread handlers run to completion synchronously
    and have no thread to signal).
    """

    def run(lt: ServiceLifetime) -> Awaitable[None]:
        def run_server() -> None:
            with start_http_server(config, handler) as server:
                lt.set_started()
                while not lt.shutting_down.is_set():
                    from_thread.check_cancelled()
                    server.run()

        return to_thread.run_sync(run_server)

    return run


def wsgi_server(config: ServerConfig, app: WSGIApplication, /):
    """Same as :func:`http_server`, but for a WSGI application.

    A WSGI app blocks during request handling (response body iteration is
    synchronous). Wrap with :func:`thread_pool_handler` if you want to
    serve more than one request at a time::

        async with thread_pool_handler(wrap_wsgi(my_app), max_concurrency=8) as h:
            async with http_server(config, h):
                ...
    """
    return http_server(config, wrap_wsgi(app))
