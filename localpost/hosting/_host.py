from __future__ import annotations

import dataclasses as dc
import logging
import threading
from _contextvars import ContextVar
from collections.abc import Callable
from typing import (
    Any,
    ClassVar,
    Literal,
    Protocol,
    final,
)

from anyio import (
    CancelScope,
)
from anyio.from_thread import BlockingPortal

from localpost._utils import (
    EventView,
)

logger = logging.getLogger("localpost.hosting")


# @final
# @dc.dataclass(frozen=True, slots=True)
# class Created:
#     name: ClassVar[Literal["created"]] = "created"
#
#
# @final
# @dc.dataclass(frozen=True, slots=True)
# class Starting:
#     name: ClassVar[Literal["starting"]] = "starting"
#     # timeout: float
#
#
# @final
# @dc.dataclass(frozen=True, slots=True)
# class Running:
#     name: ClassVar[Literal["running"]] = "running"
#     value: Any = None
#     # graceful_shutdown_scope: CancelScope | None = None
#
#
# @final
# @dc.dataclass(frozen=True, slots=True)
# class ShuttingDown:
#     name: ClassVar[Literal["shutting_down"]] = "shutting_down"
#     reason: BaseException | str | None = None
#     # timeout: float
#
#
# @final
# @dc.dataclass(frozen=True, slots=True)
# class Stopped:
#     name: ClassVar[Literal["stopped"]] = "stopped"
#     shutdown_reason: BaseException | str | None = None
#     exception: BaseException | None = None


# ServiceState = Starting | Running | ShuttingDown | Stopped
HostState = Created | Starting | Running | ShuttingDown | Stopped



class HostLifetime(Protocol):
    exit_code: int = 0

    @property
    def name(self) -> str: ...

    @property
    def state(self) -> HostState: ...

    @property
    def status(self) -> ServiceStatus: ...

    @property
    def started(self) -> EventView: ...

    @property
    def shutting_down(self) -> EventView: ...

    @property
    def stopped(self) -> EventView: ...

    def shutdown(self, *, reason: BaseException | str | None = None) -> None: ...


_current_host: ContextVar[Host] = ContextVar("localpost.current_host")


def current_host() -> Host:
    if _current_host.get(None) is None:
        raise RuntimeError("Not in localpost hosting context")
    return _current_host.get()


@final
@dc.dataclass(slots=True)
class Host:  # Actually a Host run
    portal: BlockingPortal
    thread_id: int
    run_scope: CancelScope
    _exit_code: int | None = None

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
    def state(self) -> HostState:
        if self._exec_context:
            return self._exec_context.root_service_lifetime.state
        return Created()

    # @property
    # def status(self) -> ServiceStatus:
    #     if self._exec_context:
    #         return self._exec_context.root_service_lifetime.status
    #     return {
    #         "name": self.name,
    #         "state": "created",
    #         "services": [],
    #         "shutdown_reason": None,
    #         "exception": None,
    #     }

    @property
    def same_thread(self) -> bool:
        return threading.get_ident() == self.thread_id



    def stop(self) -> None:
        in_host_thread(self, self.run_scope.cancel)


# def in_host_thread(h: HostLifetime, func: Callable[..., T]) -> T:
def in_host_thread[T](h: Host, func: Callable[..., T]) -> T:
    if h.same_thread:
        return func()
    return h.portal.start_task_soon(func).result()
