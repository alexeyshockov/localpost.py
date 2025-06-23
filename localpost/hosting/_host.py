from __future__ import annotations

import abc
import dataclasses as dc
import inspect
import itertools
import logging
import threading
from abc import abstractmethod
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Generator, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Final,
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
from typing_extensions import Concatenate, Self

from localpost._debug import debug
from localpost._utils import (
    NO_OP_TS,
    Event,
    EventView,
    EventViewProxy,
    choose_anyio_backend,
    def_full_name,
    is_async_callable,
    start_task_soon,
    unwrap_exc,
    wait_all,
)

T = TypeVar("T")
P = ParamSpec("P")

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


class HostLifetime(Protocol):
    exit_code: int = 0

    @property
    def name(self) -> str: ...

    @property
    def state(self) -> HostState: ...

    @property
    def status(self) -> ServiceStatus: ...

    # @property
    # def same_thread(self) -> bool: ...

    # @property
    # def portal(self) -> BlockingPortal: ...

    @property
    def started(self) -> EventView: ...

    @property
    def shutting_down(self) -> EventView: ...

    @property
    def stopped(self) -> EventView: ...

    def shutdown(self, *, reason: BaseException | str | None = None) -> None: ...


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
    def host(self) -> AbstractHost: ...

    def set_started(self, value=None, /, *, graceful_shutdown_scope: CancelScope | None = None) -> None: ...

    def set_shutting_down(self, *, reason: BaseException | str | None = None) -> None: ...

    # 1. PyCharm (at least 2024.03) has a bug when calling a TypeVarTuple-parameterized function with 0 arguments (see
    # https://youtrack.jetbrains.com/issue/PY-63820),
    # 2. mypy (at least 1.15.0) does not like overloads ("error: Incompatible return value type ...  [return-value]"),
    # So just skip complex typing here, for now
    def start_child_service(  # type: ignore[misc]
        self,
        # func: Callable[[ServiceLifetimeManager, Unpack[PosArgsT]], Awaitable[None]],
        func: Callable[..., Awaitable[None]],
        /,
        # *func_args: Unpack[PosArgsT],
        *func_args,
        name: str | None = None,
    ) -> ServiceLifetime: ...


# Everything that can be used as a hosted service, see HostedService.create()
ServiceFunc: TypeAlias = Union[
    Callable[..., Awaitable[None]],
    Callable[..., None],
]

if TYPE_CHECKING:
    HostedServiceFunc: TypeAlias = Callable[Concatenate[ServiceLifetimeManager, ...], Awaitable[None]]
else:
    # Python 3.10 does not support ... (ellipsis) for Concatenate
    HostedServiceFunc: TypeAlias = Callable[..., Awaitable[None]]
# HostedServiceFunc: TypeAlias = Callable[[ServiceLifetimeManager, ...], Awaitable[None]]
# class HostedServiceFunc(Protocol):
#     def __call__(self, service_lifetime: ServiceLifetimeManager, *args) -> Awaitable[None]: ...

HostedServiceDecorator: TypeAlias = Callable[
    [Callable[P, Awaitable[None]]],  # [HostedServiceFunc]
    Callable[P, Awaitable[None]],  # HostedServiceFunc
]


def async_service(func: Callable[..., Awaitable[None]]) -> HostedServiceFunc:
    """
    Decorator to create a service from an async function.
    """
    if len(inspect.signature(func).parameters) >= 1:
        return cast(HostedServiceFunc, func)

    @wraps(func)
    async def _simple_service(lifetime: ServiceLifetimeManager):
        with CancelScope() as run_scope:
            lifetime.set_started(graceful_shutdown_scope=run_scope)
            await func()

    return _simple_service


def sync_service(func: Callable[..., Any]) -> HostedServiceFunc:
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

    lifetime_aware = len(inspect.signature(func).parameters) >= 1
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
    def __init__(self, name: str, host: AbstractHost, parent_tg: TaskGroup):
        self.name = name
        self.host = host
        self.parent_tg = parent_tg
        self.tg = create_task_group()
        self.child_services: list[ServiceLifetime] = []

        self.started = Event()
        self.value: Any = None

        self.graceful_shutdown_scope: CancelScope | None = None
        self.shutting_down = Event()
        self.shutdown_reason: BaseException | str | None = None

        self.stopped = Event()
        self.exception: BaseException | None = None

    @property
    def as_view(self) -> ServiceLifetime:
        return self

    @property
    def as_manager(self) -> ServiceLifetimeManager:
        return self

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

        in_host_thread(self.host, _do)

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

        in_host_thread(self.host, _do)

    def start_child_service(
        self,
        func,
        /,
        *target_args,
        name: str | None = None,
    ) -> _ServiceLifetime:
        if self.stopped:
            raise RuntimeError("Cannot start a child service for a stopped service")

        def start_service():
            svc = HostedService.ensure(func).named(name)
            svc_lifetime = _ServiceLifetime(svc.name, self.host, self.tg)
            self.child_services.append(svc_lifetime.as_view)
            self.tg.start_soon(_run_service, svc, target_args, svc_lifetime, name=svc.name)
            return svc_lifetime

        return in_host_thread(self.host, start_service)


async def _run_service(func, func_args: Iterable[Any], svc_lifetime: _ServiceLifetime):
    async def _supervise_service():
        await func(svc_lifetime, *func_args)
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
        if debug:
            raise exc from exc.__cause__  # Re-raise the original exception for debugging
    finally:
        svc_lifetime.stopped.set()


@dc.dataclass(frozen=True, eq=False, slots=True)
class _HostExecContext:
    root_service_lifetime: _ServiceLifetime
    portal: BlockingPortal
    thread_id: int
    run_scope: CancelScope


def _hs_name(func: object) -> str:
    match func:
        case HostedService(s, attrs):
            return attrs.get("name") or _hs_name(s)
        case HostedServiceSet(services):
            return "(" + " + ".join(_hs_name(s) for s in services) + ")"
        case WrappedHostedService(w, t):
            return _hs_name(w) + " >> " + _hs_name(t)
        case _:
            return getattr(func, "name", def_full_name(func))


@final
@dc.dataclass(frozen=True)
class HostedService:  # Also a HostedServiceFunc, see __call__() below
    """
    Named hosted service callable, immutable.
    """

    source: HostedServiceFunc
    _attrs: dict[str, Any] = dc.field(compare=False, hash=False)

    @staticmethod
    def wraps(wrapped: HostedServiceFunc, attrs: dict[str, Any]) -> Callable[[HostedServiceFunc], HostedService]:
        def decorator(func: HostedServiceFunc) -> HostedService:
            wrapped_hs = HostedService.ensure(wrapped)
            return HostedService(func, **({"name": wrapped_hs.name} | attrs))

        return decorator

    @staticmethod
    def decorator(dec: Callable[..., HostedServiceFunc]) -> HostedServiceDecorator:
        def decorate(func: HostedServiceFunc) -> HostedService:
            svc_func = func
            attrs = {}
            if isinstance(func, HostedService):
                svc_func = func.source
                attrs = func._attrs
            return HostedService.ensure(dec(svc_func, **attrs))

        return decorate

    @staticmethod
    def _unwrap(func: HostedServiceFunc, attrs: dict[str, Any]) -> tuple[HostedServiceFunc, dict[str, Any]]:
        if isinstance(func, HostedService) and not func._attrs:
            return HostedService._unwrap(func.source, attrs)
        if isinstance(func, HostedService) and not attrs:
            return func.source, func._attrs
        if isinstance(func, HostedServiceSet) and len(func) == 1:
            return HostedService._unwrap(next(iter(func)), attrs)
        return func, attrs

    @classmethod
    def create(cls, target: ServiceFunc, /) -> Self:
        if isinstance(target, cls):
            return target
        return cls(async_service(target) if is_async_callable(target) else sync_service(target))

    @classmethod
    def ensure(cls, target: HostedServiceFunc, /) -> Self:
        if isinstance(target, cls):
            return target
        return cls(target)

    def __init__(self, s: HostedServiceFunc, /, **attrs):
        if "name" in attrs and not attrs["name"]:
            del attrs["name"]
        source, attrs = self._unwrap(s, attrs)
        if not callable(source):
            raise ValueError(f"Invalid service source: {source}")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "_attrs", attrs)

    @property
    def _services(self) -> HostedServiceSet:
        if isinstance(self.source, HostedServiceSet) and not self._attrs:
            return self.source
        return HostedServiceSet(self)

    @property
    def name(self) -> str:
        return _hs_name(self)

    def named(self, name: str | None) -> HostedService:
        """
        Override the service name.
        """
        if name == self._attrs.get("name"):
            return self
        attrs = self._attrs | {"name": name}
        return HostedService(self.source, **attrs)

    @property
    def attrs(self) -> Mapping[str, Any]:
        return self._attrs

    def annotated(self, **attrs: Any) -> HostedService:
        """
        Annotate the service with a set of attributes (kwargs).
        """
        if self._attrs == attrs:
            return self
        attrs = self._attrs | attrs
        return HostedService(self.source, **attrs)

    def __call__(self, service_lifetime: ServiceLifetimeManager, *args) -> Awaitable[None]:
        return self.source(service_lifetime, *args)

    def __add__(self, other: HostedServiceFunc) -> HostedService:
        """
        Combine two services to run in parallel.
        """
        if self is EMPTY_SERVICE:
            return HostedService.ensure(other)
        if other is EMPTY_SERVICE:
            return self
        if isinstance(other, HostedService):
            other = other._services
        return HostedService(self._services.add(other))

    def __iadd__(self, other: HostedServiceFunc) -> HostedService:
        return self.__add__(other)

    def wrap(self, target: HostedServiceFunc | Iterable[HostedServiceFunc]) -> HostedService:
        """
        Run `target` service(s) inside this service, like in a context manager.
        """
        if not callable(target):  # Assume multiple services
            target = HostedServiceSet(*target)
        return HostedService(WrappedHostedService(self, HostedService.ensure(target)))

    # s1 = s1 >> s2
    def __rshift__(self, target: HostedServiceFunc | Iterable[HostedServiceFunc]) -> HostedService:
        return self.wrap(target)

    # s1 >>= s2
    def __irshift__(self, target: HostedServiceFunc | Iterable[HostedServiceFunc]) -> HostedService:
        return self.wrap(target)

    def use(self, *middlewares: HostedServiceDecorator) -> HostedService:
        """
        Apply a middleware to the hosted service (decorate the source callable).
        """

        def _ensure(s: HostedServiceFunc) -> HostedService:
            return s if isinstance(s, HostedService) else HostedService(s, **self._attrs)

        source = self
        for middleware in middlewares:
            source = _ensure(middleware(source))
        return source

    # s1 = s1 // m1
    def __floordiv__(self, middleware: HostedServiceDecorator) -> HostedService:
        return self.use(middleware)

    # s1 //= m1
    def __ifloordiv__(self, middleware: HostedServiceDecorator) -> HostedService:
        return self.use(middleware)


@final
@dc.dataclass(frozen=True, slots=True)
class HostedServiceSet(Collection[HostedService]):
    services: frozenset[HostedService]

    def __init__(self, *services: HostedServiceFunc):
        def unwrap():
            for s in services:
                if isinstance(s, HostedServiceSet):  # Flatten if needed
                    yield from s.services
                else:
                    yield HostedService.ensure(s)

        object.__setattr__(self, "services", frozenset(unwrap()))

    def __repr__(self):
        return f"{self.__class__.__name__}(len={len(self)})"

    def __iter__(self) -> Iterator[HostedService]:
        return iter(self.services)

    def __len__(self) -> int:
        return len(self.services)

    def __contains__(self, x, /) -> bool:
        if not callable(x):
            return False
        other = HostedService.ensure(cast(HostedServiceFunc, x))
        return other in self.services

    def add(self, *services: HostedServiceFunc) -> HostedServiceSet:
        return HostedServiceSet(*itertools.chain(services, self.services))  # type: ignore[arg-type]

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


async def _empty_service(lifetime: ServiceLifetimeManager) -> None:
    lifetime.set_started()


EMPTY_SERVICE: Final = HostedService(_empty_service, name="empty_service")


class ExposedService(Protocol):  # Also a HostedServiceFunc
    def __call__(self, service_lifetime: ServiceLifetimeManager) -> Awaitable[None]: ...

    @property
    def started(self) -> EventView: ...

    @property
    def shutting_down(self) -> EventView: ...

    @property
    def stopped(self) -> EventView: ...

    def shutdown(self, *, reason: BaseException | str | None = None) -> None: ...


class ExposedServiceBase(abc.ABC):
    def __init__(self) -> None:
        self._started = EventViewProxy()
        self._shutting_down = EventViewProxy()
        self._stopped = EventViewProxy()
        self._service_lifetime: ServiceLifetimeManager | None = None

    @abstractmethod
    def __call__(self, service_lifetime: ServiceLifetimeManager) -> Awaitable[None]: ...

    def _assert_not_started(self):
        if self._service_lifetime:
            raise RuntimeError("Service has already started")

    @property
    def _lifetime(self) -> ServiceLifetimeManager:
        if self._service_lifetime:
            return self._service_lifetime
        raise RuntimeError("Service has not started yet")

    @_lifetime.setter
    def _lifetime(self, value: ServiceLifetimeManager):
        self._assert_not_started()
        self._service_lifetime = value
        self._started.resolve(value.started)
        self._shutting_down.resolve(value.shutting_down)
        self._stopped.resolve(value.stopped)

    @property
    def started(self) -> EventView:
        return self._started

    @property
    def shutting_down(self) -> EventView:
        return self._shutting_down

    @property
    def stopped(self) -> EventView:
        return self._stopped

    def shutdown(self, *, reason: BaseException | str | None = None) -> None:
        self._lifetime.set_shutting_down(reason=reason)


@final
class _ExposedService(ExposedServiceBase):
    def __init__(self, source: HostedServiceFunc):
        super().__init__()
        self._source = source

    def __repr__(self):
        return f"ExposedService(source={self._source!r})"

    def __call__(self, service_lifetime: ServiceLifetimeManager) -> Awaitable[None]:
        self._lifetime = service_lifetime
        return self._source(service_lifetime)


# # What is a better name: exposed() or observable()?..
# def exposed(func: HostedServiceFunc) -> ExposedService:
#     return _ExposedService(func)


class AbstractHost(ExposedServiceBase, abc.ABC):
    def __init__(self) -> None:
        super().__init__()
        self._exec_context: _HostExecContext | None = None
        self._exit_code: int | None = None

    def _assert_not_started(self):
        if self._exec_context:
            raise RuntimeError("Host has already started")

    @abstractmethod
    def _prepare_for_run(self) -> HostedService: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def exit_code(self) -> int:
        if self._exit_code is not None:
            return self._exit_code  # Set by the user
        if self._exec_context:
            return 1 if self._exec_context.root_service_lifetime.exception else 0
        return 0

    @exit_code.setter
    def exit_code(self, value: int):
        if not 0 <= value <= 255:
            raise ValueError("Exit code must be in [0,255] range")
        self._exit_code = value

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

    def stop(self) -> None:
        in_host_thread(self, self._exec.run_scope.cancel)

    def __call__(self, sl: ServiceLifetimeManager) -> Awaitable[None]:
        """Run the host as a service (in another host)."""
        assert isinstance(sl, _ServiceLifetime)
        self._lifetime = sl
        self._exec_context = _HostExecContext(sl, sl.host.portal, threading.get_ident(), sl.tg.cancel_scope)
        return self._prepare_for_run()(sl)

    @asynccontextmanager
    async def _aserve_in(self, portal: BlockingPortal, exec_tg: TaskGroup | None = None):
        # A premature optimization, to save one task group nesting level
        tg: TaskGroup = exec_tg if exec_tg else portal._task_group  # noqa
        self._lifetime = sl = _ServiceLifetime(self.name, self, tg)
        self._exec_context = _HostExecContext(sl, portal, threading.get_ident(), sl.tg.cancel_scope)
        tg.start_soon(_run_service, self._prepare_for_run(), (), sl)
        try:
            yield cast(HostLifetime, self)
        finally:
            if not self.stopped:
                # Wait till all services are stopped (act like a task group, not like a portal)
                await self.stopped

    @asynccontextmanager
    async def aserve(self, portal: BlockingPortal | None = None) -> AsyncGenerator[HostLifetime, Any]:
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

    async def aexecute(
        self, portal: BlockingPortal | None = None, *, task_status: TaskStatus[HostLifetime] = NO_OP_TS
    ) -> None:
        async with self.aserve(portal) as lifetime:
            task_status.started(lifetime)

    @contextmanager
    def serve(self) -> Generator[HostLifetime, Any, None]:
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


# def in_host_thread(h: HostLifetime, func: Callable[..., T]) -> T:
def in_host_thread(h: AbstractHost, func: Callable[..., T]) -> T:
    if h.same_thread:
        return func()
    return h.portal.start_task_soon(func).result()


@final
class Host(AbstractHost):
    def __init__(self, root_service: HostedServiceFunc, /):
        super().__init__()
        self._root_service = HostedService.ensure(root_service)

    def __repr__(self):
        return f"{self.__class__.__name__}(root_service={self.name!r})"

    def _prepare_for_run(self) -> HostedService:
        return self._root_service

    @property
    def name(self) -> str:
        return self._root_service.name

    @property
    def root_service(self) -> HostedService:
        return self._root_service

    @root_service.setter
    def root_service(self, value: HostedServiceFunc):
        self._assert_not_started()
        self._root_service = HostedService.ensure(value)
