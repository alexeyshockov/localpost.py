#!/usr/bin/env python

import time

import anyio

from localpost.hosting import ServiceLifetimeManager
from localpost.hosting.app_host import AppHost

host = AppHost()


@host.service()
def a_sync_service(service_lifetime: ServiceLifetimeManager):
    print(f"{a_sync_service.name} started")
    service_lifetime.set_started()
    # Do not do infinite loops in a sync service function, as there is no way to interrupt it from the host.
    # Always check the service_lifetime.shutting_down event to see if the service should stop.
    while not service_lifetime.shutting_down.is_set():
        print(f"{a_sync_service.name} running")
        time.sleep(1)
        raise RuntimeError("This is a test error from _sync_")
    print(f"{a_sync_service.name} stopped")


@host.service()
async def an_async_func():
    print(f"{an_async_func.name} started")
    try:
        while True:
            print(f"{an_async_func.name} running")
            await anyio.sleep(1)
            # raise RuntimeError("This is a test error from _async_")
    finally:
        print(f"{an_async_func.name} done")


if __name__ == "__main__":
    import logging

    import localpost

    logging.basicConfig()
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(host))
