from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from localpost._utils import Event


def report_started(started: Event, asgi_app: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(asgi_app)
    def asgi_app_wrapper(
        scope: dict[str, Any], receive: Callable[..., Awaitable[Any]], send: Callable[..., Awaitable[Any]]
    ) -> Awaitable[Any]:
        def wrapped_send(message: dict[str, Any]) -> Awaitable[Any]:
            if message["type"] == "lifespan.startup.complete":
                started.set()
            return send(message)

        if scope["type"] == "lifespan":
            return asgi_app(scope, receive, wrapped_send)
        return asgi_app(scope, receive, send)

    return asgi_app_wrapper
