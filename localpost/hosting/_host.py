from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import math
import threading
from collections.abc import AsyncGenerator, Generator, Iterable, Sequence
from contextlib import asynccontextmanager, contextmanager
from functools import cached_property, wraps
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
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
    Event,
    create_task_group,
    fail_after,
    get_cancelled_exc_class,
    to_thread,
)
from anyio.abc import TaskGroup, TaskStatus
from anyio.from_thread import BlockingPortal, start_blocking_portal
from typing_extensions import Self, TypeVarTuple, Unpack

from localpost._utils import (
    NO_OP_TS,
    EventView,
    choose_anyio_backend,
    def_full_name,
    is_async_callable,
    start_task_soon,
    unwrap_exc,
    wait_any,
)

T = TypeVar("T")
PosArgsT = TypeVarTuple("PosArgsT")

logger = logging.getLogger("localpost.hosting")

# A custom limiter for anyio.to_thread.run_sync (to avoid using the default limiter capacity for long-running tasks
# (hosted services)). Basically a custom thread pool.
sync_services_limiter = CapacityLimiter(1)


@final
@dc.dataclass(frozen=True, slots=True)
class Starting:
    name: ClassVar[str] = "starting"
    # timeout: float


@final
@dc.dataclass(frozen=True, slots=True)
class Running:
    name: ClassVar[str] = "running"
    value: Any = None
    # graceful_shutdown_scope: CancelScope | None = None


@final
@dc.dataclass(frozen=True, slots=True)
class ShuttingDown:
    name: ClassVar[str] = "shutting_down"
    reason: BaseException | str | None = None
    # timeout: float


@final
@dc.dataclass(frozen=True, slots=True)
class Stopped:
    name: ClassVar[str] = "stopped"
    shutdown_reason: BaseException | str | None = None
    exception: BaseException | None = None


ServiceState = Union[Starting, Running, ShuttingDown, Stopped]


# Just a dict, ready for (JSON) serialization
class ServiceStatus(TypedDict):
    name: str
    state: str
    services: Sequence[ServiceStatus]
    shutdown_reason: str | None
    exception: str | None


class ServiceLifetime(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def shutdown_timeout(self) -> float: ...

    @property
    def state(self) -> ServiceState: ...

    @property
    def status(self) -> ServiceStatus: ...

    # @property
    # def child_services(self) -> Sequence[ServiceLifetimeView]: ...

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
    def shutdown_timeout(self) -> float: ...

    @property
    def state(self) -> ServiceState: ...

    @property
    def status(self) -> ServiceStatus: ...

    # @property
    # def child_services(self) -> Sequence[ServiceLifetimeView]: ...

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
        func: Callable[[ServiceLifetimeManager], Awaitable[Any]],
        /,
        *,
        name: str | None = None,
        start_timeout: float = math.inf,
        stop_timeout: float = math.inf,
    ) -> ServiceLifetime: ...

    @overload
    def start_child_service(
        self,
        func: Callable[[ServiceLifetimeManager, Unpack[PosArgsT]], Awaitable[Any]],
        /,
        *func_args: Unpack[PosArgsT],
        name: str | None = None,
        start_timeout: float = math.inf,
        stop_timeout: float = math.inf,
    ) -> ServiceLifetime: ...

    # PyCharm has a bug when calling a TypeVarTuple-parameterized function with 0 arguments (see
    # https://youtrack.jetbrains.com/issue/PY-63820), that why this dance with overloads
    def start_child_service(  # type: ignore[misc]
        self,
        func: Callable[..., Awaitable[Any]],
        /,
        *func_args,
        name: str | None = None,
        start_timeout: float = math.inf,
        stop_timeout: float = math.inf,
    ) -> ServiceLifetime: ...


# Base types that we operate on
HostedServiceFunc: TypeAlias = Callable[[ServiceLifetimeManager], Awaitable[Any]]
# class HostedServiceFunc:
#     def __call__(self, service_lifetime: ServiceLifetimeManager, *args) -> Awaitable[Any]: ...

# Convenience types that we support in the public API (they will be wrapped to the base types)
ServiceFunc: TypeAlias = Union[
    Callable[[ServiceLifetimeManager], Awaitable[Any]],  # Service
    Callable[[], Awaitable[Any]],  # SimpleService
    Callable[[ServiceLifetimeManager], Any],  # SyncService
    Callable[[], Any],  # SimpleSyncService
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


# Can be used as a decorator
def hosted_service(target: ServiceFunc) -> HostedServiceFunc:
    return async_hosted_service(target) if is_async_callable(target) else sync_hosted_service(target)


class _ServiceLifetime:
    def __init__(self, name: str, host: Host):
        self.tg = create_task_group()
        self.name = name
        self.host = host
        self.child_services: list[ServiceLifetime] = []
        self.shutdown_timeout = math.inf  # No timeout by default

        self.started = Event()
        self.value: Any = None

        self.graceful_shutdown_scope: CancelScope | None = None
        self.shutting_down = Event()
        self.shutdown_reason: BaseException | str | None = None

        self.stopped = Event()
        self.exception: BaseException | None = None

    @property
    def state(self) -> ServiceState:
        if self.stopped.is_set():
            return Stopped(self.shutdown_reason, self.exception)
        if self.shutting_down.is_set():
            return ShuttingDown(self.shutdown_reason)
        if self.started.is_set():
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
        await self.started.wait()
        return self.value

    def set_started(self, value=None, /, *, graceful_shutdown_scope: CancelScope | None = None):
        if self.stopped.is_set() or self.shutting_down.is_set() or self.started.is_set():
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
        if self.stopped.is_set() or self.shutting_down.is_set():
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
        start_timeout: float = math.inf,
    ) -> ServiceLifetime:
        if self.stopped.is_set():
            raise RuntimeError(f"{self.name} is stopping or stopped")

        def start_service():
            svc_lifetime = _ServiceLifetime(name or def_full_name(func), self.host)
            child_lifetime = cast(ServiceLifetime, svc_lifetime)
            self.child_services.append(child_lifetime)
            _start_service(self.tg, svc_lifetime, func, *target_args, start_timeout=start_timeout)
            return child_lifetime

        return self.host.in_host_thread(start_service)


def _start_service(
    parent_tg: TaskGroup, svc_lifetime: _ServiceLifetime, func, *func_args, start_timeout: float = math.inf
):
    parent_tg.start_soon(_run_service, func, func_args, svc_lifetime, start_timeout)
    if not math.isinf(svc_lifetime.shutdown_timeout):
        parent_tg.start_soon(_observe_service_shutdown, svc_lifetime)


async def _observe_service_shutdown(lifetime: _ServiceLifetime):
    await wait_any(lifetime.shutting_down, lifetime.stopped)
    if lifetime.stopped.is_set():
        return
    service_scope = lifetime.tg.cancel_scope
    timeout = lifetime.shutdown_timeout
    assert timeout is not None
    assert timeout >= 0
    if math.isinf(timeout):
        return
    if timeout == 0:
        service_scope.cancel()
        return
    try:
        with fail_after(timeout):
            await lifetime.stopped.wait()
    except TimeoutError as exc:
        lifetime.exception = exc
        logger.error(f"{lifetime.name} shutdown timeout")
        service_scope.cancel()


async def _observe_service_startup(lifetime: _ServiceLifetime, timeout: float):
    if math.isinf(timeout):
        return
    assert timeout > 0
    try:
        with fail_after(timeout):
            await lifetime.started.wait()
    except TimeoutError as exc:
        lifetime.exception = exc
        logger.error(f"{lifetime.name} startup timeout")
        lifetime.tg.cancel_scope.cancel()


async def _run_service(func, func_args: Iterable[Any], svc_lifetime: _ServiceLifetime, start_timeout: float = math.inf):
    async def _service():
        await func(cast(ServiceLifetimeManager, svc_lifetime), *func_args)
        if child_services := svc_lifetime.child_services:
            await svc_lifetime.shutting_down.wait()
            for child in child_services:
                child.shutdown()

    svc_name = svc_lifetime.name
    logger.debug(f"Starting {svc_name}...")
    try:
        # service_tg will be used for the service itself and its child services
        async with svc_lifetime.tg as service_tg:
            start_task_soon(service_tg, _service)
            service_tg.start_soon(_observe_service_startup, svc_lifetime, start_timeout)
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

    def __init__(self, host: Host, portal: BlockingPortal, run_scope: CancelScope):
        self.portal = portal
        self.thread_id = threading.get_ident()
        self.run_scope = run_scope

        self.root_service_lifetime = svc_lifetime = _ServiceLifetime(host.name, host)
        svc_lifetime.shutdown_timeout = host.shutdown_timeout
        svc_lifetime.started = cast(Event, host.started)
        svc_lifetime.shutting_down = cast(Event, host.shutting_down)
        svc_lifetime.stopped = cast(Event, host.stopped)


@final
class Host:
    def __init__(self, root_service: HostedServiceFunc, *, name: str | None = None):
        self.name: str = name or "host"
        self._root_service = root_service
        self._shutdown_timeout = 10.0  # Seconds

        self._exec_context: _HostExecContext | None = None
        self._exit_code: int | None = None

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.name!r}>"

    @property
    def exit_code(self) -> int:
        if self._exit_code is not None:
            return self._exit_code  # Set by the user
        if self._exec_context:
            return 1 if self._exec_context.root_service_lifetime.exception else 0
        return 0

    @exit_code.setter
    def exit_code(self, value: int):
        self._exit_code = value

    @cached_property
    def started(self) -> EventView:
        return Event()

    @cached_property
    def shutting_down(self) -> EventView:
        return Event()

    @cached_property
    def stopped(self) -> EventView:
        return Event()

    @property
    def _exec(self) -> _HostExecContext:
        if self._exec_context:
            return self._exec_context
        raise RuntimeError("Host has not been started")

    @property
    def portal(self) -> BlockingPortal:
        return self._exec.portal

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

    @property
    def shutdown_timeout(self) -> float:
        return self._shutdown_timeout

    @shutdown_timeout.setter
    def shutdown_timeout(self, value: float):
        if value <= 0:
            raise ValueError("Shutdown timeout must be positive")
        self._shutdown_timeout = value
        if self._exec_context:
            self._exec_context.root_service_lifetime.shutdown_timeout = value

    @asynccontextmanager
    async def _aserve_in(self, portal: BlockingPortal, exec_tg: TaskGroup | None = None):
        assert not self._exec_context

        # A premature optimization
        tg: TaskGroup = exec_tg if exec_tg else portal._task_group  # noqa

        self._exec_context = exec_context = _HostExecContext(self, portal, tg.cancel_scope)

        _start_service(tg, exec_context.root_service_lifetime, self._root_service)
        try:
            yield self
        finally:
            if not self.stopped.is_set():
                # Wait till all services are stopped (act like a task group, not like a portal)
                await self.stopped.wait()

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
