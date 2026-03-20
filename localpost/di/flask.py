"""Flask integration"""

from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from typing import final

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


def init_app(app: Flask, registry: ServiceRegistry) -> None:
    """Initialize request-scoped DI for a Flask app.

    Opens a RequestContext scope per request. Flask's Request object is automatically
    available for injection in request-scoped services.

    The app-level scope must be managed externally (e.g. via Gunicorn hooks).
    """
    registry.register(Request, lambda: request, RequestContext)

    @app.before_request
    def _open_request_scope() -> None:
        req_ctx = RequestContext()
        req_ctx.enter(scope(registry, req_ctx, parent=registry._app_provider))
        g._localpost_req_ctx = req_ctx

    @app.teardown_request
    def _close_request_scope(exc: BaseException | None) -> None:
        req_ctx: RequestContext | None = getattr(g, "_localpost_req_ctx", None)
        if req_ctx is not None:
            req_ctx.ctx.close()
