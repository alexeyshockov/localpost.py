#!/usr/bin/env python
import logging
from asyncio import CancelledError
from datetime import timedelta

import anyio

from localpost.hosting import run_app
from localpost.scheduler import every, scheduled_task


@scheduled_task(every(timedelta(seconds=3)))
async def long_async_task():
    print("long_async_task is triggered!")
    try:
        # On a normal shutdown (Ctrl+C), the scheduler will wait for the trigger to signal stop,
        # then the task loop ends gracefully.
        await anyio.sleep(15)
    except CancelledError:
        # In case of forced shutdown (e.g. second Ctrl+C)
        print("Forced shutdown detected!")
        raise
    finally:
        # Will be executed always
        print("long_async_task has finished!")


if __name__ == "__main__":
    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    run_app(long_async_task)
