#!/usr/bin/env python

import logging
import random
from datetime import timedelta

from fast_depends import Depends, inject

from localpost.scheduler import delay, every, scheduled_task


def roll_dice():
    return random.randint(1, 6)


@scheduled_task(every(timedelta(seconds=3)) // delay((0, 3)))
@inject
def print_task(
    n1: int = Depends(roll_dice, use_cache=False),
    n2: int = Depends(roll_dice, use_cache=False),
    n3: int = Depends(roll_dice, use_cache=False),
):
    print(f"Rolling dices: {n1}, {n2}, {n3}")


if __name__ == "__main__":
    import localpost

    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("localpost").setLevel(logging.DEBUG)

    exit(localpost.run(print_task))
