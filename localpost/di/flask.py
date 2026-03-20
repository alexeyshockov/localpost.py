"""Flask integration"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from typing import Any, final

from flask import Flask, Request, g, request

from localpost.di._services import (
    ResolutionContext,
    ServiceRegistry,
    scope,
)


@final
@dataclass(frozen=True, eq=False, slots=True)
class RequestContext(ResolutionContext):
    ctx: ExitStack = field(default_factory=ExitStack)

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        return self.ctx.enter_context(cm)


type WSGIApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]


def propagate_context(wsgi_app: WSGIApp) -> WSGIApp:
    """WSGI middleware that propagates the calling thread's context vars to worker threads.

    Captures the context at the time of wrapping, then runs each request in a fresh copy.
    This makes context vars (e.g. the DI app scope) visible in threaded servers like Cheroot.
    """
    base_ctx = contextvars.copy_context()

    def middleware(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        # Each request gets its own copy so context var changes are isolated per request
        request_ctx = base_ctx.run(contextvars.copy_context)
        return request_ctx.run(wsgi_app, environ, start_response)

    return middleware


def init_app(app: Flask, registry: ServiceRegistry) -> None:
    """Initialize request-scoped DI for a Flask app.

    Opens a RequestContext scope per request. Flask's Request object is automatically
    available for injection in request-scoped services.

    The app-level scope must be managed externally (e.g. wrapped around the WSGI server).
    Use `propagate_context()` to make the app scope visible in threaded servers.
    """
    registry.register(Request, lambda: request, RequestContext)

    @app.before_request
    def _open_request_scope() -> None:
        req_ctx = RequestContext()
        req_ctx.enter(scope(registry, req_ctx))
        g._localpost_req_ctx = req_ctx

    @app.teardown_request
    def _close_request_scope(exc: BaseException | None) -> None:
        req_ctx: RequestContext | None = getattr(g, "_localpost_req_ctx", None)
        if req_ctx is not None:
            req_ctx.ctx.close()
