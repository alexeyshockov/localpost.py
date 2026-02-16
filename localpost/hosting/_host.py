from __future__ import annotations

import dataclasses as dc
import logging
import threading
from _contextvars import ContextVar
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import (
    ClassVar,
    Literal,
    final, Any, TypeVar, overload, cast, Awaitable,
)

import anyio
from anyio import (
    CancelScope, create_task_group, get_cancelled_exc_class,
)
from anyio import open_signal_receiver
from anyio.abc import TaskGroup
from anyio.from_thread import BlockingPortal

from localpost._utils import (
    EventView, Event, cancellable_from, unwrap_exc,
)
from localpost._utils import HANDLED_SIGNALS, choose_anyio_backend

__all__ = ["ServiceLifetime", "HostLifetime", "current_host", "serve", "serve_app", "run", "arun"]

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


ServiceState = Starting | Running | ShuttingDown | Stopped


_current_host: ContextVar[HostLifetime] = ContextVar("localpost.current_host")


def current_host() -> HostLifetime:
    if _current_host.get(None) is None:
        raise RuntimeError("Not in localpost hosting context")
    return _current_host.get()


def _start_scv[T: AbstractLifetime](
    lt: T, svc: Callable[[T], Awaitable[None]], /,
    start_timeout: float | None = None,
    shutdown_timeout: float | None = None,
) -> None:
    @wraps(svc)
    async def _observed():
        try:
            await svc(lt)
        except get_cancelled_exc_class():  # BaseException
            raise
        except Exception as exc:
            source_exc = unwrap_exc(exc)
            # lt.exception = source_exc
            object.__setattr__(lt, "exception", source_exc)
        finally:
            lt.stopped.set()

    lt.tg.start_soon(_observed)


@dc.dataclass(frozen=True, eq=False, slots=True)
class AbstractLifetime:
    tg: TaskGroup
    portal: BlockingPortal
    thread_id: int = dc.field(default_factory=threading.get_ident)

    started: EventView = dc.field(default_factory=Event)
    shutting_down: EventView = dc.field(default_factory=Event)
    stopped: EventView = dc.field(default_factory=Event)

    shutdown_reason: BaseException | str | None = None
    exception: BaseException | None = None

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

    def wait_started(self) -> None:
        """Helper for sync code, to wait in a thread."""
        if self.same_thread:
            raise RuntimeError("Deadlock: synchronous wait in the async thread")
        return self.portal.start_task_soon(self.started.wait).result()

    def wait_shutting_down(self) -> None:
        """Helper for sync code, to wait in a thread."""
        if self.same_thread:
            raise RuntimeError("Deadlock: synchronous wait in the async thread")
        return self.portal.start_task_soon(self.shutting_down.wait).result()

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

    @property
    def same_thread(self) -> bool:
        return threading.get_ident() == self.thread_id

    def set_started(self) -> None:
        def do_set_started():
            assert not self.stopped, "Cannot mark already stopped service as started"
            if self.started.is_set():
                return
            cast(Event, self.started).set()

        in_host_thread(self, do_set_started)

    def shutdown(self, *, reason: BaseException | str | None = None) -> None:
        def do_shutdown():
            if self.stopped or self.shutting_down:
                return
            # self.shutdown_reason = reason
            object.__setattr__(self, "shutdown_reason", reason)
            cast(Event, self.shutting_down).set()

        in_host_thread(self, do_shutdown)

    def stop(self) -> None:
        in_host_thread(self, self.run_scope.cancel)


@final
@dc.dataclass(frozen=True, eq=False, slots=True)
class ServiceLifetime(AbstractLifetime):
    pass


@final
@dc.dataclass(frozen=True, eq=False, slots=True)
class HostLifetime(AbstractLifetime):
    _exit_code: int | None = None

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


def in_host_thread[T](h: AbstractLifetime, func: Callable[..., T]) -> T:
    if h.same_thread:
        return func()
    return h.portal.start_task_soon(func).result()

ServiceF = Callable[[ServiceLifetime], Awaitable[None]]
AppF = Callable[[HostLifetime], Awaitable[None]]


@asynccontextmanager
async def serve(app: ServiceF, /):
    """Start the target service and return control to the caller."""
    async with BlockingPortal() as portal, create_task_group() as host_tg:
        host_lifetime = HostLifetime(host_tg, portal)
        _start_scv(host_lifetime, app)
        yield host_lifetime
        host_lifetime.shutdown()


@asynccontextmanager
async def serve_app(app: AppF, /):
    """Start the target app and return control to the caller."""
    async with BlockingPortal() as portal, create_task_group() as host_tg:
        host_lifetime = HostLifetime(host_tg, portal)
        _start_scv(host_lifetime, app)
        yield host_lifetime
        host_lifetime.shutdown()


def run(app_f: AppF, /) -> int:
    """Run the target app until it stops or is interrupted by a signal."""
    return anyio.run(arun, app_f, **choose_anyio_backend())


async def arun(app_f: AppF, /) -> int:
    """Run the target app until it stops or is interrupted by a signal."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Signals can only be installed on the main thread")

    async def handle_signals():
        with open_signal_receiver(*HANDLED_SIGNALS) as signals:
            async for _ in signals:
                # First Ctrl+C (or other termination method)
                if not host.shutting_down:
                    logger.info("Shutting down...")
                    host.shutdown()
                    continue
                # Ctrl+C again
                logger.warning("Forced shutdown")
                host.stop()
                break

    async with serve_app(app_f) as host:
        await host.cancel_on_stop(handle_signals)()

    return host.exit_code
