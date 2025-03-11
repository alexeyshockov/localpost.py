from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import Enum, auto
from typing import Protocol, TypeVar, final

from anyio import create_task_group
from typing_extensions import Self

from localpost._utils import EventView, EventViewProxy, def_full_name, start_task_soon, wait_all

from ._host import Host, HostedServiceFunc, ServiceFunc, ServiceLifetime, ServiceLifetimeManager, hosted_service

T = TypeVar("T")


class ServiceMode(Enum):
    NORMAL = auto()
    MAIN = auto()
    """
    When a main service stops, the host stops. If the service crashes (stops with an exception), the host stops with
    a non-zero exit code.
    """
    DAEMON = auto()
    """
    Daemon services don't prevent the host from stopping (when all other services are done).
    """

    @classmethod
    def create(cls, main: bool, daemon: bool) -> ServiceMode:
        if main and daemon:
            raise ValueError("Service can't be both main and daemon")
        if main:
            return cls.MAIN
        if daemon:
            return cls.DAEMON
        return cls.NORMAL


class HostedService(Protocol):
    # @property
    # def func(self) -> HostedServiceFunc: ...

    # TODO Support timeouts
    # start_timeout: float | None = None
    # shutdown_timeout: float | None = None

    @property
    def name(self) -> str: ...

    @property
    def mode(self) -> ServiceMode: ...

    @property
    def is_daemon(self) -> bool: ...

    @property
    def started(self) -> EventView: ...

    @property
    def shutting_down(self) -> EventView: ...

    @property
    def stopped(self) -> EventView: ...

    def shutdown(self) -> None: ...


@final
class _HostedService:
    @classmethod
    def create(cls, target: HostedServiceFunc, /, *, name: str | None = None, main=False, daemon=False) -> Self:
        name = name if name else def_full_name(target)
        return cls(target, name, ServiceMode.create(main, daemon))

    def __init__(self, func: HostedServiceFunc, name: str, mode: ServiceMode):
        self.func = func
        self.name = name
        self.mode = mode

        self.started = EventViewProxy()
        self.shutting_down = EventViewProxy()
        self.stopped = EventViewProxy()

        self._lifetime: ServiceLifetime | None = None

    # TODO repr

    @property
    def lifetime(self) -> ServiceLifetime:
        if self._lifetime:
            return self._lifetime
        raise RuntimeError("Service has not been started")

    @lifetime.setter
    def lifetime(self, value: ServiceLifetime):
        assert self._lifetime is None, "Service has already been started"
        self._lifetime = value
        self.started.resolve(value.started)
        self.shutting_down.resolve(value.shutting_down)
        self.stopped.resolve(value.stopped)

    def shutdown(self) -> None:
        self.lifetime.shutdown()

    @property
    def is_daemon(self) -> bool:
        return self.mode is ServiceMode.DAEMON


@final
class AppHost(Host):  # type: ignore
    def __init__(self, *, name: str | None = None):
        name = name or "app_host"
        super().__init__(self._run, name=name)
        self._services: list[_HostedService] = []

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.name!r} with {len(self.services)} services>"

    @property
    def services(self) -> Sequence[HostedService]:
        services: Sequence[HostedService] = self._services
        return services

    async def _run(self, root_svc_lifetime: ServiceLifetimeManager) -> None:
        services = self._services
        if not services:
            raise RuntimeError("No services to run")

        for hs in services:
            hs.lifetime = root_svc_lifetime.start_child_service(hs.func, name=hs.name)
        foreground_services = [cs for cs in services if not cs.is_daemon]

        async def when_service_stopped(svc: _HostedService):
            await svc.lifetime.stopped.wait()
            if svc.lifetime.exception and not svc.is_daemon:
                root_svc_lifetime.set_shutting_down(reason=svc.lifetime.exception)
            if svc.mode is ServiceMode.MAIN:
                root_svc_lifetime.set_shutting_down()

        async def when_foreground_services_stopped():
            await wait_all(s.lifetime.stopped for s in foreground_services)
            root_svc_lifetime.set_shutting_down()

        async def when_services_started():
            await wait_all(cs.lifetime.started for cs in services)
            # Consider the host started only when all services are started
            root_svc_lifetime.set_started()

        async with create_task_group() as tg:
            for hs in services:
                tg.start_soon(when_service_stopped, hs)
            start_task_soon(tg, when_foreground_services_stopped)
            start_task_soon(tg, when_services_started)
            await root_svc_lifetime.shutting_down.wait()
            tg.cancel_scope.cancel()

    def add_uniq_service(
        self, target: HostedServiceFunc, /, *, name: str | None = None, main=False, daemon=False
    ) -> HostedService:
        hs = _HostedService.create(target, name=name, main=main, daemon=daemon)
        if hs.name in {s.name for s in self._services}:
            raise ValueError(f"Service with the same name has been already registered: {hs.name}")
        self._services.append(hs)
        svc: HostedService = hs
        return svc

    def add_service(
        self, target: HostedServiceFunc, /, *, name: str | None = None, main=False, daemon=False
    ) -> HostedService:
        hs = _HostedService.create(target, name=name, main=main, daemon=daemon)
        self._services.append(hs)
        svc: HostedService = hs
        return svc

    def register_service(
        self,
        target: ServiceFunc,
        /,
        *,
        name: str | None = None,
        main=False,
        daemon=False,
    ) -> HostedService:
        """
        Register a hosted service.

        :param target: Function or callable object.
        :param name: Service name (if not specified, target's name will be used).
        :param main: If True, the service will be considered as a main one (the host will shut down immediately when
        it is done).
        :param daemon: If True, the service won't prevent the host from stopping (when all other services are done).
        :return: Service descriptor.
        """
        return self.add_service(hosted_service(target), name=name, main=main, daemon=daemon)

    def service(
        self, name: str | None = None, /, *, main=False, daemon=False
    ) -> Callable[[ServiceFunc], HostedService]:
        """
        Decorator to register a function as a hosted service.
        """

        def _decorator(func: ServiceFunc):
            return self.register_service(func, name=name, main=main, daemon=daemon)

        return _decorator
