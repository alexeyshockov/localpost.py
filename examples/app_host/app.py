#!/usr/bin/env python

import time

import anyio
from anyio import CancelScope

from localpost.hosting import ServiceLifetimeManager, AppHost
from localpost.hosting.middlewares import shutdown_timeout

app = AppHost()


@app.service
def a_sync_service(service_lifetime: ServiceLifetimeManager):
    print(f"{a_sync_service.name} started")
    service_lifetime.set_started()
    while not service_lifetime.shutting_down:
        print(f"{a_sync_service.name} running")
        time.sleep(1)
    print(f"{a_sync_service.name} stopped")


@app.service
async def an_async_func(_):
    print(f"{an_async_func.name} started")
    while True:
        print(f"{an_async_func.name} running")
        await anyio.sleep(1)


@app.service
async def an_async_service(service_lifetime: ServiceLifetimeManager):
    print(f"{an_async_service.name} started")
    service_lifetime.set_started()
    while not service_lifetime.shutting_down:
        print(f"{an_async_service.name} running")
        await anyio.sleep(1)


@app.service
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
    import logging

    logging.basicConfig()
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    app.root_service //= shutdown_timeout(5)

    exit(localpost.run(app))
