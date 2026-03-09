#!/usr/bin/env python

import logging
import random

from localpost.scheduler import Scheduler, after, delay, every, run, take_first

scheduler = Scheduler()


@scheduler.task(every("3s") // delay((0, 1)))
async def task1():
    """
    A simple repeating task that returns a random number.
    """
    print("task1 running!")
    return random.randint(1, 22)


@scheduler.task(after(task1) // take_first(3))
async def task2(task1_result: int):
    """
    A task that runs after task1 and prints its result.
    """
    print(f"task2 here! task1 result: {task1_result}")


if __name__ == "__main__":
    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(run(scheduler))
