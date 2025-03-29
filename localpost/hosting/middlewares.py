import math
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from functools import wraps
from typing import Any

from anyio import fail_after

from localpost._utils import wait_all, wait_any
from localpost.hosting._host import HostedServiceDecorator, _ServiceLifetime, logger

__all__ = [
    "lifespan",
    "start_timeout",
    "shutdown_timeout",
]


def lifespan(cm: AbstractAsyncContextManager[Any], /) -> HostedServiceDecorator:
    def decorator(svc_func) -> Callable[..., Awaitable[None]]:
        @wraps(svc_func)
        async def _run(sl: _ServiceLifetime, *args):
            async with cm:
                await svc_func(sl, *args)
                if child_services := sl.child_services:
                    await sl.shutting_down
                    for child in child_services:
                        child.shutdown()
                    await wait_all(child.stopped for child in child_services)

        return _run

    return decorator


def start_timeout(timeout=math.inf, /) -> HostedServiceDecorator:
    def decorator(svc_func) -> Callable[..., Awaitable[None]]:
        @wraps(svc_func)
        def _run(sl: _ServiceLifetime, *args):
            sl.parent_tg.start_soon(_observe_service_startup, sl, timeout)
            return svc_func(sl, *args)

        return _run

    return decorator


def shutdown_timeout(timeout=math.inf, /) -> HostedServiceDecorator:
    def decorator(svc_func) -> Callable[..., Awaitable[None]]:
        @wraps(svc_func)
        def _run(sl: _ServiceLifetime, *args):
            sl.parent_tg.start_soon(_observe_service_shutdown, sl, timeout)
            return svc_func(sl, *args)

        return _run

    return decorator


async def _observe_service_shutdown(lifetime: _ServiceLifetime, timeout: float):
    if math.isinf(timeout):
        return
    assert timeout >= 0
    await wait_any(lifetime.shutting_down, lifetime.stopped)
    if lifetime.stopped:
        return
    service_scope = lifetime.tg.cancel_scope
    if timeout == 0:
        service_scope.cancel()
        return
    try:
        with fail_after(timeout):
            await lifetime.stopped
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
            await wait_any(lifetime.started, lifetime.stopped)
    except TimeoutError as exc:
        lifetime.exception = exc
        logger.error(f"{lifetime.name} startup timeout")
        lifetime.tg.cancel_scope.cancel()
