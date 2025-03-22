import math
from collections.abc import Awaitable
from contextlib import AbstractAsyncContextManager
from typing import Any

from anyio import fail_after

from localpost._utils import wait_any
from localpost.hosting import HostedServiceFunc, ServiceLifetimeManager
from localpost.hosting._host import ServiceMiddleware, _ServiceLifetime, logger

__all__ = [
    "lifespan",
    "start_timeout",
    "shutdown_timeout",
]


def lifespan(cm: AbstractAsyncContextManager[Any], /) -> ServiceMiddleware:
    def decorator(func: HostedServiceFunc) -> HostedServiceFunc:
        async def _run(service_lifetime: ServiceLifetimeManager, *args) -> None:
            async with cm:
                await func(service_lifetime, *args)

        return _run

    return decorator


# def with_timeout(*, start_timeout: float = math.inf, shutdown_timeout: float = math.inf) -> ServiceMiddleware:
#     def decorator(func: HostedServiceFunc) -> HostedServiceFunc:
#         async def _run(service_lifetime: ServiceLifetimeManager, *args) -> None:
#             sl = service_lifetime
#             assert isinstance(sl, _ServiceLifetime)
#             sl.parent_tg.start_soon(_observe_service_startup, sl, start_timeout)
#             sl.parent_tg.start_soon(_observe_service_shutdown, sl, shutdown_timeout)
#             await func(service_lifetime, *args)
#
#         return _run
#
#     return decorator


def start_timeout(timeout=math.inf, /) -> ServiceMiddleware:
    def decorator(func: HostedServiceFunc) -> HostedServiceFunc:
        def _run(service_lifetime: ServiceLifetimeManager, *args) -> Awaitable[None]:
            sl = service_lifetime
            assert isinstance(sl, _ServiceLifetime)
            sl.parent_tg.start_soon(_observe_service_startup, sl, timeout)
            return func(service_lifetime, *args)

        return _run

    return decorator


def shutdown_timeout(timeout=math.inf, /) -> ServiceMiddleware:
    def decorator(func: HostedServiceFunc) -> HostedServiceFunc:
        def _run(service_lifetime: ServiceLifetimeManager, *args) -> Awaitable[None]:
            sl = service_lifetime
            assert isinstance(sl, _ServiceLifetime)
            sl.parent_tg.start_soon(_observe_service_shutdown, sl, timeout)
            return func(service_lifetime, *args)

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
