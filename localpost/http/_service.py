from collections.abc import Awaitable
from wsgiref.types import WSGIApplication

from anyio import CapacityLimiter, from_thread, to_thread

from localpost import hosting
from localpost.hosting import ServiceLifetime
from localpost.http.config import ServerConfig
from localpost.http.server import RequestHandler, start_http_server
from localpost.threadtools import create_executor


@hosting.service
def http_server(config: ServerConfig, handler: RequestHandler, /):
    def run(sl: ServiceLifetime) -> Awaitable[None]:
        def run_forever():
            with start_http_server(config) as server:
                sl.set_started()
                while not sl.shutting_down.is_set():
                    from_thread.check_cancelled()
                    server.run(handler)

        return to_thread.run_sync(run_forever, limiter=CapacityLimiter(1))

    return run


@hosting.service
def wsgi_server(config: ServerConfig, app: WSGIApplication, /):
    async def run(sl: ServiceLifetime):
        def run_forever():
            with start_http_server(config) as server:
                sl.set_started()
                while not sl.shutting_down.is_set():
                    from_thread.check_cancelled()
                    server.run(handler)

        async with create_executor(handle_route) as executor:
            # FIXME handler
            await to_thread.run_sync(run_forever, limiter=CapacityLimiter(1))


