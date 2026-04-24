#!/usr/bin/env python
import logging
import random
import sys

from localpost.hosting import run_app
from localpost.scheduler import after, delay, every, scheduled_task, take_first


@scheduled_task(every("3s") // delay((0, 1)))
async def task1():
    """
    A simple repeating task that returns a random number.
    """
    print("task1 running!")
    return random.randint(1, 22)


@scheduled_task(after(task1) // take_first(3))
async def task2(task1_result: int):
    """
    A task that runs after task1 and prints its result.
    """
    print(f"task2 here! task1 result: {task1_result}")


if __name__ == "__main__":
    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    sys.exit(run_app(task1, task2))
