from os import getenv
from typing import Any, Callable, cast, final

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
        self.name = "uvicorn"

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

    # It is hard to use server.serve() directly, because it overrides the signal handlers. A possible workaround is
    # to call it in a separate thread, but currently it looks like an overkill.
    # See uvicorn.Server._serve() for the original implementation.
    async def __call__(self, service_lifetime: ServiceLifetimeManager):
        config = self.config
        server = uvicorn.Server(config)

        if config.should_reload:
            raise ValueError("Reload is not supported")
        elif config.workers > 1:
            raise ValueError("Multiple workers are not supported")

        try:
            if not config.loaded:
                config.load()
            server.lifespan = config.lifespan_class(config)
            await server.startup()
        except SystemExit as e:
            service_lifetime.host.exit_code = cast(int, e.code)
            raise e.__context__ if e.__context__ else RuntimeError("Server startup failed") from None

        if not server.started:
            raise RuntimeError("Server did not start")

        async def serve():
            service_lifetime.set_started()
            await server.main_loop()
            service_lifetime.set_shutting_down()
            await server.shutdown()

        async def observe_shutdown():
            await service_lifetime.shutting_down
            server.should_exit = True

        async with create_task_group() as tg:
            start_task_soon(tg, serve)
            start_task_soon(tg, observe_shutdown)
