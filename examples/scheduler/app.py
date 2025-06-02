#!/usr/bin/env python

import logging
import random

from localpost.scheduler import Scheduler, after, delay, every

logging.basicConfig()
logging.getLogger("localpost").setLevel(logging.DEBUG)

scheduler = Scheduler()


@scheduler.task(every("3s") // delay((0, 1)))
async def task1(_):
    """
    A simple repeating task that returns a random number.
    """
    print("task1 running!")
    return random.randint(1, 22)


@scheduler.task(after(task1))
async def task2(task1_result: str):
    """
    A task that runs after task1 and prints its result.
    """
    print(f"task2 here! task1 result: {task1_result}")


# @scheduler.task(recurrent())
# async def task3(_):
#     """
#     A more complex repeating task, which period is dynamically changed.
#     """
#     print("task3 here!")
#     # And override the next iteration's interval (delay)
#     return timedelta(seconds=random.randint(3, 9))


if __name__ == "__main__":
    import localpost

    exit(localpost.run(scheduler))
