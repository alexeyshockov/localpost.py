from __future__ import annotations

import inspect
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final, Protocol, cast, final, get_type_hints


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
            raise RuntimeError(f"No service of type {service_type} is registered")

        # Check if this service belongs to this scope
        if not isinstance(self.scope, descriptor.scope):
            # Delegate to parent scope (the service belongs to an outer scope)
            return self.parent.resolve(service_type)

        # Create the service instance via its factory (returns a CM), and enter it in the scope
        instance = self.scope.enter(descriptor.factory(self))
        # Cache for future resolutions within this scope
        self.services[service_type] = instance
        return instance


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


def _resolve_dep(provider: _ServiceProvider, dep_type: type) -> object:
    """Resolve a dependency, handling well-known DI types specially."""
    if dep_type is ServiceProvider:
        return provider
    if dep_type is ResolutionContext or dep_type is AppContext:
        return provider.scope
    return provider.resolve(dep_type)


def _make_service_factory[T](factory: Callable[..., T | Generator[T]]) -> ServiceFactory[T]:
    """Turn any callable (plain, generator, or already-wired) into a ServiceFactory."""
    deps = _collect_deps(factory)

    if inspect.isgeneratorfunction(factory):

        @contextmanager
        def cm_gen(provider: ServiceProvider) -> Generator[T]:
            kwargs = {name: _resolve_dep(cast(_ServiceProvider, provider), dep_type) for name, dep_type in deps}
            yield from factory(**kwargs)

        return cm_gen

    @contextmanager
    def cm_plain(provider: ServiceProvider) -> Generator[T]:
        kwargs = {name: _resolve_dep(cast(_ServiceProvider, provider), dep_type) for name, dep_type in deps}
        yield cast(T, factory(**kwargs))

    return cm_plain


def _factory_for_type[T](service_type: type[T]) -> ServiceFactory[T]:
    """Create a ServiceFactory that auto-wires a type's constructor."""
    # Classes without a custom __init__ inherit object.__init__(*args, **kwargs) — no deps to wire
    deps = _collect_deps(service_type.__init__, skip_self=True) if "__init__" in service_type.__dict__ else []
    has_close = callable(getattr(service_type, "close", None))

    @contextmanager
    def cm(provider: ServiceProvider) -> Generator[T]:
        kwargs = {name: _resolve_dep(cast(_ServiceProvider, provider), dep_type) for name, dep_type in deps}
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
        @contextmanager
        def value_factory(_: ServiceProvider) -> Generator[T]:
            yield value

        sd = ServiceDescriptor(service_type or type(value), scope or AppContext, value_factory)
        self.descriptors[sd.service_type] = sd

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
