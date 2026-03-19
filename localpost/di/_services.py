from __future__ import annotations

import inspect
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final, Protocol, cast, final, get_type_hints

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
        # Return cached instance if already resolved in this scope
        if service_type in self.services:
            return cast(T, self.services[service_type])

        # Look up the descriptor
        descriptor = self.registry.descriptors.get(service_type)
        if descriptor is None:
            # Delegate to parent scope
            return self.parent.resolve(service_type)

        # Check if this service belongs to this scope
        if not isinstance(self.scope, descriptor.scope):
            # Delegate to parent scope (the service belongs to an outer scope)
            return self.parent.resolve(service_type)

        # Create the service instance via its factory
        instance = descriptor.factory(self)
        # Cache for future resolutions within this scope
        self.services[service_type] = instance
        return instance

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        return self.scope.enter(cm)


class NullServiceProvider(ServiceProvider):
    def resolve[T](self, service_type: type[T], /) -> T:
        raise RuntimeError(f"No service of type {service_type} is registered")

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        raise RuntimeError("No active DI scope")


NULL_PROVIDER: Final[ServiceProvider] = NullServiceProvider()


_provider: ContextVar[ServiceProvider] = ContextVar("_provider")


def current_provider() -> ServiceProvider:
    if provider := _provider.get(None):
        return provider
    raise RuntimeError("No active DI scope")


@contextmanager
def scope(registry: ServiceRegistry, ctx: ResolutionContext, /) -> Generator[ServiceProvider]:
    provider = _ServiceProvider(parent=_provider.get(NULL_PROVIDER), registry=registry, scope=ctx)
    contextvar_token = _provider.set(provider)
    try:
        yield provider
    finally:
        _provider.reset(contextvar_token)


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

    def register_value[T](
        self, value: T, service_type: type[T] | None = None, scope: type[ResolutionContext] | None = None
    ) -> None:
        self.register(service_type or type(value), lambda _: value, scope)

    def register[T](
        self,
        service_type: type[T],
        factory: Callable[[ServiceProvider], T] | None = None,
        scope: type[ResolutionContext] | None = None,
    ) -> None:
        sd = ServiceDescriptor(service_type, scope or AppContext, factory or factory_for(service_type))
        self.descriptors[service_type] = sd

    @contextmanager
    def app_scope(self) -> Generator[ServiceProvider]:
        with ExitStack() as ctx, scope(self, AppContext(ctx)) as provider:
            yield provider


def factory_for[T](service_type: type[T]) -> Callable[[ServiceProvider], T]:
    """Inspect the type's __init__ and create a factory that resolves all parameters from the provider."""
    hints = get_type_hints(service_type.__init__)
    params = inspect.signature(service_type).parameters

    # Collect (name, type) for each constructor parameter
    deps: list[tuple[str, type]] = []
    for name, param in params.items():
        if name == "self":
            continue
        if name not in hints:
            raise TypeError(f"Cannot auto-wire {service_type.__name__}: parameter '{name}' has no type annotation")
        deps.append((name, hints[name]))

    def factory(provider: ServiceProvider) -> T:
        kwargs = {name: provider.resolve(dep_type) for name, dep_type in deps}
        return service_type(**kwargs)

    return factory
