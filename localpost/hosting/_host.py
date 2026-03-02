from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import threading
from _contextvars import ContextVar
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from functools import cached_property, wraps
from typing import Any, ClassVar, Literal, TypeVar, final, overload

from anyio import CancelScope, CapacityLimiter, create_task_group, get_cancelled_exc_class, to_thread
from anyio.abc import TaskGroup
from anyio.from_thread import BlockingPortal

from localpost._utils import Event, EventView, cancellable_from, is_async_callable, unwrap_exc, wait_all

F = TypeVar("F", bound=Callable[..., Any])

logger = logging.getLogger("localpost.hosting")


@final
@dc.dataclass(frozen=True, slots=True)
class Starting:
    name: ClassVar[Literal["starting"]] = "starting"
    # timeout: float


@final
@dc.dataclass(frozen=True, slots=True)
class Running:
    name: ClassVar[Literal["running"]] = "running"


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


ServiceState = Starting | Running | ShuttingDown | Stopped


_svc_lt: ContextVar[ServiceLifetime] = ContextVar("localpost.current_service")


def _current_service() -> ServiceLifetime:
    if lt := _svc_lt.get(None):
        return lt
    raise RuntimeError("Not in hosting context")


def current_service() -> ServiceLifetimeView:
    return _current_service().view


@dc.dataclass(frozen=True)
class ServiceLifetimeView:
    _state: ServiceLifetime

    started: EventView
    shutting_down: EventView
    stopped: EventView

    @asynccontextmanager
    async def observe(self) -> AsyncIterator[ServiceLifetimeView]:
        await self.started
        try:
            yield self
        finally:
            self.shutdown()
            await self.stopped

    @property
    def exit_code(self) -> int:
        return self._state.exit_code

    @property
    def state(self) -> ServiceState:
        return self._state.state

    def wait_started(self) -> None:
        """Helper for sync code, to wait in a thread."""
        if self._state.same_thread:
            raise RuntimeError("Deadlock: synchronous wait in the async thread")
        # noinspection PyTypeChecker
        return self._state.portal.start_task_soon(self.started.wait).result()

    def wait_shutting_down(self) -> None:
        """Helper for sync code, to wait in a thread."""
        if self._state.same_thread:
            raise RuntimeError("Deadlock: synchronous wait in the async thread")
        # noinspection PyTypeChecker
        return self._state.portal.start_task_soon(self.shutting_down.wait).result()

    @overload
    def cancel_on_shutdown(self) -> Callable[[F], F]: ...

    @overload
    def cancel_on_shutdown(self, target: F | None = None, /) -> F: ...

    def cancel_on_shutdown(self, target: F | None = None, /) -> Any:
        dec = cancellable_from(self.shutting_down)
        return dec(target) if target is not None else dec

    @overload
    def cancel_on_stop(self) -> Callable[[F], F]: ...

    @overload
    def cancel_on_stop(self, target: F | None = None, /) -> F: ...

    def cancel_on_stop(self, target: F | None = None, /) -> Any:
        dec = cancellable_from(self.stopped)
        return dec(target) if target is not None else dec

    def shutdown(self, *, reason: BaseException | str | None = None) -> None:
        def do_shutdown():
            if self.stopped or self.shutting_down:
                return
            self._state.shutdown_reason = reason
            self._state.shutting_down.set()

        in_host_thread(self._state, do_shutdown)

    def stop(self) -> None:
        in_host_thread(self._state, self._state.run_scope.cancel)


@dc.dataclass(eq=False, unsafe_hash=True)
class ServiceLifetime:
    portal: BlockingPortal
    tg: TaskGroup = dc.field(default_factory=create_task_group)
    thread_id: int = dc.field(default_factory=threading.get_ident)

    started: Event = dc.field(default_factory=Event)
    shutting_down: Event = dc.field(default_factory=Event)
    stopped: Event = dc.field(default_factory=Event)

    shutdown_reason: BaseException | str | None = None
    exception: BaseException | None = None

    _exit_code: int | None = None

    @cached_property
    def view(self) -> ServiceLifetimeView:
        return ServiceLifetimeView(self, self.started, self.shutting_down, self.stopped)

    @property
    def exit_code(self) -> int:
        if self._exit_code is not None:
            return self._exit_code  # Set by the user
        return 1 if self.exception else 0

    @exit_code.setter
    def exit_code(self, value: int):
        if not 0 <= value <= 255:
            raise ValueError("Exit code must be in [0,255] range")
        object.__setattr__(self, "_exit_code", value)

    @property
    def run_scope(self) -> CancelScope:
        return self.tg.cancel_scope

    @property
    def state(self) -> ServiceState:
        if self.stopped:
            return Stopped(self.shutdown_reason, self.exception)
        if self.shutting_down:
            return ShuttingDown(self.shutdown_reason)
        if self.started:
            return Running()
        return Starting()

    @property
    def same_thread(self) -> bool:
        return threading.get_ident() == self.thread_id

    def set_started(self) -> None:
        def do_set():
            assert not self.stopped, "Cannot mark already stopped service as started"
            self.started.set()

        in_host_thread(self, do_set)

    def set_shutting_down(self, reason: BaseException | str | None = None) -> None:
        def do_set():
            assert not self.stopped, "Cannot mark already stopped service as started"
            if not self.shutting_down.is_set():
                self.shutdown_reason = reason
                self.shutting_down.set()

        in_host_thread(self, do_set)

    def start(self, svc_f: ServiceF, /) -> ServiceLifetimeView:
        """Start a child service from the given function."""

        def do_start() -> ServiceLifetimeView:
            child_lt = ServiceLifetime(self.portal)
            self.tg.start_soon(_run, svc_f, child_lt)
            return child_lt.view

        return in_host_thread(self, do_start)


def in_host_thread[R](h: ServiceLifetime, func: Callable[..., R], *args) -> R:
    if h.same_thread:
        return func(*args)
    # noinspection PyTypeChecker
    return h.portal.start_task_soon(func).result()


ServiceF = Callable[[ServiceLifetime], Awaitable[None]]
# AppF = Callable[[HostLifetime], Awaitable[None]]


@dc.dataclass()
class _ResolvedService(AbstractAsyncContextManager[ServiceLifetimeView]):
    func: ServiceF
    _run: AbstractAsyncContextManager | None = dc.field(default=None, compare=False, repr=False)

    def __call__(self, lt: ServiceLifetime, /) -> Awaitable[None]:
        return self.func(lt)

    async def __aenter__(self) -> ServiceLifetimeView:
        assert self._run is None
        self._run = serve(self.func, parent=_svc_lt.get(None))
        return await self._run.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        assert self._run is not None
        return await self._run.__aexit__(exc_type, exc_val, exc_tb)


@asynccontextmanager
async def _serve_and_observe(svc_f: ServiceF, /):
    async with serve(svc_f, parent=_svc_lt.get(None)) as lt, lt.observe():
        yield lt


@overload
def service[**P](target: Callable[P, Any]) -> Callable[P, _ResolvedService]:
    """Decorator to create a hosted service."""


@overload
def service[**P]() -> Callable[[Callable[P, Any]], Callable[P, _ResolvedService]]:
    """Decorator to create a hosted service."""


def service(target: Callable[..., Any] | None = None):
    # FIXME Wrap sync functions, wrap context managers
    def decorator(func: Callable[..., Any]) -> Callable[..., _ResolvedService]:
        @wraps(func)
        def wrapper(*args, **kwargs):
            raw_svc_f = func(*args, **kwargs)
            if is_async_callable(raw_svc_f):
                svc_f = raw_svc_f
            else:
                svc_f = lambda lt: to_thread.run_sync(raw_svc_f, lt, limiter=CapacityLimiter(1))
            return _ResolvedService(svc_f)

        if inspect.isasyncgenfunction(func):
            return _service_cm(func)
        return wrapper

    return decorator(target) if callable(target) else decorator


def _service_cm(func: Callable[..., AsyncGenerator]):
    """Decorator to transform a generator function into a service factory."""
    cm_f = asynccontextmanager(func)

    @wraps(func)
    def wrapper(*args, **kwargs):
        async def svc_f(lt: ServiceLifetime) -> None:
            async with cm_f(*args, **kwargs):
                lt.set_started()
                await lt.shutting_down.wait()

        return _ResolvedService(svc_f)

    return wrapper


@asynccontextmanager
async def observe_services(*lifetimes: ServiceLifetimeView):
    await wait_all(svc.started for svc in lifetimes)
    try:
        yield
    finally:
        for svc in lifetimes:
            svc.shutdown()
        await wait_all(svc.stopped for svc in lifetimes)


async def _run(svc_f: ServiceF, lt: ServiceLifetime) -> None:
    outer_ctx_token = _svc_lt.set(lt)
    try:
        async with lt.tg as tg:
            await svc_f(lt)
            tg.cancel_scope.cancel()  # Cancel any remaining tasks
    except get_cancelled_exc_class() as exc:  # BaseException
        lt.exception = exc
        raise  # Always reraise cancellations
    except Exception as exc:
        lt.exception = unwrap_exc(exc)
    finally:
        lt.stopped.set()
        _svc_lt.reset(outer_ctx_token)


def serve(
    svc: ServiceF, /, *, parent: ServiceLifetime | None = None
) -> AbstractAsyncContextManager[ServiceLifetimeView]:
    return _serve_in(svc, parent) if parent else _serve_root(svc)


@asynccontextmanager
async def _serve_root(svc: ServiceF) -> AsyncIterator[ServiceLifetimeView]:
    async with BlockingPortal() as portal:
        child_lt = ServiceLifetime(portal)
        tg = portal._task_group
        tg.start_soon(_run, svc, child_lt)
        await child_lt.started
        yield child_lt.view
        child_lt.view.shutdown()
        await child_lt.stopped


@asynccontextmanager
async def _serve_in(svc: ServiceF, parent: ServiceLifetime) -> AsyncIterator[ServiceLifetimeView]:
    # TODO Just throw an exception if the service stops with an error?.. Instead of binding the parent lifetime
    async def bind_parent_to(csl: ServiceLifetimeView):
        await csl.stopped
        parent.view.shutdown()

    child_lt = parent.start(svc)
    async with create_task_group() as observe_tg:
        # Bind the parent lifetime (if the child is stopped, shutdown the parent)
        observe_tg.start_soon(bind_parent_to, child_lt)
        await child_lt.started
        yield child_lt
        observe_tg.cancel_scope.cancel()
        child_lt.shutdown()
        await child_lt.stopped


async def run(svc_f: ServiceF, /, parent: ServiceLifetime | None = None) -> int:
    async with (nullcontext(parent.portal) if parent else BlockingPortal()) as portal:
        lt = ServiceLifetime(portal)
        await _run(svc_f, lt)
        return lt.exit_code
