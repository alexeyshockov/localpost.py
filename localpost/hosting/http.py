from os import getenv
from typing import Any, Callable, final

import uvicorn
from anyio import create_task_group
from typing_extensions import Self

from localpost._utils import start_task_soon

from ._host import ServiceLifetimeManager


# Also see /health endpoint in http_app.py example
@final
class UvicornService:
    def __init__(self, config: uvicorn.Config):
        self.config = config

    @classmethod
    def for_app(cls, app: Callable[..., Any]) -> Self:
        return cls(
            uvicorn.Config(
                app,
                host=getenv("UVICORN_HOST", "127.0.0.1"),
                port=int(getenv("UVICORN_PORT", "8000")),
                log_config=None,  # Do not touch current logging configuration
            )
        )

    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        server = uvicorn.Server(config=self.config)
        config = self.config
        if not config.loaded:
            config.load()
        server.lifespan = config.lifespan_class(config)

        # It is hard to use server.serve() directly, because it overrides the signal handlers. A possible workaround is
        # to call it in a separate thread, but currently it looks like an overkill.
        # See uvicorn.Server._serve() for the original implementation.
        async def _serve():
            await server.startup()
            service_lifetime.set_started()
            # TODO Log started (host + port)
            await server.main_loop()
            service_lifetime.set_shutting_down()  # TODO Extract the exception, if any
            await server.shutdown()

        async def _observe_shutdown():
            await service_lifetime.shutting_down.wait()
            server.should_exit = True

        async with create_task_group() as tg:
            start_task_soon(tg, _serve)
            start_task_soon(tg, _observe_shutdown)
