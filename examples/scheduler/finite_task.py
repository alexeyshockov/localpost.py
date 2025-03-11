#!/usr/bin/env python

import logging
import random
from datetime import timedelta

from localpost.scheduler import Scheduler, every, take_first, delay

logging.basicConfig()
logging.getLogger("localpost").setLevel(logging.DEBUG)

scheduler = Scheduler()


@scheduler.task(every(timedelta(seconds=3)) // take_first(3) // delay((0, 3)))
async def task1():
    """
    A simple repeating task that returns a random number.
    """
    print("task1 running!")
    return random.randint(1, 22)  # Not used


if __name__ == "__main__":
    import localpost

    exit(localpost.run(scheduler))
