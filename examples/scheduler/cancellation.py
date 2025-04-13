#!/usr/bin/env python

import logging
from asyncio import CancelledError
from datetime import timedelta

import anyio

from localpost.hosting import hosted_service
from localpost.hosting.middlewares import shutdown_timeout
from localpost.scheduler import every, scheduled_task


@scheduled_task(every(timedelta(seconds=3)))
async def long_async_task():
    print("long_async_task is triggered!")
    try:
        # On a normal shutdown (Ctrl+C), the scheduler will just wait for the current task execution to finish
        await anyio.sleep(15)
    except CancelledError:
        # Only in case of forced shutdown (second Ctrl+C (results to Host.stop()) or the shutdown timeout)
        print("Forced shutdown detected!")
        raise
    finally:
        # Will be executed always
        print("long_async_task has finished!")


# host = Host(long_async_task)
# host.root_service //= shutdown_timeout(1)
host = hosted_service(long_async_task) // shutdown_timeout(1)


if __name__ == "__main__":
    import localpost

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(host))
