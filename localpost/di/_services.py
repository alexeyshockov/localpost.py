from __future__ import annotations

import inspect
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from dataclasses import dataclass as define
from functools import partial
from typing import Any, Final, Protocol, Self, cast, final, get_type_hints

from localpost._utils import set_cvar


class ResolutionContext(Protocol):
    def enter[T](self, cm: AbstractContextManager[T]) -> T: ...


@final
@define(frozen=True, eq=False, slots=True)
class AppContext(ResolutionContext):
    ctx: ExitStack = field(default_factory=ExitStack)

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        return self.ctx.enter_context(cm)


class ServiceProvider(Protocol):
    """Service provider, scoped to the current resolution context."""

    def create[T](self, target_type: type[T], /, **kwargs: Any) -> T:
        """Create an instance of the given type by calling its constructor, resolving any dependencies."""

    def resolve[T](self, service_type: type[T], /) -> T:
        """Create an instance (or take already created one) for the service type, resolving any dependencies."""

    def __getitem__[T](self, service_type: type[T], /) -> T:
        return self.resolve(service_type)


class ServiceNotRegisteredError(ValueError):
    """Raised when resolving a service that is not registered."""


@final
@define(frozen=True, slots=True)
class DefaultServiceProvider(ServiceProvider):
    parent: ServiceProvider
    registry: ServiceRegistry
    scope: ResolutionContext
    """Scope stack, from outermost to innermost."""
    services: dict[type, object] = field(default_factory=dict)
    """Resolved services, keyed by service type."""

    @classmethod
    def from_current(cls, scope: ResolutionContext) -> Self:
        if parent := current_provider.get(None):
            return cls(parent=parent, registry=parent.registry, scope=scope)
        raise RuntimeError("No active DI scope")  # Did you forget to enter the app scope?

    def create[T](self, target_type: type[T], /, **kwargs: Any) -> T:
        """Create an instance of the given type, resolving constructor deps not provided in kwargs."""
        deps = _collect_deps(target_type) if "__init__" in target_type.__dict__ else []
        resolved = {name: self.resolve(dep_type) for name, dep_type in deps if name not in kwargs}
        return target_type(**resolved, **kwargs)

    def resolve[T](self, service_type: type[T], /) -> T:
        # Well-known DI types
        if service_type is ServiceProvider:
            return cast(T, self)
        if service_type is ResolutionContext or service_type is AppContext:
            return cast(T, self.scope)

        # Return cached instance if already resolved in this scope
        if service_type in self.services:
            return cast(T, self.services[service_type])

        # Look up the descriptor for this scope
        descriptor = self.registry.descriptors.get((service_type, type(self.scope)))
        if descriptor is not None:
            instance = self.services[service_type] = self.scope.enter(descriptor.factory(self))
            return instance

        # Delegate to parent scope
        return self.parent.resolve(service_type)


class NullServiceProvider(ServiceProvider):
    def create[T](self, target_type: type[T], /, **kwargs: Any) -> T:
        raise RuntimeError("No active DI scope")

    def resolve[T](self, service_type: type[T], /) -> T:
        raise ServiceNotRegisteredError(f"{service_type} is not registered")

    def enter[T](self, cm: AbstractContextManager[T]) -> T:
        raise RuntimeError("not in DI scope")


@contextmanager
def scope(provider: DefaultServiceProvider, /) -> Generator[ServiceProvider]:
    with set_cvar(current_provider, provider):
        # Eagerly resolve services marked with create_on_enter for this scope type
        scope_type = type(provider.scope)
        for desc in provider.registry.descriptors.values():
            if desc.create_on_enter and desc.scope is scope_type:
                provider.resolve(desc.service_type)
        yield provider


class CurrentServiceProvider(ServiceProvider):
    def create[T](self, target_type: type[T], /, **kwargs: Any) -> T:
        if provider := current_provider.get(None):
            return provider.create(target_type, **kwargs)
        raise RuntimeError("No active DI scope")

    def resolve[T](self, service_type: type[T], /) -> T:
        if provider := current_provider.get(None):
            return provider.resolve(service_type)
        raise RuntimeError("No active DI scope")


service_provider: Final[CurrentServiceProvider] = CurrentServiceProvider()
"""Proxy for the current DI service provider."""
current_provider: ContextVar[DefaultServiceProvider] = ContextVar("current_provider")
NULL_PROVIDER: Final[ServiceProvider] = NullServiceProvider()


# A factory that returns a context manager — the provider enters it to get the instance and manage its lifecycle.
type ServiceFactory[T] = Callable[[ServiceProvider], AbstractContextManager[T]]


@dataclass(frozen=True, slots=True)
class ServiceDescriptor[T]:
    service_type: type[T]
    scope: type[ResolutionContext]
    factory: ServiceFactory[T] = field(repr=False)
    create_on_enter: bool = False


def _collect_deps(factory: Callable[..., object]) -> list[tuple[str, type]]:
    """Inspect a callable's parameters and collect (name, type) pairs for auto-wiring.

    Handles classes, classmethods, partial functions, etc. Uses inspect.signature for the effective
    parameter list (excludes self/cls, accounts for positional partial args) and get_type_hints on the
    underlying callable for type annotations.
    """
    # Unwrap to get the real callable for type hints
    unwrapped = factory
    bound_kwargs: set[str] = set()
    while isinstance(unwrapped, partial):
        bound_kwargs.update(unwrapped.keywords)
        unwrapped = unwrapped.func

    # inspect.signature handles partial, classmethod, staticmethod, classes, etc.
    params = inspect.signature(factory).parameters
    # get_type_hints needs the real callable; for classes, use __init__ to get param hints
    hints = get_type_hints(unwrapped.__init__ if isinstance(unwrapped, type) else unwrapped)

    factory_name = getattr(unwrapped, "__name__", repr(factory))
    deps: list[tuple[str, type]] = []
    for name in params:
        if name in bound_kwargs:
            continue
        if name not in hints:
            raise TypeError(f"Cannot auto-wire {factory_name}: parameter '{name}' has no type annotation")
        deps.append((name, hints[name]))

    return deps


def _make_service_factory[T](factory: Callable[..., T | Generator[T]]) -> ServiceFactory[T]:
    """Turn any callable (plain, generator, or already-wired) into a ServiceFactory."""
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
    deps = _collect_deps(service_type) if "__init__" in service_type.__dict__ else []

    def factory(provider: ServiceProvider) -> AbstractContextManager[T]:
        kwargs = {name: provider.resolve(dep_type) for name, dep_type in deps}
        return nullcontext(service_type(**kwargs))

    return factory


# Kinda like IServiceCollection in .NET, or svcs.Registry
@final
class ServiceRegistry:
    def __init__(self):
        self.descriptors: dict[tuple[type, type[ResolutionContext]], ServiceDescriptor] = {}

    def register_instance[T](
        self, value: T, service_type: type[T] | None = None, scope: type[ResolutionContext] | None = None
    ) -> None:
        sd = ServiceDescriptor(service_type or type(value), scope or AppContext, lambda _: nullcontext(value))
        self.descriptors[(sd.service_type, sd.scope)] = sd

    def register[T](
        self,
        service_type: type[T],
        factory: Callable[..., T | Generator[T]] | None = None,
        scope: type[ResolutionContext] | None = None,
        create_on_enter: bool = False,
    ) -> None:
        wrapped = _make_service_factory(factory) if factory else _factory_for_type(service_type)
        sd = ServiceDescriptor(service_type, scope or AppContext, wrapped, create_on_enter)
        self.descriptors[(sd.service_type, sd.scope)] = sd

    @contextmanager
    def app_scope(self) -> Generator[DefaultServiceProvider]:
        app_scope = AppContext()
        provider = DefaultServiceProvider(NULL_PROVIDER, self, app_scope)
        with app_scope.ctx, scope(provider):
            yield provider
