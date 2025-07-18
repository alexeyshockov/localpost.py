from collections.abc import Callable
from typing import Any, final

import hypercorn
from sniffio import current_async_library

from .._host import ServiceLifetimeManager


@final
class HypercornService:
    def __init__(self, app: Callable[..., Any], config: hypercorn.Config):
        self.app = app
        self.config = config
        self.name = "hypercorn"

    # See https://hypercorn.readthedocs.io/en/latest/how_to_guides/api_usage.html
    async def __call__(self, service_lifetime: ServiceLifetimeManager) -> None:
        if current_async_library() == "trio":
            from hypercorn.trio import serve
        else:
            from hypercorn.asyncio import serve  # type: ignore[assignment]

        await serve(self.app, self.config, shutdown_trigger=service_lifetime.shutting_down.wait)
