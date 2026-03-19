"""Flask integration"""

from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from typing import final

from flask import Flask, Request, g, request

from localpost.di._services import (
    AppContext,
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
    """Initialize DI for a Flask app.

    Opens an app-level scope (lazily, on first request) and a request-level scope per request.
    Flask's Request object is automatically available for injection in request-scoped services.
    """
    # Register Flask Request with RequestContext scope
    registry.register(Request, lambda: request, RequestContext)

    app_scope_cm: AbstractContextManager | None = None

    @app.before_request
    def _open_scopes() -> None:
        nonlocal app_scope_cm

        # Lazily open the app scope on first request
        if app_scope_cm is None:
            app_ctx = AppContext()
            app_scope_cm = scope(registry, app_ctx)
            app_scope_cm.__enter__()  # noqa: PLC2801

        # Open a request-level scope (nested under the app scope)
        req_ctx = RequestContext()
        req_scope_cm = scope(registry, req_ctx)
        req_scope_cm.__enter__()  # noqa: PLC2801

        g._localpost_req_ctx = req_ctx
        g._localpost_req_scope_cm = req_scope_cm

    @app.teardown_request
    def _close_request_scope(exc: BaseException | None) -> None:
        req_scope_cm = getattr(g, "_localpost_req_scope_cm", None)
        req_ctx: RequestContext | None = getattr(g, "_localpost_req_ctx", None)
        if req_scope_cm is not None:
            req_scope_cm.__exit__(type(exc) if exc else None, exc, None)
        if req_ctx is not None:
            req_ctx.ctx.close()
