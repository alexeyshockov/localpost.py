from contextlib import nullcontext

import uvicorn
from anyio import create_task_group

from localpost._utils import AnyEventView
from localpost.hosting._host import ServiceLifetime, service


# Also see /health endpoint in http_app.py example
@service
def uvicorn_server(config: uvicorn.Config):
    if config.should_reload:
        raise ValueError("Uvicorn: reload is not supported")
    elif config.workers > 1:
        raise ValueError("Uvicorn: multiple workers are not supported")

    async def run(sl: ServiceLifetime) -> None:
        server = uvicorn.Server(config)
        server_main_loop = server.main_loop

        async def lf_aware_main_loop():
            sl.set_started()
            await server_main_loop()
            sl.set_shutting_down()

        # Monkey-patch the bound methods on this Server instance: the original
        # ``main_loop`` is wrapped to fire ``set_started`` / ``set_shutting_down``
        # at the right moments, and ``capture_signals`` is replaced with a no-op
        # CM so uvicorn doesn't install its own signal handlers (we want our
        # ``shutdown_on_signal`` middleware to drive shutdown).
        server.main_loop = lf_aware_main_loop  # ty: ignore[invalid-assignment]
        server.capture_signals = nullcontext  # ty: ignore[invalid-assignment]

        async def observe_shutdown(trigger: AnyEventView):
            await trigger.wait()
            server.should_exit = True

        async with create_task_group() as tg:
            tg.start_soon(server.serve, None)
            tg.start_soon(observe_shutdown, sl.shutting_down)

    return run
