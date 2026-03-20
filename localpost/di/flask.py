"""Flask integration"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any, final

from flask import Flask, Request, g, request

from localpost._utils import set_cvar
from localpost.di._services import (
    DefaultServiceProvider,
    ResolutionContext,
    ServiceProvider,
    ServiceRegistry,
    current_provider,
)

_EXT_KEY = "localpost.di.provider"


@final
@dataclass(frozen=True, eq=False, slots=True)
class RequestContext(ResolutionContext):
    ctx: ExitStack = field(default_factory=ExitStack)

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        return self.ctx.enter_context(cm)


def init_app(app: Flask, registry: ServiceRegistry, provider: ServiceProvider) -> None:
    """Initialize DI for a Flask app.

    Opens a RequestContext scope per request. Flask's Request object is automatically
    available for injection in request-scoped services.
    """
    registry.register(Request, lambda: request, RequestContext)
    app.extensions[_EXT_KEY] = (registry, provider)

    @contextmanager
    def flask_req_ctx() -> Generator[None]:
        registry, parent = app.extensions[_EXT_KEY]
        req_scope = RequestContext()
        provider = DefaultServiceProvider(parent, registry, req_scope)
        with req_scope.ctx, set_cvar(current_provider, provider):
            yield

    @app.before_request
    def _open_request_scope() -> None:
        g._localpost_di_scope = ctx = flask_req_ctx()
        ctx.__enter__()

    @app.teardown_request
    def _close_request_scope(exc: BaseException | None) -> None:
        ctx: AbstractContextManager[Any] | None = getattr(g, "_localpost_di_scope", None)
        if ctx is not None:
            ctx.__exit__(type(exc) if exc else None, exc, None)
