import logging
import random
import sys
from datetime import timedelta

from localpost.hosting import run_app
from localpost.scheduler import delay, every, scheduled_task, take_first


@scheduled_task(every(timedelta(seconds=3)) // take_first(3) // delay((0, 3)))
def task1(_):
    """
    A simple repeating task that returns a random number.
    """
    print("task1 running!")
    return random.randint(1, 22)  # Not used


if __name__ == "__main__":
    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    sys.exit(run_app(task1))
