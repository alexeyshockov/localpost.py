import math
from contextlib import AbstractAsyncContextManager
from typing import Any

from anyio import fail_after

from localpost._utils import wait_all, wait_any
from localpost.hosting._host import HostedService, HostedServiceDecorator, HostedServiceFunc, _ServiceLifetime, logger

__all__ = [
    "lifespan",
    "start_timeout",
    "shutdown_timeout",
]


def lifespan(cm: AbstractAsyncContextManager[Any], /) -> HostedServiceDecorator:
    def decorator(svc_func: HostedServiceFunc, **attrs) -> HostedService:
        @HostedService.wraps(svc_func, attrs)
        async def run(sl, *args):
            assert isinstance(sl, _ServiceLifetime)
            async with cm:
                await svc_func(sl, *args)
                if child_services := sl.child_services:
                    await sl.shutting_down
                    for child in child_services:
                        child.shutdown()
                    await wait_all(child.stopped for child in child_services)

        return run

    return decorator


def start_timeout(timeout: float, /) -> HostedServiceDecorator:
    def decorator(svc_func: HostedServiceFunc, **attrs) -> HostedService:
        if timeout > attrs.get("start_timeout", math.inf):
            raise ValueError("Timeout must be less than the existing one")
        attrs["start_timeout"] = timeout

        @HostedService.wraps(svc_func, attrs)
        def run(sl, *args):
            assert isinstance(sl, _ServiceLifetime)
            sl.parent_tg.start_soon(_observe_service_start, sl, timeout)
            return svc_func(sl, *args)

        return run

    return HostedService.decorator(decorator)


def shutdown_timeout(timeout: float, /) -> HostedServiceDecorator:
    def decorator(svc_func: HostedServiceFunc, **attrs) -> HostedService:
        if timeout > attrs.get("shutdown_timeout", math.inf):
            raise ValueError("Timeout must be less than the existing one")
        attrs["shutdown_timeout"] = timeout

        @HostedService.wraps(svc_func, attrs)
        def run(sl, *args):
            assert isinstance(sl, _ServiceLifetime)
            sl.parent_tg.start_soon(_observe_service_shutdown, sl, timeout)
            return svc_func(sl, *args)

        return run

    return HostedService.decorator(decorator)


async def _observe_service_shutdown(svc: _ServiceLifetime, timeout: float):
    if math.isinf(timeout):
        return
    assert timeout >= 0
    await wait_any(svc.shutting_down, svc.stopped)
    if svc.stopped:
        return
    service_scope = svc.tg.cancel_scope
    if timeout == 0:
        service_scope.cancel()
        return
    try:
        with fail_after(timeout):
            await svc.stopped
    except TimeoutError as exc:
        svc.exception = exc
        logger.error(f"{svc.name} shutdown timeout")
        service_scope.cancel()


async def _observe_service_start(svc: _ServiceLifetime, timeout: float):
    if math.isinf(timeout):
        return
    assert timeout > 0
    try:
        with fail_after(timeout):
            await wait_any(svc.started, svc.stopped)
    except TimeoutError as exc:
        svc.exception = exc
        logger.error(f"{svc.name} startup timeout")
        svc.tg.cancel_scope.cancel()
