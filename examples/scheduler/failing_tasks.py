#!/usr/bin/env python

import logging
from datetime import timedelta

from localpost.scheduler import Scheduler, every, delay, take_first

logging.basicConfig()
logging.getLogger("localpost").setLevel(logging.DEBUG)

scheduler = Scheduler()


@scheduler.task(every(timedelta(seconds=3)) // delay((0, 3)) // take_first(3))
async def an_async_task():
    print("an_async_task running")
    raise RuntimeError("This is a test error from _async_")


@scheduler.task(every(timedelta(seconds=5)) // delay((0, 3)))
def a_sync_task():
    print("a_sync_task running")
    raise RuntimeError("This is a test error from _sync_")


if __name__ == "__main__":
    import localpost

    exit(localpost.run(scheduler))
