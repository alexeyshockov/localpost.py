from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack, closing
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final, Protocol, cast, final

from localpost._utils import _SupportsClose


class ResolutionContext(Protocol):
    def enter[T](self, cm: AbstractContextManager[T]) -> T: ...


@final
@dataclass(frozen=True, eq=False, slots=True)
class AppContext(ResolutionContext):
    ctx: ExitStack = field(default_factory=ExitStack)

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        return self.ctx.enter_context(cm)


class ServiceProvider(Protocol):
    """Service provider, scoped to the current resolution context."""

    def resolve[T](self, service_type: type[T], /) -> T: ...

    def __getitem__[T](self, service_type: type[T], /) -> T:
        return self.resolve(service_type)

    def enter[T](self, cm: AbstractContextManager[T]) -> T: ...

    def defer(self, resource: _SupportsClose, /):
        return self.enter(closing(resource))


@dataclass(frozen=True, slots=True)
class _ServiceProvider(ServiceProvider):
    parent: ServiceProvider
    registry: ServiceRegistry
    scope: ResolutionContext
    """Scope stack, from outermost to innermost."""
    services: dict[type, object] = field(default_factory=dict)
    """Resolved services, keyed by service type."""

    def resolve[T](self, service_type: type[T], /) -> T:
        return cast(T, None)  # FIXME

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        return self.scope.enter(cm)


class NullServiceProvider(ServiceProvider):
    def resolve[T](self, service_type: type[T], /) -> T:
        raise RuntimeError(f"No service of type {service_type} is registered")

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        raise RuntimeError("No active DI scope")


_provider: ContextVar[ServiceProvider] = ContextVar("_provider")


def current_provider() -> ServiceProvider:
    if provider := _provider.get(None):
        return provider
    raise RuntimeError("No active DI scope")


def scope(self, scope: ResolutionContext, /) -> AbstractContextManager[ServiceProvider]:
    pass  # TODO Implement


class CurrentServiceProvider(ServiceProvider):
    def resolve[T](self, service_type: type[T], /) -> T:
        return current_provider().resolve(service_type)

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        return current_provider().enter(cm)


service_provider: Final[CurrentServiceProvider] = CurrentServiceProvider()


@dataclass(frozen=True, slots=True)
class ServiceDescriptor[T]:
    service_type: type[T]
    scope: type[ResolutionContext]
    factory: Callable[[ServiceProvider], T]


# Kinda like IServiceCollection in .NET, or svcs.Registry
@final
class ServiceRegistry:
    def __init__(self):
        self.descriptors: dict[type, ServiceDescriptor] = {}

    def bind[T](
        self,
        service_type: type[T],
        factory: Callable[[ServiceProvider], T] | None = None,
        scope: type[ResolutionContext] | None = None,
    ) -> None:
        sd = ServiceDescriptor(service_type, scope or AppContext, factory or default_factory_for(service_type))
        self.descriptors[service_type] = sd

    def app_scope(self) -> AbstractContextManager[ServiceProvider]:
        pass  # TODO Implement


def default_factory_for[T](service_type: type[T]) -> Callable[[ServiceProvider], T]:
    pass  # TODO Inspect the type constructor, extract all the params and create a factory function, to bind them from the provider
