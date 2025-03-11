#!/usr/bin/env python

import logging
import time

import anyio
from anyio import CancelScope

from localpost.hosting import ServiceLifetimeManager
from localpost.hosting.app_host import AppHost

logging.basicConfig()
logging.getLogger("localpost").setLevel(logging.DEBUG)

host = AppHost()
host._shutdown_timeout = 5


@host.service()
def a_sync_service(service_lifetime: ServiceLifetimeManager):
    print("Service started")
    service_lifetime.set_started()
    while not service_lifetime.stopped:
        print("Service running")
        time.sleep(1)
    print("Service stopped")


@host.service()
async def an_async_func(_):
    print(f"{an_async_func.name} started")
    while True:
        print(f"{an_async_func.name} running")
        await anyio.sleep(1)


@host.service()
async def an_async_service(service_lifetime: ServiceLifetimeManager):
    print(f"{an_async_service.name} started")
    service_lifetime.set_started()
    while service_lifetime.shutting_down.is_set():
        print(f"{an_async_service.name} running")
        await anyio.sleep(1)


@host.service()
async def a_graceful_async_service(service_lifetime: ServiceLifetimeManager):
    print(f"{a_graceful_async_service.name} started")
    with CancelScope() as scope:
        service_lifetime.set_started(graceful_shutdown_scope=scope)
        while not scope.cancel_called:
            print(f"{a_graceful_async_service.name} running")
            await anyio.sleep(1)
    print(f"{a_graceful_async_service.name} gracefully shut down")


# @host.service()
# async def an_anyio_func(*, task_status: TaskStatus[None] = TASK_STATUS_IGNORED):
#     print(f"{an_anyio_func.__name__} started")
#     task_status.started()
#     while True:
#         print(f"{an_anyio_func.__name__} running")
#         await anyio.sleep(1)
#
#
# @host.service()
# async def a_graceful_anyio_func(*, task_status: TaskStatus[CancelScope] = TASK_STATUS_IGNORED):
#     print(f"{a_graceful_anyio_func.__name__} started")
#     with CancelScope() as scope:
#         task_status.started(scope)
#         while not scope.cancel_called:
#             print(f"{a_graceful_anyio_func.__name__} running")
#             await anyio.sleep(1)
#     print(f"{a_graceful_anyio_func.__name__} gracefully shut down")


# async def an_async_service_tpl(n: int):
#     print(f"Async Service {n} started")
#     while True:
#         print(f"Async Service {n} running")
#         await anyio.sleep(1)
#
#
# host.service("first_service")(partial(an_async_service_tpl, 1))
# host.service("second_service")(partial(an_async_service_tpl, 2))


if __name__ == "__main__":
    import localpost

    exit(localpost.run(host))
