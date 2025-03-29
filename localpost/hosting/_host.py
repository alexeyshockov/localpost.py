from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Generator, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import cached_property, wraps
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    ParamSpec,
    Protocol,
    TypeAlias,
    TypedDict,
    TypeVar,
    Union,
    cast,
    final,
    overload,
)

from anyio import (
    CancelScope,
    CapacityLimiter,
    create_task_group,
    get_cancelled_exc_class,
    to_thread,
)
from anyio.abc import TaskGroup, TaskStatus
from anyio.from_thread import BlockingPortal, start_blocking_portal
from typing_extensions import Concatenate, Self, TypeVarTuple, Unpack

from localpost._utils import (
    NO_OP_TS,
    EventView,
    _Event,
    choose_anyio_backend,
    def_full_name,
    is_async_callable,
    start_task_soon,
    unwrap_exc,
    wait_all,
)

T = TypeVar("T")
PosArgsT = TypeVarTuple("PosArgsT")

logger = logging.getLogger("localpost.hosting")

# A custom limiter for anyio.to_thread.run_sync (to avoid using the default limiter capacity for long-running tasks
# (hosted services)). Basically a custom thread pool.
sync_services_limiter = CapacityLimiter(1)


@final
@dc.dataclass(frozen=True, slots=True)
class Created:
    name: ClassVar[Literal["created"]] = "created"


@final
@dc.dataclass(frozen=True, slots=True)
class Starting:
    name: ClassVar[Literal["starting"]] = "starting"
    # timeout: float


@final
@dc.dataclass(frozen=True, slots=True)
class Running:
    name: ClassVar[Literal["running"]] = "running"
    value: Any = None
    # graceful_shutdown_scope: CancelScope | None = None


@final
@dc.dataclass(frozen=True, slots=True)
class ShuttingDown:
    name: ClassVar[Literal["shutting_down"]] = "shutting_down"
    reason: BaseException | str | None = None
    # timeout: float


@final
@dc.dataclass(frozen=True, slots=True)
class Stopped:
    name: ClassVar[Literal["stopped"]] = "stopped"
    shutdown_reason: BaseException | str | None = None
    exception: BaseException | None = None


ServiceState = Union[Starting, Running, ShuttingDown, Stopped]
HostState = Union[Created, Starting, Running, ShuttingDown, Stopped]


# Just a dict, ready for (JSON) serialization
class ServiceStatus(TypedDict):
    name: str
    state: Literal["created", "starting", "running", "shutting_down", "stopped"]
    services: Collection[ServiceStatus]
    shutdown_reason: str | None
    exception: str | None


class ServiceLifetime(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def state(self) -> ServiceState: ...

    @property
    def status(self) -> ServiceStatus: ...

    # @property
    # def child_services(self) -> Collection[ServiceLifetimeView]: ...

    @property
    def started(self) -> EventView: ...

    @property
    def shutting_down(self) -> EventView: ...

    @property
    def stopped(self) -> EventView: ...

    # --- Common ---

    @property
    def value(self) -> Any: ...

    @property
    def exception(self) -> BaseException | None: ...

    @property
    def shutdown_reason(self) -> BaseException | str | None: ...

    async def wait_started(self) -> Any: ...

    def shutdown(self, *, reason: BaseException | str | None = None) -> None: ...


class ServiceLifetimeManager(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def state(self) -> ServiceState: ...

    @property
    def status(self) -> ServiceStatus: ...

    # @property
    # def child_services(self) -> Collection[ServiceLifetimeView]: ...

    @property
    def started(self) -> EventView: ...

    @property
    def shutting_down(self) -> EventView: ...

    @property
    def stopped(self) -> EventView: ...

    # --- Common ---

    @property
    def host(self) -> Host: ...

    def set_started(self, value=None, /, *, graceful_shutdown_scope: CancelScope | None = None) -> None: ...

    def set_shutting_down(self, *, reason: BaseException | str | None = None) -> None: ...

    @overload
    def start_child_service(
        self,
        func: Callable[[ServiceLifetimeManager], Awaitable[None]],
        /,
        *,
        name: str | None = None,
    ) -> ServiceLifetime: ...

    @overload
    def start_child_service(
        self,
        func: Callable[[ServiceLifetimeManager, Unpack[PosArgsT]], Awaitable[None]],
        /,
        *func_args: Unpack[PosArgsT],
        name: str | None = None,
    ) -> ServiceLifetime: ...

    # PyCharm has a bug when calling a TypeVarTuple-parameterized function with 0 arguments (see
    # https://youtrack.jetbrains.com/issue/PY-63820), that why this dance with overloads
    def start_child_service(  # type: ignore[misc]
        self,
        func: Callable[..., Awaitable[None]],
        /,
        *func_args,
        name: str | None = None,
    ) -> ServiceLifetime: ...


# Everything that can be used as a hosted service, see HostedService.create()
ServiceFunc: TypeAlias = Union[
    Callable[[ServiceLifetimeManager], Awaitable[Any]],  # Service
    Callable[[], Awaitable[Any]],  # SimpleService
    Callable[[ServiceLifetimeManager], Any],  # SyncService
    Callable[[], Any],  # SimpleSyncService
]

if TYPE_CHECKING:
    HostedServiceFunc: TypeAlias = Callable[Concatenate[ServiceLifetimeManager, ...], Awaitable[None]]
else:
    HostedServiceFunc: TypeAlias = Callable[..., Awaitable[None]]
# HostedServiceFunc: TypeAlias = Callable[[ServiceLifetimeManager, ...], Awaitable[None]]
# class HostedServiceFunc(Protocol):
#     def __call__(self, service_lifetime: ServiceLifetimeManager, *args) -> Awaitable[None]: ...

P = ParamSpec("P")
HostedServiceDecorator: TypeAlias = Callable[
    [Callable[P, Awaitable[None]]],  # [HostedServiceFunc]
    Callable[P, Awaitable[None]],  # HostedServiceFunc
]


def async_hosted_service(func: Callable[..., Awaitable[Any]]) -> HostedServiceFunc:
    """
    Decorator to create a service from an async function.
    """
    func_signature = inspect.signature(func)
    if len(func_signature.parameters) >= 1:
        return cast(HostedServiceFunc, func)

    @wraps(func)
    async def _simple_service(lifetime: ServiceLifetimeManager):
        with CancelScope() as run_scope:
            lifetime.set_started(graceful_shutdown_scope=run_scope)
            await func()

    return _simple_service


def sync_hosted_service(func: Callable[..., Any]) -> HostedServiceFunc:
    """
    Decorator to create a service from a target sync function (by running it in a separate thread).

    The target can be lifetime-aware (by accepting `ServiceLifetime` as the first argument).

    Important: the target must call `from_thread.check_cancelled()` periodically, to check for cancellation.
    """

    @wraps(func)
    async def _service(lifetime: ServiceLifetimeManager):
        sync_services_limiter.total_tokens += 1
        await to_thread.run_sync(func, lifetime, limiter=sync_services_limiter)

    @wraps(func)
    async def _simple_service(lifetime: ServiceLifetimeManager):
        sync_services_limiter.total_tokens += 1
        with CancelScope() as run_scope:
            lifetime.set_started(graceful_shutdown_scope=run_scope)
            await to_thread.run_sync(func, limiter=sync_services_limiter)

    func_signature = inspect.signature(func)
    lifetime_aware = len(func_signature.parameters) >= 1

    return _service if lifetime_aware else _simple_service


@overload
def hosted_service(target: ServiceFunc, /) -> HostedService: ...


@overload
def hosted_service(name: str | None, /) -> Callable[[ServiceFunc], HostedService]: ...


def hosted_service(target: ServiceFunc | str | None) -> HostedService | Callable[[ServiceFunc], HostedService]:
    """
    Create a hosted service from an arbitrary callable.
    """

    def _decorator(func: ServiceFunc) -> HostedService:
        return HostedService.create(func).named(cast(str | None, target))

    return HostedService.create(target) if callable(target) else _decorator


class _ServiceLifetime:
    def __init__(self, name: str, host: Host, parent_tg: TaskGroup):
        self.name = name
        self.host = host
        self.parent_tg = parent_tg
        self.tg = create_task_group()
        self.child_services: list[ServiceLifetime] = []

        self.started = _Event()
        self.value: Any = None

        self.graceful_shutdown_scope: CancelScope | None = None
        self.shutting_down = _Event()
        self.shutdown_reason: BaseException | str | None = None

        self.stopped = _Event()
        self.exception: BaseException | None = None

    @property
    def state(self) -> ServiceState:
        if self.stopped:
            return Stopped(self.shutdown_reason, self.exception)
        if self.shutting_down:
            return ShuttingDown(self.shutdown_reason)
        if self.started:
            return Running(self.value)
        return Starting()

    @property
    def status(self) -> ServiceStatus:
        return {
            "name": self.name,
            "state": self.state.name,
            "services": [cs.status for cs in self.child_services],
            "shutdown_reason": str(self.shutdown_reason) if self.shutdown_reason else None,
            # "exception": traceback.format_exception(self.exception) if self.exception else None,
            "exception": str(self.exception) if self.exception else None,
        }

    async def wait_started(self) -> Any:
        await self.started
        return self.value

    def set_started(self, value=None, /, *, graceful_shutdown_scope: CancelScope | None = None):
        if self.stopped or self.shutting_down or self.started:
            return

        def _do():
            self.started.set()
            self.value = value
            self.graceful_shutdown_scope = graceful_shutdown_scope
            logger.debug(f"{self.name} started")

        self.host.in_host_thread(_do)

    def set_shutting_down(self, *, reason: BaseException | str | None = None):
        self.shutdown(reason=reason)

    def shutdown(self, *, reason: BaseException | str | None = None):
        if self.stopped or self.shutting_down:
            return

        def _do():
            self.shutdown_reason = reason
            self.shutting_down.set()
            logger.debug(f"{self.name} shutting down")
            if graceful_shutdown_scope := self.graceful_shutdown_scope:
                graceful_shutdown_scope.cancel()

        self.host.in_host_thread(_do)

    def start_child_service(
        self,
        func,
        /,
        *target_args,
        name: str | None = None,
    ) -> ServiceLifetime:
        if self.stopped:
            raise RuntimeError("Cannot start a child service for a stopped service")

        def start_service():
            svc = HostedService.ensure(func)
            if name:
                svc = svc.named(name)
            svc_lifetime = _ServiceLifetime(svc.name, self.host, self.tg)
            child_lifetime = cast(ServiceLifetime, svc_lifetime)
            self.child_services.append(child_lifetime)
            self.tg.start_soon(_run_service, svc, target_args, svc_lifetime)
            return child_lifetime

        return self.host.in_host_thread(start_service)


async def _run_service(func, func_args: Iterable[Any], svc_lifetime: _ServiceLifetime):
    async def _supervise_service():
        await func(cast(ServiceLifetimeManager, svc_lifetime), *func_args)
        if child_services := svc_lifetime.child_services:
            await svc_lifetime.shutting_down
            for child in child_services:
                child.shutdown()

    svc_name = svc_lifetime.name
    logger.debug(f"Starting {svc_name}...")
    try:
        # service_tg will be used for the service itself and its child services
        async with svc_lifetime.tg as service_tg:
            start_task_soon(service_tg, _supervise_service)
        logger.debug(f"{svc_name} stopped")
    except get_cancelled_exc_class() as c_exc:  # Cancellation exception inherits directly from BaseException
        svc_lifetime.exception = c_exc
        logger.error(f"{svc_name} got cancelled")
        raise  # Always propagate the cancellation
    except Exception as exc:
        exc = unwrap_exc(exc)
        svc_lifetime.exception = exc
        logger.exception(f"{svc_name} crashed", exc_info=exc)
    finally:
        svc_lifetime.stopped.set()


@dc.dataclass(slots=True)  # TODO Make frozen
class _HostExecContext:
    root_service_lifetime: _ServiceLifetime
    portal: BlockingPortal
    thread_id: int
    run_scope: CancelScope

    def __init__(self, host: Host, portal: BlockingPortal, tg: TaskGroup):
        self.portal = portal
        self.thread_id = threading.get_ident()
        self.run_scope = tg.cancel_scope

        self.root_service_lifetime = svc_lifetime = _ServiceLifetime(host.name, host, tg)
        svc_lifetime.started = cast(_Event, host.started)
        svc_lifetime.shutting_down = cast(_Event, host.shutting_down)
        svc_lifetime.stopped = cast(_Event, host.stopped)


@final
@dc.dataclass(frozen=True, slots=True)
class HostedService:
    """
    Named hosted service callable, immutable.
    """

    _source: HostedServiceFunc
    _name_override: str | None = None

    @staticmethod
    def _unwrap(func: HostedServiceFunc) -> HostedServiceFunc:
        if isinstance(func, HostedService) and not func._name_override:
            return HostedService._unwrap(func._source)
        return func

    @classmethod
    def create(cls, target: ServiceFunc, /) -> Self:
        if isinstance(target, cls):
            return target
        return cls(async_hosted_service(target) if is_async_callable(target) else sync_hosted_service(target))

    @classmethod
    def ensure(cls, target: HostedServiceFunc, /) -> Self:
        if isinstance(target, cls):
            return target
        return cls(target)

    def __init__(self, source: HostedServiceFunc, name_override: str | None = None, /):
        assert callable(source)
        source = self._unwrap(source)
        if isinstance(source, HostedService) and not name_override:
            source, name_override = source._source, source._name_override
        if isinstance(source, HostedServiceSeq) and len(source) == 1:
            source = source.services[0]
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_name_override", name_override)

    @property
    def _services(self) -> HostedServiceSeq:
        if isinstance(self._source, HostedServiceSeq) and not self._name_override:
            return self._source
        return HostedServiceSeq(self)

    @property
    def name(self) -> str:
        return self._name_override or getattr(self._source, "name", def_full_name(self._source))

    def named(self, name_override: str | None) -> HostedService:
        if self._name_override == name_override:
            return self
        if not name_override and isinstance(self._source, HostedService):
            return self._source
        return HostedService(self._source, name_override)

    def __call__(self, service_lifetime: ServiceLifetimeManager, *args) -> Awaitable[None]:  # HostedServiceFunc
        return self._source(service_lifetime, *args)

    def __add__(self, other: HostedServiceFunc) -> HostedService:
        """
        Combine two services to run in parallel.
        """
        if isinstance(other, HostedService):
            other = other._services
        return HostedService(self._services.append(other))

    def __iadd__(self, other: HostedServiceFunc) -> HostedService:
        return self.__add__(other)

    # s1 = s1 >> s2
    def __rshift__(self, target: HostedServiceFunc | Iterable[HostedServiceFunc]) -> HostedService:
        """
        Run `wrapped` service(s) inside this host, like in a context manager.
        """
        if not callable(target):  # Assume multiple services
            target = HostedServiceSeq(*target)
        return HostedService(WrappedHostedService(self, HostedService.ensure(target)))  # TODO Name

    # s1 >>= s2
    def __irshift__(self, target: HostedServiceFunc | Iterable[HostedServiceFunc]) -> HostedService:
        return self.__rshift__(target)

    def use(self, *middlewares: HostedServiceDecorator) -> HostedService:
        """
        Apply a middleware to the hosted service (decorate the source callable).
        """
        source = self._source
        for middleware in middlewares:
            source = middleware(source)
        return HostedService(source, self._name_override)

    # s1 = s1 // m1
    def __floordiv__(self, middleware: HostedServiceDecorator) -> HostedService:
        return self.use(middleware)

    # s1 //= m1
    def __ifloordiv__(self, middleware: HostedServiceDecorator) -> HostedService:
        return self.use(middleware)


@final
@dc.dataclass(frozen=True, slots=True)
class HostedServiceSeq(Iterable[HostedService]):
    services: tuple[HostedServiceFunc]

    def __init__(self, *services: HostedServiceFunc):
        def unwrap():
            for s in services:
                if isinstance(s, HostedServiceSeq):  # Flatten if needed
                    yield from s.services
                else:
                    yield s

        object.__setattr__(self, "services", tuple(unwrap()))

    def __repr__(self):
        return f"{self.__class__.__name__}(len={len(self)})"

    def append(self, *services: HostedServiceFunc) -> HostedServiceSeq:
        return HostedServiceSeq(*(self.services + services))

    def __iter__(self) -> Iterator[HostedService]:
        for s in self.services:
            yield HostedService.ensure(s)

    def __len__(self) -> int:
        return len(self.services)

    async def __call__(self, lifetime: ServiceLifetimeManager) -> None:
        async def when_all_started():
            await wait_all(lt.started for lt in svc_lifetimes)
            lifetime.set_started()

        async def when_all_done():
            await wait_all(lt.stopped for lt in svc_lifetimes)
            lifetime.set_shutting_down()

        async def when_done(svc: ServiceLifetime):
            await svc.stopped
            if svc.exception:
                lifetime.set_shutting_down(reason="Child service crashed")

        svc_lifetimes = [lifetime.start_child_service(s) for s in self.services]
        if not svc_lifetimes:
            lifetime.set_started()
            return
        async with create_task_group() as observers_tg:
            start_task_soon(observers_tg, when_all_started)
            start_task_soon(observers_tg, when_all_done)
            for s in svc_lifetimes:
                observers_tg.start_soon(when_done, s)
            await lifetime.shutting_down
            observers_tg.cancel_scope.cancel()


@final
@dc.dataclass(frozen=True, slots=True)
class WrappedHostedService:
    wrapper: HostedService
    target: HostedService

    async def __call__(self, lifetime: ServiceLifetimeManager) -> None:
        async def when_target_started():
            await target.started
            lifetime.set_started()

        async def when_target_done():
            await target.stopped
            lifetime.set_shutting_down()

        async def when_wrapper_done():
            await wrapper.stopped
            lifetime.set_shutting_down(reason="Wrapper service stopped")

        wrapper = lifetime.start_child_service(self.wrapper)
        await wrapper.started
        target = lifetime.start_child_service(self.target)
        async with create_task_group() as observers_tg:
            start_task_soon(observers_tg, when_target_started)
            start_task_soon(observers_tg, when_target_done)
            start_task_soon(observers_tg, when_wrapper_done)
            await lifetime.shutting_down
            observers_tg.cancel_scope.cancel()
        target.shutdown()
        await target.stopped
        wrapper.shutdown()


@final
class Host:
    def __init__(self, root_service: HostedServiceFunc, /):
        self._root_service = HostedService.ensure(root_service)
        self._exec_context: _HostExecContext | None = None
        self._exit_code: int | None = None

    def __repr__(self):
        return f"{self.__class__.__name__}(root_service={self.name!r})"

    def _assert_not_started(self):
        if self._exec_context:
            raise RuntimeError("Host has already started")

    @property
    def root_service(self) -> HostedService:
        return self._root_service

    @root_service.setter
    def root_service(self, value: HostedServiceFunc):
        self._assert_not_started()
        self._root_service = HostedService.ensure(value)

    @property
    def name(self) -> str:
        return self._root_service.name

    @name.setter
    def name(self, value: str | None):
        self._assert_not_started()
        self._root_service = self._root_service.named(value)

    @property
    def exit_code(self) -> int:
        if self._exit_code is not None:
            return self._exit_code  # Set by the user
        if self._exec_context:
            return 1 if self._exec_context.root_service_lifetime.exception else 0
        return 0

    @exit_code.setter
    def exit_code(self, value: int):
        if 0 <= value <= 255:
            self._exit_code = value
        raise ValueError("Exit code must be in [0,255] range")

    @cached_property
    def started(self) -> EventView:
        return _Event()

    @cached_property
    def shutting_down(self) -> EventView:
        return _Event()

    @cached_property
    def stopped(self) -> EventView:
        return _Event()

    @property
    def _exec(self) -> _HostExecContext:
        if self._exec_context:
            return self._exec_context
        raise RuntimeError("Host has not started yet")

    @property
    def portal(self) -> BlockingPortal:
        return self._exec.portal

    @property
    def state(self) -> HostState:
        if self._exec_context:
            return self._exec_context.root_service_lifetime.state
        return Created()

    @property
    def status(self) -> ServiceStatus:
        if self._exec_context:
            return self._exec_context.root_service_lifetime.status
        return {
            "name": self.name,
            "state": "created",
            "services": [],
            "shutdown_reason": None,
            "exception": None,
        }

    @property
    def same_thread(self) -> bool:
        return threading.get_ident() == self._exec.thread_id

    def in_host_thread(self, func: Callable[[], T]) -> T:
        if self.same_thread:
            return func()
        return self.portal.start_task_soon(func).result()  # type: ignore

    def shutdown(self, *, reason: BaseException | str | None = None) -> None:
        self._exec.root_service_lifetime.shutdown(reason=reason)

    def stop(self) -> None:
        self.in_host_thread(self._exec.run_scope.cancel)

    @asynccontextmanager
    async def _aserve_in(self, portal: BlockingPortal, exec_tg: TaskGroup | None = None):
        self._assert_not_started()

        # A premature optimization, to save one task group nesting level
        tg: TaskGroup = exec_tg if exec_tg else portal._task_group  # noqa
        self._exec_context = exec_context = _HostExecContext(self, portal, tg)
        tg.start_soon(_run_service, self._root_service, (), exec_context.root_service_lifetime)
        try:
            yield self
        finally:
            if not self.stopped:
                # Wait till all services are stopped (act like a task group, not like a portal)
                await self.stopped

    @asynccontextmanager
    async def aserve(self, portal: BlockingPortal | None = None) -> AsyncGenerator[Self, Any]:
        """
        Start the host in the current event loop.

        :param portal: An optional portal for the current event loop (thread), if already created.
        :return: A context manager that returns the host instance.
        """
        if portal is None:
            async with BlockingPortal() as portal, self._aserve_in(portal) as lifetime:
                yield lifetime
        else:
            async with create_task_group() as exec_tg, self._aserve_in(portal, exec_tg) as lifetime:
                yield lifetime

    async def aexecute(self, portal: BlockingPortal | None = None, *, task_status: TaskStatus[Self] = NO_OP_TS) -> None:
        async with self.aserve(portal) as lifetime:
            task_status.started(lifetime)

    @contextmanager
    def serve(self) -> Generator[Self, Any, None]:
        """
        Start the host in a separate thread, on a separate event loop.

        Intended mainly for integration with legacy apps. Like when you have an old (not async) app and want to run some
        hosted services around it.

        In general, do prefer :meth:`aserve` instead.
        """
        logger.debug(f"Starting a separate thread for {self.name}...")
        with start_blocking_portal(**choose_anyio_backend()) as thread:
            with thread.wrap_async_context_manager(self._aserve_in(thread)) as lifetime:
                yield lifetime


@final
class AppHost(Host):  # type: ignore[misc]
    def __init__(self):
        async def _empty_service(lifetime: ServiceLifetimeManager):
            lifetime.set_started()
            logger.warning("AppHost is empty")

        super().__init__(HostedService(_empty_service, "app_host"))

    def _add_service(self, target: ServiceFunc, name: str | None = None) -> HostedService:
        self._assert_not_started()
        hs = HostedService.create(target).named(name)
        self.root_service += hs
        return hs

    @overload
    def service(self, target: ServiceFunc, /) -> HostedService: ...

    @overload
    def service(self, name: str | None, /) -> Callable[[ServiceFunc], HostedService]: ...

    def service(self, target: ServiceFunc | str | None) -> HostedService | Callable[[ServiceFunc], HostedService]:
        """
        Register a service in the host.

        Equivalent to: `host.root_service += hosted_service(target).named(name)`
        """

        def _decorator(func: ServiceFunc) -> HostedService:
            return self._add_service(func, cast(str | None, target))

        return self._add_service(target) if callable(target) else _decorator
