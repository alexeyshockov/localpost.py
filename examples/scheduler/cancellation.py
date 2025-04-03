#!/usr/bin/env python

import logging
from asyncio import CancelledError
from datetime import timedelta

import anyio

from localpost.hosting import Host
from localpost.hosting.middlewares import shutdown_timeout
from localpost.scheduler import Scheduler, every

scheduler = Scheduler()
host = Host(scheduler)
host.root_service //= shutdown_timeout(1)


@scheduler.task(every(timedelta(seconds=3)))
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


if __name__ == "__main__":
    import localpost

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(host))
