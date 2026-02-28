from collections.abc import Awaitable

import hypercorn
from sniffio import current_async_library

from localpost.hosting._host import ServiceF, ServiceLifetime, service
from localpost.hosting.services._asgi import report_started


@service
def hypercorn_server(app, config: hypercorn.Config, /) -> ServiceF:
    def run(sl: ServiceLifetime) -> Awaitable[None]:
        # See https://hypercorn.readthedocs.io/en/latest/how_to_guides/api_usage.html
        if current_async_library() == "trio":
            from hypercorn.trio import serve
        else:
            from hypercorn.asyncio import serve  # type: ignore[assignment]
        observed_app = report_started(sl.started, app)
        return serve(observed_app, config, shutdown_trigger=sl.shutting_down.wait)

    return run
