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

        # Create the service instance via its factory (returns a CM), and enter it in the scope
        instance = self.enter(descriptor.factory(self))
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


# A factory that returns a context manager — the provider enters it to get the instance and manage its lifecycle.
type ServiceFactory[T] = Callable[[ServiceProvider], AbstractContextManager[T]]


@dataclass(frozen=True, slots=True)
class ServiceDescriptor[T]:
    service_type: type[T]
    scope: type[ResolutionContext]
    factory: ServiceFactory[T]


def _collect_deps(factory: Callable[..., object], *, skip_self: bool = False) -> list[tuple[str, type]]:
    """Inspect a callable's parameters and collect (name, type) pairs for auto-wiring."""
    hints = get_type_hints(factory)
    params = inspect.signature(factory).parameters

    factory_name = getattr(factory, "__name__", repr(factory))
    deps: list[tuple[str, type]] = []
    for name in params:
        if skip_self and name == "self":
            continue
        if name not in hints:
            raise TypeError(f"Cannot auto-wire {factory_name}: parameter '{name}' has no type annotation")
        deps.append((name, hints[name]))

    return deps


def _is_sp_factory(factory: Callable[..., object]) -> bool:
    """Check if a callable already accepts a single ServiceProvider parameter (no auto-wiring needed)."""
    hints = get_type_hints(factory)
    params = list(inspect.signature(factory).parameters.values())
    if len(params) == 1:
        param_hint = hints.get(params[0].name)
        return param_hint is ServiceProvider or param_hint is None
    return False


def _make_service_factory[T](factory: Callable[..., T | Generator[T]]) -> ServiceFactory[T]:
    """Turn any callable (plain, generator, or already-wired) into a ServiceFactory."""
    if _is_sp_factory(factory):
        # Already accepts a single ServiceProvider param — just wrap for lifecycle
        if inspect.isgeneratorfunction(factory):
            return contextmanager(factory)

        @contextmanager
        def cm_passthrough(sp: ServiceProvider) -> Generator[T]:
            yield cast(T, factory(sp))

        return cm_passthrough

    # Auto-wire: resolve deps from the provider, then call the factory
    deps = _collect_deps(factory)

    if inspect.isgeneratorfunction(factory):

        @contextmanager
        def cm_gen(provider: ServiceProvider) -> Generator[T]:
            kwargs = {name: provider.resolve(dep_type) for name, dep_type in deps}
            yield from factory(**kwargs)

        return cm_gen

    @contextmanager
    def cm_plain(provider: ServiceProvider) -> Generator[T]:
        kwargs = {name: provider.resolve(dep_type) for name, dep_type in deps}
        yield cast(T, factory(**kwargs))

    return cm_plain


def _factory_for_type[T](service_type: type[T]) -> ServiceFactory[T]:
    """Create a ServiceFactory that auto-wires a type's constructor."""
    # Classes without a custom __init__ inherit object.__init__(*args, **kwargs) — no deps to wire
    deps = _collect_deps(service_type.__init__, skip_self=True) if "__init__" in service_type.__dict__ else []
    has_close = callable(getattr(service_type, "close", None))

    @contextmanager
    def cm(provider: ServiceProvider) -> Generator[T]:
        kwargs = {name: provider.resolve(dep_type) for name, dep_type in deps}
        instance = service_type(**kwargs)
        try:
            yield instance
        finally:
            if has_close:
                instance.close()  # type: ignore[union-attr]

    return cm


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
        factory: Callable[..., T | Generator[T]] | None = None,
        scope: type[ResolutionContext] | None = None,
    ) -> None:
        wrapped = _make_service_factory(factory) if factory else _factory_for_type(service_type)
        sd = ServiceDescriptor(service_type, scope or AppContext, wrapped)
        self.descriptors[service_type] = sd

    @contextmanager
    def app_scope(self) -> Generator[ServiceProvider]:
        with ExitStack() as ctx, scope(self, AppContext(ctx)) as provider:
            yield provider
