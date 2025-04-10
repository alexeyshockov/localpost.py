from collections.abc import Callable
from typing import Any, TypeVar, cast, final, overload

from localpost.hosting._host import (
    EMPTY_SERVICE,
    AbstractHost,
    HostedService,
    HostedServiceDecorator,
    HostedServiceFunc,
)

T = TypeVar("T", bound=Callable[..., Any])


@final
class AppHost(AbstractHost):
    def __init__(self, name: str | None = "app", /):
        super().__init__()
        self._name = name
        self._middlewares: tuple[HostedServiceDecorator, ...] = ()
        self._root_service = EMPTY_SERVICE

    def __repr__(self):
        return f"{self.__class__.__name__}(root_service={self.name!r})"

    def _prepare_for_run(self) -> HostedService:
        return self._root_service.use(*self._middlewares).named(self.name)

    @property
    def name(self) -> str:
        return self._name or self._root_service.name

    @name.setter
    def name(self, value: str | None):
        self._assert_not_started()
        self._name = value

    @property
    def root_service(self) -> HostedService:
        return self._root_service

    @root_service.setter
    def root_service(self, value: HostedServiceFunc):
        self._assert_not_started()
        self._root_service = HostedService.ensure(value)

    def use(self, *middlewares: HostedServiceDecorator) -> None:
        """
        Register middlewares for the root service.
        """
        self._assert_not_started()
        self._middlewares += middlewares

    def _add_service(self, target: T, name: str | None = None) -> T:
        self._assert_not_started()
        hs = HostedService.create(target).named(name)
        self.root_service += hs
        return target

    @overload
    def service(self, target: T, /) -> T: ...

    @overload
    def service(self, name: str | None, /) -> Callable[[T], T]: ...

    def service(self, target: T | str | None) -> T | Callable[[T], T]:
        """
        Register a service in the host.

        Equivalent to: `host.root_service += hosted_service(target).named(name)`
        """

        def _decorator(func: T) -> T:
            return self._add_service(func, cast(str | None, target))

        return self._add_service(target) if callable(target) else _decorator
