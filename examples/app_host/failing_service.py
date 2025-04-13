#!/usr/bin/env python

import time

import anyio

from localpost.hosting import AppHost, ServiceLifetimeManager

app = AppHost()


@app.service
def a_sync_service(service_lifetime: ServiceLifetimeManager):
    print(f"{a_sync_service.__name__} started")
    service_lifetime.set_started()
    # Do not do infinite loops in a sync service function, as there is no way to interrupt it from the host.
    # Always check the service_lifetime.shutting_down event to see if the service should stop.
    while not service_lifetime.shutting_down:
        print(f"{a_sync_service.__name__} running")
        time.sleep(1)
        raise RuntimeError("This is a test error from _sync_")
    print(f"{a_sync_service.__name__} stopped")


@app.service
async def an_async_func():
    print(f"{an_async_func.__name__} started")
    try:
        while True:
            print(f"{an_async_func.__name__} running")
            await anyio.sleep(1)
            # raise RuntimeError("This is a test error from _async_")
    finally:
        print(f"{an_async_func.__name__} done")


if __name__ == "__main__":
    import logging

    import localpost

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(app))
